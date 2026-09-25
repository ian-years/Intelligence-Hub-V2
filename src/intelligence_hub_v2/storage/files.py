"""文件落盘路径的唯一真源。

契约来源：docs/specs/data-model.md §1（Locked 的目录布局）

```
data/
  intelligence_hub.sqlite3
  manifests/20260922-120000-douyin_collect-<task_id>.json
  media/<platform>/<creator_name>/<video_id>-<safe_title>/
      media.mp4                       # 主媒体（合并后）
      media.f137.mp4 + media.f140.m4a # 或 DASH 未合并分片（B站）
      cover.jpg
      metadata.json
      shots/
        shot-8.5.jpg              # 工坊「截图包」：按分镜时间点从 media.mp4 截的帧
      transcript/
        speech-clean.txt
        segments.json
  cookies/<domain>.txt                # Netscape 格式，yt-dlp 直读（**凭证**）
  cdp-bridge-profile/                 # Chrome 持久化 profile（**凭证**）
  asr-models/                         # SenseVoice 权重（gitignore）
  logs/server.log
  tmp/<task_id>/
```

三条纪律：

1. **`data/` 永不入库**（V1 §1.1 硬约束）。里面有有效会话凭证与真实博主数据，
   按凭证对待：不复制进仓库、不为"方便复现"往 `.gitignore` 加例外。
2. **DB 里存相对 `data/` 的路径**（`rel()`）。整个数据目录可以搬走、备份、换机器；
   存绝对路径的话搬一次库就全废。
3. **路径拼接全部过这里**。V1 的教训是口播稿目录按平台不对称（§7.5）——
   启动器与前端各按自己那套读，改错一处前端就读不到稿子。
   V2 里"口播稿在哪"只有 `transcript_path()` 一个答案。
"""

from __future__ import annotations

import math
import re
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from intelligence_hub_v2.core.config import AppConfig

__all__ = ["FileStorage", "safe_filename", "shot_stem"]

# ---------------------------------------------------------------------------
# 文件名净化（V1 §7.8）
# ---------------------------------------------------------------------------

_WINDOWS_DEVICE_NAME = re.compile(r"^(CON|PRN|AUX|NUL|COM[0-9]|LPT[0-9])(\..*)?$", re.IGNORECASE)
"""Windows 保留设备名。**带扩展名也算**（`CON.txt` 同样打不开）。"""

_ILLEGAL_FILENAME_CHARS = re.compile(r'[/\\?:"<>|\x00-\x1f*]')

# Windows 上还有一批"合法字符但语义特殊"的：结尾的点与空格会被静默剥掉，
# 于是 `foo.` 与 `foo` 变成同一个名字 —— 两个博主的目录会撞在一起。
_TRAILING_JUNK = ". "

DEFAULT_FALLBACK = "unnamed"
"""净化后什么都不剩时用的名字。与 V1 `utils.safe_filename()` 一致，
迁移脚本搬 V1 的目录名时不会因为兜底值不同而凭空多出一层重命名。"""


def safe_filename(name: str, *, max_length: int = 120, fallback: str = DEFAULT_FALLBACK) -> str:
    """把任意外部字符串变成安全、合法的文件/目录名。

    **昵称与标题都是外部输入**。只把 `/` 换掉挡不住：
    - `..`（穿越到上级目录）
    - 结尾的点与空格（Windows 静默剥掉 → 两个不同博主撞成同一个目录）
    - `CON` / `NUL` 等设备名（能建出来但打不开）
    - 控制字符（终端里显示成乱码，日志没法读）
    - 超长（Windows 单个路径段上限 255，加上前缀很容易超）

    顺序有讲究：**先截断再 rstrip**。反过来的话，
    `"a" * 200 + "..."` 截断后结尾正好是点，会留下一个以点结尾的目录名。
    """
    text = _ILLEGAL_FILENAME_CHARS.sub("_", str(name or "").strip())
    text = re.sub(r"\s+", " ", text).strip()
    if max_length > 0 and len(text) > max_length:
        text = text[:max_length]
    text = text.rstrip(_TRAILING_JUNK)
    # 纯点号（"." / ".." / "..."）净化完是空串，走兜底 —— 不特判 ".."，
    # 因为穿越风险来自分隔符，而分隔符上面已经换成 "_" 了。
    if not text:
        return fallback
    if _WINDOWS_DEVICE_NAME.match(text):
        text = f"_{text}"
    return text


