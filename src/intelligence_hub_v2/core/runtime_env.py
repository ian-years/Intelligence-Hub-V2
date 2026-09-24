"""运行环境的 PATH：向注册表要一次真相（V1 §7.19 的正解）。

**为什么会缺**：子进程继承的是**启动方那份环境快照**。装完 ffmpeg 改的是注册表，
不重开启动方（终端 / IDE / 服务宿主）就永远看不见 —— 于是出现"注册表里有、
`shutil.which` 说没有"这种最容易被误判成"没装"的状态。本机 2026-09-24 又撞到一次
（Gyan.FFmpeg 装在 WinGet 目录里，Git Bash 起的进程查不到）。

常驻服务启动时自己去要一次真相，比要求使用者记得重开宿主可靠。

三条规矩，都是从 V1 搬过来的：

1. **只追加，不动既有顺序**。进程 PATH 是启动方刻意设置的那份，注册表版本不能盖过它。
2. **只增不删**。注册表里已被移除的目录继续留在进程 PATH 里 —— 修一个坑不该挖出另一个。
3. **读不到注册表就当没补上**，不能让服务起不来（`winreg` 失败、被组策略挡住、
   非 Windows 全部走同一条路：返回空列表）。

`config.paths.*` 那一组在这里获得它的读者：**显式路径赢**，做法是把那个文件所在的
目录**插到 PATH 最前面**（而不是往每个调用点塞一个"如果配了就用它"的分支 ——
那会把同一件事写四五遍，正是 V1 §7.11 那一族）。指的文件不存在就如实记一条问题，
不静默回落到 PATH：配了路径却悄悄用别人的二进制，是最难查的那种"成功"。
"""

from __future__ import annotations

import os
import shutil
from collections.abc import MutableMapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from intelligence_hub_v2.logging import get_logger

if TYPE_CHECKING:
    from intelligence_hub_v2.core.config import AppConfig

__all__ = [
    "TOOL_COMMANDS",
    "RuntimeEnvReport",
    "apply_runtime_environment",
    "merge_path_entries",
    "prepare_runtime_environment",
    "registry_path_values",
    "sync_path_from_registry",
]

logger = get_logger(__name__)

TOOL_COMMANDS: dict[str, str] = {
    # `PathsSection` 的字段名 → 实际命令名。`test_the_path_knobs_are_the_tools_we_probe`
    # 把这张表与 `PathsSection`、preflight 的探针清单钉在同一个源头。
    "ffmpeg": "ffmpeg",
    "ffprobe": "ffprobe",
    "node": "node",
    "yt_dlp": "yt-dlp",
}

PATH_KEY_SEP = os.pathsep


@dataclass(frozen=True)
class RuntimeEnvReport:
    """一次 `apply_runtime_environment()` 的结果。给启动日志与 preflight 用。"""

    added: tuple[str, ...] = ()
    """从注册表补进来的目录（保持注册表里的先后）。"""

    forced: dict[str, str] = field(default_factory=dict)
    """`config.paths.*` 指的文件 → 被插到 PATH 最前面的那个目录。"""

    problems: tuple[str, ...] = ()
    """配了路径但那个文件不存在 —— 一句话能说清该改配置还是该装东西。"""

    resolved: dict[str, str | None] = field(default_factory=dict)
    """命令名 → 最终解析到的文件（`None` = 还是没有）。**只按本次 env 里那份 PATH 算**，
    不去读 `os.environ`，否则在别的进程/别的调用里答案会不一样。"""

    @property
    def changed(self) -> bool:
        return bool(self.added or self.forced)


def _path_key(entry: str) -> str:
    """比较用的键：Windows 的路径大小写不敏感、斜杠两种都合法、结尾分隔符可有可无。"""
    return entry.strip().replace("/", "\\").rstrip("\\").casefold()


def registry_path_values() -> list[str]:
    """注册表里 Machine + User 两份 PATH 原文（新开进程会看到的顺序）。非 Windows 返回 []。"""
    if os.name != "nt":
        return []
    import winreg  # noqa: PLC0415 - 只有 Windows 走得到这里，别的平台上这个模块根本不存在

    values: list[str] = []
    for hive, sub_key in (
        (
            winreg.HKEY_LOCAL_MACHINE,
            r"SYSTEM\CurrentControlSet\Control\Session Manager\Environment",
        ),
        (winreg.HKEY_CURRENT_USER, "Environment"),
    ):
        try:
            with winreg.OpenKey(hive, sub_key) as key:
                raw, _ = winreg.QueryValueEx(key, "Path")
        except OSError:
            # 键被组策略拿掉、没有权限、值不存在：都只是补不全，不是错误。
            continue
        if raw:
            values.append(str(raw))
    return values


