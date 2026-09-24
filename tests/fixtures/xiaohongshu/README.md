# `tests/fixtures/xiaohongshu/` —— 小红书页面 payload

## 来源：**全部是手工合成的，未与现网核对**

这些 JSON 不是从真页面抓下来的，是**照着 `src/intelligence_hub_v2/platforms/xiaohongshu/`
里那四份页面 JS 的返回形状手工造的**（`listing.PROFILE_PAGE_FUNCTION` /
`listing.COLLECT_AFTER_SCROLL_FUNCTION` / `detail.NOTE_DETAIL_PAGE_FUNCTION` /
`detail.SEARCH_USER_PAGE_FUNCTION` 的 `JSON.stringify(...)` 那一层）。

字段名与取值形状的依据是 V1 `download_xiaohongshu_latest.py` 里**跑通过**的那套解析，
以及实现注释里记的实测结论（`userPageData.basicInfo` 才是博主、`xsecToken` 挂在外层包装、
搜索卡片是一整块多行文本）。域名一律用 `*.xhscdn.com` / `xiaohongshu.com` 的**假路径**，
用例里的字节流全部来自 `httpx.MockTransport`，**没有一条请求真出过本机**。

## 每份文件是什么

| 文件 | 对应页函数 | 钉住的判据 |
|---|---|---|
| `profile_page.json` | PROFILE | 首屏 DOM 那半边（有标题/封面/点赞，**没有** token 与类型）；含一条非笔记 ID 的推荐位、一条 note_id 重复的水合行 |
| `scroll_page.json` | COLLECT_AFTER_SCROLL | 状态树那半边（有 token 与 `note_type`，标题为空）；含一条广告位 |
| `profile_page_user_id_mismatch.json` | PROFILE | 打开 A 主页被重定向到 B（`ok:false` + `user_id_mismatch`） |
| `profile_page_self_reported_other_uid.json` | PROFILE | 页函数那道闸被绕过、`ok:true` 却自报别人的 uid（解析层的第二道闸） |
| `profile_page_login_wall.json` | PROFILE | 登录墙：动作是"去扫码" |
| `profile_page_user_not_found.json` | PROFILE | 查无此人：动作是"改链接"。与上一条**必须是两条不同的红** |
| `profile_page_empty_ok.json` | PROFILE | 零笔记**不是**错误 |
| `detail_video_note.json` | NOTE_DETAIL | 视频笔记（有 `video_url`，`published_at` 是毫秒串） |
| `detail_image_note.json` | NOTE_DETAIL | 图文笔记（4 张原图、无 `video_url`、`note_type=normal`），ADR-0019 的产物形状 |
| `detail_note_id_mismatch.json` | NOTE_DETAIL | 跳到别的笔记（`ok:false` + `note_id_mismatch`） |
| `detail_self_reported_other_id.json` | NOTE_DETAIL | 同样的绕过形状，第二道闸 |
| `detail_login_wall.json` / `detail_empty_note.json` | NOTE_DETAIL | "要去登录" vs "这条本身没内容" |
| `search_candidates.json` | SEARCH_USER | 候选含重复 user_id、无昵称的那一条，`homepage_url` 带门票 |
| `search_login_wall.json` / `search_no_candidates.json` | SEARCH_USER | 搜索页那对可区分结果 |

## 这一族 fixture **挡不住**什么（必须看这段）

1. **挡不住"页面 JS 本身写错了"**。用例喂给解析层的是**我们声明页函数会产出的东西**，
   而不是页函数真的产出的东西。JS 里 `user.userInfo` 与 `user.userPageData.basicInfo`
   取反了、`anchor.href` 写成 `getAttribute("href")`、`xsec_source=pc_search` 那个过滤器
   漏了 —— 这四类错误**一条用例都不会红**。对这几条只有源码文本级的那几条看护
   （`test_the_anchor_contract_survives_in_both_templates` 一族）在守，
   它们钉的是"模板里还写着这句话"，**不是**"页面真的这么干"。
   这正是 V1 §7.21 与 §7.13 那一族"测试与实现同方向错"：fixture 是照实现的形状造的，
   实现照着自己的误解造 fixture，两边一起错、一起绿。
2. **挡不住"小红书改版"**。真页面不再给 `noteDetailMap`、把 `imageList` 挪进别的键、
   换了 `note.type` 的字面量，这些都要**真机跑一次**才看得见
   （`make test-real` 那一条真机烟雾，以及 `tools/refresh_bridge_cookies.py` 起桥手验）。
   本目录里没有任何一条用例能预见它。
3. **挡不住 cookie 与 CDN 的真实行为**。`403 没有 UA/Referer`、失效直链回几百字节错误页 ——
   用例里这些都是**我们自己写的假响应**，只证明"我们的代码会按体积拦"，
   不证明"小红书的 CDN 确实这么回"（那一条来自 V1 实测，V2 未复验）。
4. **不覆盖时间字段的时区正确性**。`published_at: "1717503102000"` 解析出来对不对，
   只看它落在这个进程的本机时区上 —— 换一台不同时区的机器跑，绝对时刻不变但
   `datetime` 的 `tzinfo` 会变，这是 ADR-0016 明说的口径，不是回归。