_DOMAIN = re.compile(r"^[a-z0-9]([a-z0-9-]*[a-z0-9])?(\.[a-z0-9]([a-z0-9-]*[a-z0-9])?)+$")

SHOT_TIME_DECIMALS = 3
"""截图文件名里秒数保留的位数。

**量化本身是判据的一部分**：分镜时间点在 UI 与引擎之间会来回换算
（`60 / 7 = 8.571428571428571`），不归一的话"同一个时间点"每次都能得到一个新文件名，
`shot_file()` 的单射就只剩纸面意义。1 毫秒比人眼能分辨的帧间隔还小，
归一到这一档不会把两个不同的镜头并成一张。
"""


def shot_stem(at_seconds: float) -> str:
    """`at_seconds` → 文件名中间那一段（`8.5`、`0`、`65`）。

    负数与非有限值（NaN / inf）直接抛：它们到 ffmpeg 那里会变成 `-ss -1` 这种
    **看起来像参数**的字符串（`run_subprocess` 走 `shell=False`，不会被解释成选项，
    但产物会莫名其妙），而且"负的第几秒"本身没有语义 —— 早失败、报清楚，
    比产出一个没人能解释的文件好。
    """
    seconds = float(at_seconds)
    if not math.isfinite(seconds):
        msg = f"截图时间点必须是有限秒数，收到 {at_seconds!r}"
        raise ValueError(msg)
    if seconds < 0:
        msg = f"截图时间点不能是负数，收到 {at_seconds!r}"
        raise ValueError(msg)
    text = f"{seconds:.{SHOT_TIME_DECIMALS}f}".rstrip("0").rstrip(".")
    return text or "0"


# ---------------------------------------------------------------------------
# FileStorage
# ---------------------------------------------------------------------------


