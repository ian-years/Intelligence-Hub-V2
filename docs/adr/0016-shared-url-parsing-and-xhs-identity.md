# ADR-0016: 共用的 URL / 计数解析提到 `platforms/urls.py`，以及小红书的身份词汇

- **状态**：Accepted
- **日期**：2026-09-24
- **决策人**：用户 + Qoder
- **相关**：ADR-0004（平台 Adapter Protocol）、ADR-0006（数据模型）、ADR-0011（cookie 阶梯只留一处真相）、
  `docs/specs/platform-adapter.md`、`platforms/bilibili/urls.py` 那段"第三个平台进来时"的话、
  `docs/plans/v2.1-migration-plan.md` T2.1

## 背景

`platforms/bilibili/urls.py::as_http_url` 的 docstring 里早就写着判据（V2.0 收尾时留下的）：

> 与 `platforms/douyin/urls.py` 里那个**是同一份逻辑**。为什么不合并：两处各十几行的重复比
> 那个流程便宜……**第三个平台进来时（V2.1 小红书）就该通过 ADR 提到公共层** ——
> 三份重复就不再是权衡，而是漂移的开始。

现在第三个平台进来了。今天真实的重复面是两块，不是一块：

| 逻辑 | 现有份数 | 小红书要不要 |
|---|---|---|
| 协议相对地址（`//host/…`）→ `HttpUrl` | B站 一份、抖音一份（**两份行为不同**，见下） | 要（图片直链就是这个形状） |
| 中文计数（"1.2万" / "3.5亿" / "10w+"）→ `int \| None` | 抖音一份（`parse_cn_count`） | 要（点赞 / 收藏 / 粉丝三个字段都用） |

顺带还有一条：`platforms/` 下没有任何地方能放"跨平台但不属于契约"的助手 ——
`base.py` 是 Locked 契约模块，`__init__.py` 是注册表，各平台子包彼此不认识。

## 选项

**A. 放进 `platforms/base.py`。**
那是这份仓库里"V3 不能动"的那一块（Protocol + 契约模型）。把 `parse_cn_count` 这种
纯实现细节塞进契约面，等于让 V3 也必须实现同一个中文计数解析器。否。

**B. 放进 `core/` 或 `infra/`。**
`core/` 在依赖图**上方**（`platforms/ → core/` 是单向边，反过来就是循环导入 ——
`tests/unit/test_import_layers.py` 守的就是这个）；`infra/` 是外部世界的客户端层，
让它认识 Pydantic 的 `HttpUrl` 类型是反向依赖。

**C. 新建 `platforms/urls.py`，实现层模块，不在契约清单里。** ← 选定

## 决定

1. 新增 `src/intelligence_hub_v2/platforms/urls.py`，只放**不需要知道是哪个平台、也不碰网络**的东西。
   今天搬进去两样：`absolute_http_url`（协议相对补 `https:`）与 `parse_cn_count`。
2. **抖音与 B站 的模块名一个都不改**：`douyin/urls.py` 与 `bilibili/urls.py`
   继续导出 `parse_cn_count` / `as_http_url` / `require_http_url`。
   改的是实现位置，不是调用点 —— 那两个包各有 4~6 处调用、`tests/contracts/` 另有十几处，
   把它们全改成新名字不属于这一格任务的收益。转发那一行留一句"实现在公共层"的注释。
3. **不顺手统一抖音那份 `as_http_url`**。它对 `//p3.douyinpic.com/…` 返回 None，
   而 B站 那份会补成 `https:` —— 两份行为**不同**。把抖音改成"补协议"会让以前被丢弃的
   头像/封面开始入库，那是一次真实行为变更，得拿抖音自己的用例当证据另做。
   公共层里那份是 B站 语义（也是小红书要的），差异写在 docstring 里并明确
   **"这不是待修的 bug 的默认"**。留一条欠账：ADR-0017 或 T2.2 里连带决定。
4. 同一条 ADR 定下**小红书的身份词汇**（它决定库里存什么，晚定就要搬数据）：

   | 概念 | 存哪 | 不是身份的东西 |
   |---|---|---|
   | 一条笔记 | `videos.platform_video_id = note_id`（`[0-9a-zA-Z]{18,}`） | **`xsec_token` 不是身份** —— 它是取详情的门票，同一条笔记换个 token 还是同一条，所以只在拼详情 URL 那一刻用，不进库 |
   | 一个博主 | `creators.platform_id = user_id`（主页路径段，24 位 hex 常见但不设长度去猜） | 分享短链 `xhslink.com/a/xxxx` 不含身份，要先跟 302（那是适配器的事） |
   | 发布时间 | `videos.published_at`，**aware datetime** | 页面给的裸时间戳与裸日期串按**本机时区**解释，与 `tools/migrate_from_v1.py` 同一口径；不假装它是没有来源的 UTC |

   为什么"发布时间"要写进 ADR：V1 一路传的是 `"%Y-%m-%d %H:%M:%S"` 这种无时区文本，
   照搬进 V2 的 `UTCDateTime` 列不会当场报错，报错点在很久之后那句
   `datetime.now(UTC) - row.published_at`。所以 `xiaohongshu/urls.py` 的契约是
   **要么 None、要么带时区**，并有用例按"关系"钉住（不是逐样本比字符串）。

## 后果

- `platforms/urls.py` 是**实现层**：不在 `AGENTS.md §2` 的契约清单里，V3 可以整个换掉。
- 新平台进来时默认从公共层拿这两样，不再抄第三份。
- `note_id` 用的是 `{18,}` 而不是定长：现网既有 24 位也有更长的。**代价**是被截断的链接
  可能被认成另一条合法 ID —— 所以详情页那一步必须拿页面自报的 `note_id` 对一次
  （V1 那条"串号就整条丢弃"的判据，落在还没写的 `listing.py` 里，T2.1 第二片）。
- 低界故意不设：`note_id` 前 8 位十六进制换算出来是 1970 年那一秒也照样交回时间。
  小红书 2013 年才有内容，看着像可以判掉，但"下界取哪一年"是我们发明的，
  猜错的后果是一条真笔记的发布时间被判成"没有"（用例把这条决定钉住了）。
- 本 ADR 只覆盖 T2.1 的第一片（纯解析）。**注册平台还要动 17 处**（注册表、yaml、
  `task_registry`、`dispatch` 的 host 后缀与 URL 模板、`single_link` 的 id 提取、
  `tools/refresh_bridge_cookies.py` 的默认域名清单、契约测试基类、以及三处
  "xiaohongshu 现在应当 404"的断言要翻向）—— 清单记在
  `docs/progress/2026-09-24.md` 主题九，那一票留在适配器落地那一次提交里。
