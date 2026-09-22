"""yt-dlp 的 argv 装配与调用。

**yt-dlp 是子进程，不是 pip 包**（V1 的 cookie 三档、`--flat-playlist` 枚举、
DASH 分片行为全在命令行上）。这一层的职责是把"档位阶梯"这件事收成一份实现。

V1 §7.15 是这整个模块的存在理由，两条：

1. **枚举与下载是分开的两条路，两条都得带导出 cookie**。
   无 cookie 时 `--flat-playlist` 会随机回 `Request is rejected by server (352)` /
   `Request is blocked by server (412)`，同一台机器上一条过一条不过 ——
   所以"我手动跑通了"不能证明链路稳。
2. **退档的判据要认得全报错原文**。旧代码只在错误文本命中"读 cookie 失败"那几句时才退档，
   而 Chrome 开着时的那句 `Could not copy Chrome cookie database` **不在表里**，
   于是退档不触发、整条判死。`looks_like_cookie_failure()` 就是那张表，
   每加一种写法都要往这里加一条。

档位差别**不是"能不能下"，是画质**：同一条 B站 视频，带导出 cookie 回 21 条视频轨/
最高 1772p@59.94，匿名只有 15 条/886p@29.97。所以"降级成功"必须在结果里说清是哪一档。
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

from intelligence_hub_v2.errors import MediaDownloadError
from intelligence_hub_v2.infra.subprocess import SubprocessResult, run_subprocess
from intelligence_hub_v2.logging import get_logger
from intelligence_hub_v2.platforms.base import CookieVariant

logger = get_logger(__name__)

__all__ = [
    "YtDlpCookieVariant",
    "YtDlpResult",
    "YtDlpRunner",
    "looks_like_cookie_failure",
    "plan_cookie_variants",
    "should_escalate_cookie_rung",
]

YTDLP = "yt-dlp"
"""命令名。不解析成绝对路径：让 PATH 解析保持一致，"装了但没进 PATH"要如实报错。"""

ProgressFn = Callable[[str], None]

COOKIE_READ_FAILURE_MARKERS: tuple[str, ...] = (
    "Could not copy Chrome cookie database",
    "Failed to decrypt with DPAPI",
    "cannot open cookie file",
    "Cookie file",
    "NSLError",
    "Operation not permitted",
    "browser cookie",
    "Lockbox",
)
"""判定"这次失败是 cookie **读取**问题"的原文片段表。

大小写不敏感匹配。**宁可多认**：多认的后果只是白试一档（几秒），
而 V1 的教训正好是少认了一句（`Could not copy Chrome cookie database` 不在表里）
→ 退档不触发 → 整轮判死（§7.15）。
"""

RISK_CONTROL_MARKERS: tuple[str, ...] = (
    "Request is blocked by server",
    "Request is rejected by server",
    "(412)",
    "(352)",
    "Fresh cookies (not necessarily logged in) are needed",
    "captcha",
    "gated",
    "Login required",
)
"""风控形状的错。它**不是** cookie 读不出来，但处置动作一样：换一档 cookie 再试。