class FileStorage:
    """产物路径计算器。**只算路径，不写文件** —— 写入是各任务的事。

    唯一的例外是 `ensure_dirs()`（建骨架目录）与 `tmp_dir(create=True)`：
    这两处的 `mkdir(parents=True, exist_ok=True)` 是幂等的，
    V1 的经验（§4.1）是那棵树不需要人手工建，首次运行自己长出来。
    """

    def __init__(
        self,
        data_dir: Path | str,
        *,
        media_subdir: str = "media",
        manifests_subdir: str = "manifests",
        cookies_subdir: str = "cookies",
        logs_subdir: str = "logs",
        tmp_subdir: str = "tmp",
        bridge_profile_subdir: str = "cdp-bridge-profile",
        asr_models_subdir: str = "asr-models",
    ) -> None:
        self._root = Path(data_dir)
        self._media_subdir = media_subdir
        self._manifests_subdir = manifests_subdir
        self._cookies_subdir = cookies_subdir
        self._logs_subdir = logs_subdir
        self._tmp_subdir = tmp_subdir
        self._bridge_profile_subdir = bridge_profile_subdir
        self._asr_models_subdir = asr_models_subdir

    @classmethod
    def from_config(cls, config: AppConfig, root: Path | None = None) -> FileStorage:
        """从 `AppConfig.data` 装配。

        **分层例外**：`storage/` 在依赖方向的最底层，按规矩不该 import `core.config`。
        这里只在 `TYPE_CHECKING` 下引类型，运行时不产生依赖（`db.py` 同样处理）。
        真要在运行时读配置，用 `resolve_all()` 的产物调 `FileStorage(...)`。
        """
        paths = config.data.resolve_all(root)
        data = config.data
        return cls(
            paths["root"],
            media_subdir=data.media_subdir,
            manifests_subdir=data.manifests_subdir,
            cookies_subdir=data.cookies_subdir,
            logs_subdir=data.logs_subdir,
            tmp_subdir=data.tmp_subdir,
        )

    # ---- 根与子目录 ----

    @property
    def root(self) -> Path:
        return self._root

    @property
    def media_root(self) -> Path:
        return self._root / self._media_subdir

    @property
    def manifests_dir(self) -> Path:
        return self._root / self._manifests_subdir

    @property
    def cookies_dir(self) -> Path:
        """**凭证目录**：里面是有效的会话 cookie（Netscape 格式）。"""
        return self._root / self._cookies_subdir

    @property
    def logs_dir(self) -> Path:
        return self._root / self._logs_subdir

    @property
    def tmp_root(self) -> Path:
        return self._root / self._tmp_subdir

    @property
    def bridge_profile_dir(self) -> Path:
        """CDP 桥的 Chrome 用户数据目录。**同样是凭证**（含登录态）。"""
        return self._root / self._bridge_profile_subdir

    @property
    def asr_models_dir(self) -> Path:
        """SenseVoice 权重目录（本机 233 MB，gitignore）。

        V1 §4.1 的教训：这是新人最容易漏的一项 —— 代码在 git 里，
        权重不在，缺了转写就静默降级成"只下视频"。
        """
        return self._root / self._asr_models_subdir

    def ensure_dirs(self) -> None:
        """建骨架目录（幂等）。启动时调一次。"""
        for directory in (
            self._root,
            self.media_root,
            self.manifests_dir,
            self.cookies_dir,
            self.logs_dir,
            self.tmp_root,
        ):
            directory.mkdir(parents=True, exist_ok=True)

    # ---- 媒体 ----

    def media_dir(self, platform: str, creator_name: str, video_id: str, title: str) -> Path:
        """一条作品的目录：`data/media/<platform>/<creator_name>/<video_id>-<safe_title>/`。

        **`video_id` 前缀是必须的**，不是为了好看：
        两个标题净化后可能撞成同一个名字（`"测试:上"` 与 `"测试_上"`），
        撞了就是"第二条作品的媒体覆盖第一条" —— V1 §7.8 那句自检
        "会不会把已有目录变成第二个同名博主"说的就是这个。
        `platform_video_id` 在平台内唯一，所以带上它就一定不会撞。

        `creator_name` 用昵称而不是 `sec_uid`：V1 实测（§6）目录名是 `姜胡说`
        比 `MS4wLjABAAAA...` 对人友好得多，而唯一性由外层 `platform` + 内层
        `video_id` 保证，昵称重复不会造成数据错乱（只会让目录看起来像同一个博主）。
        """
        safe_creator = safe_filename(creator_name, max_length=60)
        # 标题只占后半段，给 video_id 留出位置。
        safe_title = safe_filename(title, max_length=60)
        leaf = f"{safe_filename(video_id, max_length=64)}-{safe_title}"
        return self.media_root / safe_filename(platform, max_length=32) / safe_creator / leaf

    def media_file(self, platform: str, creator_name: str, video_id: str, title: str) -> Path:
        """主媒体（合并后的 mp4）。"""
        return self.media_dir(platform, creator_name, video_id, title) / "media.mp4"

    def cover_file(self, platform: str, creator_name: str, video_id: str, title: str) -> Path:
        return self.media_dir(platform, creator_name, video_id, title) / "cover.jpg"

    def metadata_file(self, platform: str, creator_name: str, video_id: str, title: str) -> Path:
        """平台原始 metadata（审计用）。

        V1 §7.2 的看护落在这里：`yt_dlp_error`（yt-dlp 的失败原文）与
        `media_source`（这条片实际走了 yt-dlp 还是页面播放直链）都写进这个文件，
        否则"兜底成功了"就等于把失败原因丢了。
        """
        return self.media_dir(platform, creator_name, video_id, title) / "metadata.json"

    def dash_part_file(
        self,
        platform: str,
        creator_name: str,
        video_id: str,
        title: str,
        *,
        format_id: str,
        ext: str,
    ) -> Path:
        """DASH 未合并分片：`media.f137.mp4` / `media.f140.m4a`。

        V1 §7.21 的教训：yt-dlp 合并失败时会留下这种"纯视频轨 + 纯音频轨"，
        而 `rglob("*.mp4")` 会把纯视频流喂给 `ffmpeg -vn`，
        报 `Output file does not contain any stream`（exit -22）——
        **长得和"ffmpeg 没装"一模一样**，方向却完全不同。
        命名与 yt-dlp 自己的产物一致，所以后处理不需要区分是谁写的。
        """
        safe_fmt = safe_filename(format_id, max_length=16)
        safe_ext = safe_filename(ext, max_length=8).lstrip(".") or "mp4"
        return (
            self.media_dir(platform, creator_name, video_id, title)
            / f"media.f{safe_fmt}.{safe_ext}"
        )

    # ---- 分镜截图（工坊页「截图包」）----

    def shots_dir(self, media_dir: Path) -> Path:
        """按分镜时间点从**这条成片**截出来的帧：`<media_dir>/shots/`。

        与 `audio_dir()` 同一个理由：帧是**可再生产物**，不是真相，所以

        - 库里不加列、不做迁移（`videos.media_path` 指的成片才是真相）；
        - 它住在媒体目录**里面**，搬走/删掉一条作品时不会留下孤儿，
          而 `media.py` 出图那两条边界判据（只认 `media_root` 底下 + 扩展名白名单）
          天然罩得住它。

        参数同样是 `media_dir` 而不是 `video_id`：算这一层的唯一输入是"成片在哪"，
        而库里那一列给的正是那个（`resolve_media_file()` 的返回值取其父目录）。
        """
        return media_dir / "shots"

    def shot_file(self, media_dir: Path, at_seconds: float) -> Path:
        """某一秒那一帧：`<media_dir>/shots/shot-<秒>.jpg`。

        **命名对 `at_seconds` 单射且确定**，两件事各挡一类坏：

        - 单射 → 同一秒重截是**覆盖**，不是 `shot-8.jpg` / `shot-8(1).jpg` /
          `shot-8-2.jpg` 那样攒垃圾。少了这条，`extract_frame()` 的"已存在就跳过"
          永远不会命中，每点一次按钮就多一整套帧。
        - 确定性 → 界面刷新后还能认出同一帧（`cached` 那个标志全靠它），
          也不需要为截图另开一张表去记"上次截到哪了"。
        """
        return self.shots_dir(media_dir) / f"shot-{shot_stem(at_seconds)}.jpg"

    # ---- 口播稿 ----

    def transcript_dir(self, media_dir: Path) -> Path:
        return media_dir / "transcript"

    def transcript_path(self, media_dir: Path) -> Path:
        """口播稿正文。**全平台同一条路径**（V1 §7.5 的结构性解决）。

        V1 里启动器/前端按各自平台目录读 `speech-clean.txt`，
        改错一处前端就读不到稿子，而且"顺手统一"被明确禁止（因为两边都有消费者）。
        V2 没有第二套约定：口播稿永远在 `<media_dir>/transcript/speech-clean.txt`。

        参数是 `media_dir` 而不是 `video_id`：稿子与媒体同住一个目录
        （备份/搬走时不会漏），所以只有先算出媒体目录才能算出稿子路径。
        DB 里的 `transcripts.text_path` 存的是这个路径的 `rel()` 形式。
        """
        return self.transcript_dir(media_dir) / "speech-clean.txt"

    def segments_path(self, media_dir: Path) -> Path:
        """切句结果（时间戳 + 文本），给前端逐句高亮用。"""
        return self.transcript_dir(media_dir) / "segments.json"

    def reference_path(self, media_dir: Path) -> Path:
        """本地抽取式参考材料。V1 那个文件叫 `summary.md`，这里换了名字并写清了性质：
        它是**抽取**出来的原文片段，不是摘要生成结果（`tasks/reference.py`）。

        与 `speech-clean.txt` 同住 `transcript/`：两个文件说的是同一份稿子，
        分开放会让人以为它们是两件事。
        """
        return self.transcript_dir(media_dir) / "reference.md"

    def asr_audio_path(self, media_dir: Path) -> Path:
        """ASR 的中间产物：16k 单声道 WAV（`infra/ffmpeg.extract_audio` 的产物）。

        放 `audio/` 而不是 `transcript/`：V1 §7.21 踩过"中间产物被当成源媒体，
        一条作品转两遍"，所以中间产物与成品必须分家（见 `audio_dir`）。
        """
        return self.audio_dir(media_dir) / "speech-16k.wav"

    def audio_dir(self, media_dir: Path) -> Path:
        """抽出来的音频（转写中间产物）。

        与 `transcript/` 分开：V1 §7.21 踩过"自己产出的 `postprocess/audio/part-001.m4a`
        被当成源媒体、一条作品转两遍"，所以中间产物必须在一个
        源媒体扫描**不会**去看的地方。
        """
        return media_dir / "audio"

    # ---- 清单 ----

    def manifest_path(self, started_at: datetime, kind: str, task_id: str) -> Path:
        """`data/manifests/YYYYMMDD-HHMMSS-<kind>-<task_id>.json`。

        时间戳按 **UTC** 格式化（不是本地时间）：文件名要能排序，
        而混合时区（或夏令时切换）会让排序与真实先后不一致。
        V1 用的是本地时间，单机没出问题，但 V2 的清单要进 DB 与
        `written_at`（UTC）对齐，两套时区并存是自找麻烦。

        **`task_id` 在文件名里是必须的，不是可选项。**
        光有"秒 + kind"会撞：两个平台采集任务在同一秒起跑（`all_platforms`
        就是故意让它们并行的），后写的那份 `os.replace` 会**静默覆盖**前一份，
        于是 DB 里有两条索引指向同一个文件，而文件内容是后那一份 ——
        前一个任务的审计凭据凭空消失，没有任何错误。（这条是 2026-09-22
        `test_two_runs_get_two_files_and_two_index_rows` 当场抓出来的。）

        `kind` 与 `task_id` 都过 `safe_filename`：`kind` 现在来自代码内的白名单，
        `task_id` 来自 UUID，但"路径段来自外部输入"这类防线不该等到有人传错才生效。
        """
        stamp = started_at.strftime("%Y%m%d-%H%M%S")
        safe_kind = safe_filename(kind, max_length=48)
        safe_task = safe_filename(task_id, max_length=40)
        return self.manifests_dir / f"{stamp}-{safe_kind}-{safe_task}.json"

    # ---- cookie ----

    def cookies_path(self, domain: str) -> Path:
        """`data/cookies/<domain>.txt`（Netscape 格式，yt-dlp `--cookies` 直读）。

        **这是有效会话凭证**。V1 §7.3 的结论是：Windows 上 yt-dlp 读不了
        Chrome 的 cookie 库（Chrome 开着 → `Could not copy Chrome cookie database`，
        关着 → `Failed to decrypt with DPAPI`），唯一稳定路径就是这个导出文件。

        `domain` 只接受纯主机名。传 URL 会先把 scheme 与路径剥掉，
        剥完不像域名就抛 `ValueError` —— 静默生成一个
        `cookies/https:__www.douyin.com_user_x.txt` 比报错难查得多。
        """
        host = _normalize_domain(domain)
        return self.cookies_dir / f"{host}.txt"

    # ---- 临时与日志 ----

    def tmp_dir(self, task_id: str, *, create: bool = False) -> Path:
        """任务专属临时目录 `data/tmp/<task_id>/`。任务结束时整个删掉（精确路径）。

        `create=True` 时顺手建目录。默认不建：算路径不该有副作用，
        而"顺手 mkdir"会让一个拼错的 task_id 在磁盘上留一个空目录。
        """
        path = self.tmp_root / safe_filename(task_id, max_length=64)
        if create:
            path.mkdir(parents=True, exist_ok=True)
        return path

    def log_file(self, name: str = "server.log") -> Path:
        return self.logs_dir / safe_filename(name, max_length=64)

    # ---- 相对/绝对换算 ----

    def rel(self, path: Path | str) -> str:
        """转成"相对 `data/`、posix 分隔符"的字符串，用于写进 DB。

        不在 `data/` 底下时**回落成绝对路径**而不是抛异常：
        用户把媒体目录挂到别的盘是合法配置，为这个把整轮采集搞挂不值得。
        回落是可识别的（绝对路径带盘符/前导斜杠），读的时候 `abs()` 两种都认。
        """
        target = Path(path)
        try:
            return target.resolve().relative_to(self._root.resolve()).as_posix()
        except (ValueError, OSError):
            return target.as_posix()

    def abs(self, stored: Path | str) -> Path:
        """`rel()` 的逆操作。已经是绝对路径就原样返回。"""
        candidate = Path(stored)
        if candidate.is_absolute():
            return candidate
        return self._root / candidate


def _normalize_domain(domain: str) -> str:
    """从 URL 或主机名里取出纯主机名（小写）。认不出来抛 `ValueError`。"""
    text = str(domain or "").strip()
    if not text:
        msg = "cookies 域名不能为空"
        raise ValueError(msg)
    if "://" in text:
        text = text.split("://", 1)[1]
    # 去掉 path / query / 端口 / 用户名
    text = re.split(r"[/?#@]", text, maxsplit=1)[0]
    text = text.split(":", 1)[0].strip().lower().rstrip(".")
    if not _DOMAIN.match(text):
        msg = f"认不出这是域名: {domain!r}（只接受 douyin.com 这种主机名或完整 URL）"
        raise ValueError(msg)
    return text