def merge_path_entries(process_path: str, registry_values: list[str]) -> tuple[list[str], str]:
    """把「注册表有、进程 PATH 没有」的目录**追加**到末尾，返回 `(追加项, 新 PATH)`。"""
    parts = str(process_path or "").split(PATH_KEY_SEP)
    seen = {_path_key(part) for part in parts if part.strip()}
    added: list[str] = []
    for blob in registry_values:
        for entry in str(blob).split(PATH_KEY_SEP):
            stripped = entry.strip()
            if not stripped:
                continue  # 空项在 Windows 上表示当前目录，不参与比对也不搬运
            key = _path_key(stripped)
            if key in seen or not Path(stripped).is_dir():
                continue  # 注册表里写着早已被删掉的目录是常态
            seen.add(key)
            added.append(stripped)
    merged = PATH_KEY_SEP.join([*parts, *added]) if parts else PATH_KEY_SEP.join(added)
    return added, merged


def sync_path_from_registry(env: MutableMapping[str, str] | None = None) -> list[str]:
    """用注册表补齐 `env`（默认 `os.environ`）里的 PATH，返回补上的目录。"""
    if os.name != "nt":
        return []
    try:
        registry_values = registry_path_values()
    except Exception as exc:  # noqa: BLE001 - 读不到注册表只是补不全，不能让服务起不来
        logger.warning("runtime_env.registry_unavailable", error=f"{type(exc).__name__}: {exc}")
        return []
    if not registry_values:
        return []
    target: MutableMapping[str, str] = os.environ if env is None else env
    added, merged = merge_path_entries(target.get("PATH", ""), registry_values)
    if added:
        target["PATH"] = merged
    return added


def explicit_path_dirs(
    config: AppConfig, env: MutableMapping[str, str]
) -> tuple[dict[str, str], tuple[str, ...]]:
    """把 `config.paths.*` 指的显式二进制目录插到 PATH 最前面。返回 `(用了哪些, 问题)`。"""
    forced: dict[str, str] = {}
    problems: list[str] = []
    for field_name, command in TOOL_COMMANDS.items():
        raw = getattr(config.paths, field_name)
        if raw is None or not str(raw).strip():
            continue
        path = Path(str(raw)).expanduser()
        if not path.is_file():
            problems.append(
                f"paths.{field_name} 指着一个不存在的文件：{path}"
                f"（改配置，或装上 {command}；不设这一项才会回落 PATH 查找）"
            )
            continue
        directory = str(path.parent.resolve())
        if _path_key(directory) in {
            _path_key(part) for part in env.get("PATH", "").split(PATH_KEY_SEP)
        }:
            forced[command] = directory  # 已经在 PATH 里了，不重复插
            continue
        env["PATH"] = (
            f"{directory}{PATH_KEY_SEP}{env.get('PATH', '')}" if env.get("PATH") else directory
        )
        forced[command] = directory
    return forced, tuple(problems)


def apply_runtime_environment(
    config: AppConfig, env: MutableMapping[str, str] | None = None
) -> RuntimeEnvReport:
    """在**给定的 env** 上做全套：显式路径优先，再补注册表。不碰 `os.environ`。

    拆开成"改哪份 env 可以传"是为了能让测试与 preflight 问一句而不改掉全进程环境
    （改掉真实 `os.environ` 的那一位是 `prepare_runtime_environment()`）。
    """
    target: MutableMapping[str, str] = os.environ if env is None else env
    forced, problems = explicit_path_dirs(config, target)
    added = sync_path_from_registry(target)
    path = target.get("PATH", "")
    resolved = {command: shutil.which(command, path=path) for command in TOOL_COMMANDS.values()}
    return RuntimeEnvReport(added=tuple(added), forced=forced, problems=problems, resolved=resolved)


def prepare_runtime_environment(config: AppConfig) -> RuntimeEnvReport:
    """改 `os.environ` 的那一层：服务启动与每轮 preflight 各跑一次（幂等）。

    为什么 preflight 也要跑：装完 ffmpeg 不必重启服务，"下一轮预检就变绿"才是
    这个函数存在的意义；而它第二次跑几乎什么都做不了（已经在 PATH 里的不会重复加）。
    """
    report = apply_runtime_environment(config)
    if report.changed or report.problems:
        logger.info(
            "runtime_env.prepared",
            added=list(report.added),
            forced=dict(report.forced),
            problems=list(report.problems),
            resolved={k: v for k, v in report.resolved.items() if v},
        )
    return report