V1 §7.15 实测：B站 的 `--flat-playlist` 无 cookie 时随机回 352/412，带导出 cookie 就过。
所以"看到 412 就判死"等于放弃一档确实能成的路径。
"""

_MEDIA_SUFFIXES = (".mp4", ".mkv", ".webm", ".m4a", ".mp3", ".opus")


def looks_like_cookie_failure(text: str) -> bool:
    """这句话像不像"cookie 读不出来"。见模块 docstring 第 2 条。"""
    lowered = text.lower()
    return any(marker.lower() in lowered for marker in COOKIE_READ_FAILURE_MARKERS)


def should_escalate_cookie_rung(text: str) -> bool:
    """该不该退到下一档 cookie。

    两个判据分开定义是有意义的：`looks_like_cookie_failure` 回答
    "是不是 cookie 读不出来"（预检与文案要用它 —— 那决定人是去修 DPAPI 还是去登录），
    这里回答"要不要再试一档"（处置动作相同，理由完全不同）。
    合成一个函数的话，"风控 412"会被文案说成"cookie 读不出来"，
    于是人去找 cookie 库的解密问题，而真正的原因是压根没带 cookie。
    """
    if looks_like_cookie_failure(text):
        return True
    lowered = text.lower()
    return any(marker.lower() in lowered for marker in RISK_CONTROL_MARKERS)


@dataclass(frozen=True)
class YtDlpCookieVariant:
    """阶梯上的一档，带它自己的 argv 片段与**给人看的名字**。

    `label` 是必须的：清单 note 要写"这次是带桥导出的登录 cookie 下的"还是
    "匿名下载（4K/高帧率档不可用）"。V1 §7.15 明确过 —— 档位差别是画质，
    不写清楚就没人知道这批视频为什么糊。
    """

    kind: CookieVariant
    args: tuple[str, ...]
    label: str

    def __str__(self) -> str:
        return self.label


@dataclass(frozen=True)
class YtDlpResult:
    """一次 yt-dlp 调用的结果。`attempts` 是**每一档的原文**，不许只留最后一条。

    为什么留全部：V1 §7.2 的经验是"兜底成功了就把 yt-dlp 的失败原文丢掉，
    结果日志只剩一句『未拿到媒体』，等于没法判断该修什么"。
    """

    ok: bool
    variant: YtDlpCookieVariant | None
    stdout: str
    stderr: str
    returncode: int
    artifacts: tuple[Path, ...] = ()
    attempts: tuple[tuple[str, int, str], ...] = ()
    """`(档位标签, 退出码, stderr 尾部)`，按尝试顺序。"""

    def attempts_note(self) -> str:
        """给清单 `note` / `yt_dlp_error` 用的一行人话。"""
        if not self.attempts:
            return "yt-dlp 没有产生任何尝试记录"
        return "；".join(f"{label}→exit {code}" for label, code, _ in self.attempts)


def plan_cookie_variants(
    order: Sequence[CookieVariant],
    *,
    cookies_file: Path | None = None,
    browser: str | None = None,
) -> list[YtDlpCookieVariant]:
    """按声明的顺序生成 argv 片段，**跳过没法执行的那几档**。

    `order` 来自 `Capabilities.cookie_variants`（V1 §7.15 那条阶梯的契约化）。
    跳过规则：
    - `exported_file` 但文件不在 → 跳。传一个不存在的路径给 `--cookies`，
      yt-dlp 报的是"打不开文件"，看起来像权限问题。
    - `browser` 但没有浏览器名 → 跳（Windows 上这一档基本永远读不出来，
      所以抖音的阶梯通常根本不列它 —— 声明了就得多花一次子进程时间）。
    - `anonymous` / `none` → 永远可用，是阶梯的兜底档。
    """
    out: list[YtDlpCookieVariant] = []
    for kind in order:
        if kind == "exported_file":
            if cookies_file is None or not cookies_file.is_file():
                continue
            out.append(
                YtDlpCookieVariant(kind, ("--cookies", str(cookies_file)), "带导出的登录 cookie")
            )
        elif kind == "browser":
            if not browser:
                continue
            out.append(
                YtDlpCookieVariant(
                    kind, ("--cookies-from-browser", browser), f"从浏览器 {browser} 读 cookie"
                )
            )
        elif kind in ("anonymous", "none"):
            out.append(YtDlpCookieVariant(kind, (), "匿名（登录档画质不可用）"))
        else:  # pragma: no cover - Literal 收窄后走不到，留着是为了加新档位时这里会红
            msg = f"未知的 cookie 档位: {kind!r}"
            raise ValueError(msg)
    return out


class YtDlpRunner:
    """yt-dlp 的异步封装。`default_variants` 是这个平台的阶梯，单条命令可覆盖。"""

    def __init__(
        self,
        *,
        default_variants: Sequence[YtDlpCookieVariant] = (),
        timeout_seconds: float = 900.0,
        extra_args: Sequence[str] = (),
    ) -> None:
        self._default_variants = tuple(default_variants)
        self._timeout = timeout_seconds
        self._extra_args = tuple(extra_args)

    async def download(
        self,
        url: str,
        dest_dir: Path,
        *,
        variants: Sequence[YtDlpCookieVariant] | None = None,
        file_template: str = "%(id)s.%(ext)s",
        on_line: ProgressFn | None = None,
        timeout: float | None = None,
    ) -> YtDlpResult:
        """按档位阶梯依次尝试下载。

        **退档只在"值得再试一档"时发生**（`should_escalate_cookie_rung`），其它失败立刻返回。
        这是对 V1 的一处有意改动：V1 三档盲试，于是一条真·已删除的视频要等三次超时才判死。
        """
        ladder = tuple(variants) if variants is not None else self._default_variants
        if not ladder:
            msg = f"没有可用的 cookie 档位，下载不了 {url}"
            raise MediaDownloadError("ytdlp", "download", msg)

        attempts: list[tuple[str, int, str]] = []
        last: SubprocessResult | None = None
        for variant in ladder:
            result = await self._run(
                [
                    *self._base_args(),
                    *variant.args,
                    "--no-playlist",
                    "--restrict-filenames",
                    "-o",
                    str(dest_dir / file_template),
                    url,
                ],
                cwd=dest_dir,
                on_line=on_line,
                timeout=timeout,
            )
            last = result
            attempts.append((variant.label, result.returncode, _tail(result.stderr)))
            if result.ok:
                return YtDlpResult(
                    ok=True,
                    variant=variant,
                    stdout=result.stdout,
                    stderr=result.stderr,
                    returncode=result.returncode,
                    artifacts=_artifacts_from(result.stdout, dest_dir),
                    attempts=tuple(attempts),
                )
            if not should_escalate_cookie_rung(result.stderr):
                break

        assert last is not None  # ladder 非空 ⇒ 至少跑过一次
        return YtDlpResult(
            ok=False,
            variant=ladder[-1],
            stdout=last.stdout,
            stderr=last.stderr,
            returncode=last.returncode,
            attempts=tuple(attempts),
        )

    async def flat_playlist(
        self,
        url: str,
        *,
        variants: Sequence[YtDlpCookieVariant] | None = None,
        playlist_items: int | None = None,
        timeout: float | None = None,
    ) -> YtDlpResult:
        """`--flat-playlist -J`：只枚举，不下媒体。V1 §7.15：这一路**也要带 cookie**。"""
        ladder = tuple(variants) if variants is not None else self._default_variants
        if not ladder:
            msg = f"没有可用的 cookie 档位，枚举不了 {url}"
            raise MediaDownloadError("ytdlp", "list", msg)

        attempts: list[tuple[str, int, str]] = []
        last: SubprocessResult | None = None
        for variant in ladder:
            args = [*self._base_args(), *variant.args]
            if playlist_items is not None:
                args += ["--playlist-items", f"1:{playlist_items}"]
            args += ["--flat-playlist", "-J", url]
            result = await self._run(args, cwd=None, on_line=None, timeout=timeout)
            last = result
            attempts.append((variant.label, result.returncode, _tail(result.stderr)))
            if result.ok:
                return YtDlpResult(
                    ok=True,
                    variant=variant,
                    stdout=result.stdout,
                    stderr=result.stderr,
                    returncode=result.returncode,
                    attempts=tuple(attempts),
                )
            if not should_escalate_cookie_rung(result.stderr):
                break

        assert last is not None
        return YtDlpResult(
            ok=False,
            variant=ladder[-1],
            stdout=last.stdout,
            stderr=last.stderr,
            returncode=last.returncode,
            attempts=tuple(attempts),
        )

    def _base_args(self) -> list[str]:
        return [
            YTDLP,
            "--no-warnings",
            # --newline：yt-dlp 默认用回车符刷同一行进度，按行读就永远读不到东西，
            # on_line 回调（推 SSE 进度）会形同不存在。
            "--newline",
            "--encoding",
            "utf-8",
            *self._extra_args,
        ]

    async def _run(
        self,
        argv: Sequence[str],
        *,
        cwd: Path | None,
        on_line: ProgressFn | None,
        timeout: float | None,
    ) -> SubprocessResult:
        """跑一次 yt-dlp。

        二进制没装时 `run_subprocess` 抛 `LookupError` —— 这里**原样往上抛**，
        不翻译成 `MediaDownloadError`：两种失败要做的动作不同
        （"装 yt-dlp" vs "重新登录/换 cookie"），糊成一个类型就分不出文案。
        """
        return await run_subprocess(
            list(argv),
            timeout=timeout or self._timeout,
            cwd=cwd if cwd is not None and cwd.is_dir() else None,
            on_stdout_line=on_line,
        )


def _tail(text: str, *, lines: int = 5) -> str:
    return "\n".join(text.splitlines()[-lines:])


def _artifacts_from(stdout: str, dest_dir: Path) -> tuple[Path, ...]:
    """从 yt-dlp 自己报告的行里认文件。

    为什么不用 glob 扫目录：V1 §7.21 的教训 —— 扫 `*.mp4` 会把**我们自己产出的**
    中间产物（`audio/part-001.m4a`）也认成源媒体，一条作品转两遍。
    只认 yt-dlp 报告的路径，是唯一不会认错的办法。
    """
    found: list[Path] = []
    for line in stdout.splitlines():
        candidate = _path_from_line(line.strip())
        if candidate is None:
            continue
        path = Path(candidate)
        found.append(path if path.is_absolute() else dest_dir / candidate)
    return tuple(dict.fromkeys(found))


def _path_from_line(line: str) -> str | None:
    """从一行输出里认出一个媒体文件路径。两种真实句式：

    - `[download] Destination: media.f137.mp4` —— 正在下
    - `[download] media.mp4 has already been downloaded` —— 跳过（重跑采集是常态）

    第二种曾经配的是 `"has already downloaded: "`（尾巴多了个冒号），
    yt-dlp 从不这么写，于是"这条其实已经有了"被当成"什么都没下"。
    症状是**库里有作品行、媒体列表为空**，而排查的人会先去怀疑磁盘。
    """
    marker = "[download] Destination: "
    if line.startswith(marker):
        candidate = line.removeprefix(marker)
        return candidate if candidate.endswith(_MEDIA_SUFFIXES) else None

    prefix, _, tail = line.partition("[download] ")
    if prefix or not tail:
        return None
    for phrase in (" has already been downloaded", " has already downloaded"):
        if tail.endswith(phrase):
            candidate = tail[: -len(phrase)].strip()
            return candidate if candidate.endswith(_MEDIA_SUFFIXES) else None
    return None
