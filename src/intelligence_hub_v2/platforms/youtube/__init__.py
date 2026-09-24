"""YouTube 平台适配器（V2.1 T2.2）。

V1 对应物是 `download_youtube_latest.py`（630 行）。搬进 `PlatformAdapter` 之后
这一包里只剩"YouTube 特有"的那几件事：

| 文件 | 职责 |
| --- | --- |
| `urls.py` | 纯解析：频道 ID / 手柄 / 作品 ID 与规范 URL，**不碰网络** |
| `config.py` | `YouTubeConfig` / `YouTubeAdvanced`（`/api/platforms/youtube/schema` 的模型） |
| `listing.py` | `--flat-playlist -j` 输出的解析 + **`--recent-days` 那条时间窗**（V1 语义） |
| `media.py` | yt-dlp 下载 argv + 字幕轨（vtt）解析 |
| `adapter.py` | 装配成 `PlatformAdapter` |

与 B站 同构的那一半：`needs_browser=False`、`list_strategy="yt_dlp_flat"`、
`media_strategy="yt_dlp"`（都没有"页面播放直链"这种第二条路）。
与 B站 不同的那一半：**不需要任何登录态**（`needs_cookies=False`、
`cookie_variants=("none",)`），所以没有 cookie 阶梯，也因此 `--flat-playlist`
这一路不会撞上 V1 §7.15 那种"随机 352/412"。
"""
