# `tests/fixtures/youtube/` —— YouTube 的接口样本

## `subtitles_en.vtt` 同样是**合成样本**

照 vtt 规范与 **YouTube 自动字幕**的两个真实特征造的：
滚动重复行（上一条的文本原样再出现一次）、内联 `<c>` 与 `<00:00:04.500>` 标签、
`-->` 右侧的 cue settings（`align:start position:0%`）、一条正文占多行。
这些特征由 `intelligence_hub_v2.platforms.youtube.media` 的解析注释提出，
**没有一条来自真抓取**（本机到 YouTube 不通）。
所以它挡不住"yt-dlp 换了 `--sub-format` 的实际产物名/编码"这一类回归。

## `flat_playlist.jsonl` 是**合成样本**，未与真页面 / 真 yt-dlp 输出核对

本会话这台机器到 `www.youtube.com` **不通**，所以没有任何一条 yt-dlp 的真输出被
抓回来过（真机验的那一步记在任务汇报的"真机未验"清单里）。这一份是照
`yt-dlp --flat-playlist -j <频道>/videos` 的**公开字段口径**手工造的：
一行一个 JSON 对象，字段名取自 yt-dlp 的信息字典（`id` / `url` / `type` /
`title` / `duration` / `timestamp` / `upload_date` / `view_count` /
`channel` / `channel_id` / `uploader_id`）。

形状层面的依据是 `docs/specs/platform-adapter.md` 与
`infra/ytdlp.py::FLAT_PLAYLIST_DUMP_FLAG` 那段注释（`-j` = 逐行一条，
`-J` = 整个 playlist 一个对象）—— **命令行与解析器必须共用同一份契约**，
2026-09-24 那条 P0 就是两边各写一遍写漂了。

## 它挡得住什么

- 解析器把"逐行 JSON"当成"整体一个 JSON 对象"（用 `-J` 的输出去喂它会抽不出条目）。
- 非作品条目（`type: playlist`）、空 `id`、缺字段这几种脏输入让整批解析炸掉。
- 时间窗对 `timestamp` / `upload_date` / 两者都缺三种条目的判断。

## 它挡不住什么（**这一节才是重点**）

1. **字段名与取值类型对不上真实现**。如果 yt-dlp 在频道 flat 输出里其实给的是
   `channel_follower_count`、`availability`、或把 `timestamp` 放在别的键下，
   这份 fixture 会让解析器"看起来工作正常"而真机上什么字段都读不出来。
   这是这个仓库踩过一次的"测试与实现同方向错"。
2. **真实输出的噪声形状**。真 yt-dlp 的 stdout 里会混 `[debug]` / `[info]` 行、
   Windows 上可能是 CRLF、非 UTF-8 语言标题会带代理对 —— 这些由用例里的
   手写样本另行覆盖，不来自真抓取。
3. **`--flat-playlist` 到底给不给 `timestamp`**。V1 的实践是"有时给有时不给"
   （所以才有"日期缺失就保留"那条判据），但**具体哪一版、哪一个标签页给**没量过。
   窗口边界行为在真机上可能整体偏向"全部缺失 → 全部保留"，
   那样时间窗就等于不生效，而这条用例不会红。

所以：**真机跑通之后，第一件事是把这一份换成真抓的输出**（并在文件里写明抓取时间与命令）。
