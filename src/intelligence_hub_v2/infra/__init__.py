"""外部世界的边界：子进程、HTTP、二进制、CDP 桥。

依赖方向（`docs/architecture.md`）：`api/ → core/ → platforms/ → infra/ → storage/`。
**只有这一层允许碰进程与网络**。这么划不是洁癖，是为了三条能在别处守不住的纪律：

1. **不许 shell 拼接**。所有外部命令走 `create_subprocess_exec` + argv 列表。
   昵称、标题、URL 都是外部输入，`shell=True` 等于把它们当代码执行
   （与"SQL 一律参数化"是同一条要求的另一个面）。
2. **不许臆造成功**（V1 §1.3）。二进制没装（yt-dlp / ffmpeg / node）时的正确行为是
   **如实失败并把原因交出去**，不是"跳过这一步继续"。所以这里的每个 runner
   都要把 `FileNotFoundError` 翻译成带可执行文件名的错误，而不是让它变成一次
   看起来像网络问题的失败。
3. **适配器不许自己拼 cookie 档 / 自己写 Netscape 文件**。V1 的 B站 与抖音各自实现了
   一份 cookie 阶梯，两处漂移过一次（V1 §7.15：新加的报错原文不在识别表里，
   于是退档不触发、整条判死）。这里收成一份。
"""

from __future__ import annotations

from intelligence_hub_v2.infra.cdp_bridge import BridgeClient, BridgeHealth
from intelligence_hub_v2.infra.cookies import CookieManager
from intelligence_hub_v2.infra.ffmpeg import (
    StreamInfo,
    extract_audio,
    has_audio_stream,
    probe_streams,
)
from intelligence_hub_v2.infra.pacing import RatePacer
from intelligence_hub_v2.infra.subprocess import (
    SubprocessResult,
    SubprocessTimeoutError,
    run_subprocess,
)
from intelligence_hub_v2.infra.ytdlp import (
    YtDlpResult,
    YtDlpRunner,
    looks_like_cookie_failure,
    netscape_file_blocker,
    pick_exported_cookie_file,
    plan_cookie_variants,
    progress_from_ytdlp_line,
)

__all__ = [
    "BridgeClient",
    "BridgeHealth",
    "CookieManager",
    "RatePacer",
    "StreamInfo",
    "SubprocessResult",
    "SubprocessTimeoutError",
    "YtDlpResult",
    "YtDlpRunner",
    "extract_audio",
    "has_audio_stream",
    "looks_like_cookie_failure",
    "netscape_file_blocker",
    "pick_exported_cookie_file",
    "plan_cookie_variants",
    "probe_streams",
    "progress_from_ytdlp_line",
    "run_subprocess",
]
