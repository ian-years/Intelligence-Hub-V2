"""跨平台主体匹配与它的三套口径（T4.3）。

V1 源：`Intelligence-Hub/cross_platform_model.py`（399 行）。按计划它标的是"搬运 未适配"，
ADR-0018 决定 1 把这一条改成**按 V2 范式适配**：体量小、全是纯函数、能类型化，
所以住在 `core/` 而不是 `ported/`，受全套门禁（mypy strict / ruff / 覆盖率）。

模块只管三件事，都不碰 IO：

1. **判定** —— `publication_match()` 给两条发布记录打同源分（`MatchEvidence`）。
2. **口径** —— 三套归一函数（标题 / 博主名 / 正文）与 `compact_number_to_int()`
   这种"页面计数 → 整数"的唯一解释。仓库里不许出现第二套。
3. **快照记账** —— `checkpoint_target()` / `snapshot_unique_key()` /
   `work_key_from_video()`。T4.2（指标快照）依赖的就是这一格（见
   `docs/plans/v2.1-migration-plan.md` T4.2 的"依赖: T4.3"），放在这里是因为
   检查点与快照键的**词表**和匹配同属"跨平台主体"这套语义。

**输入是调用方已经解好 join 的记录**（`Publication`）。本模块不连库、不读飞书、
不知道哪张表存什么。V1 用 `left["_creator_names"]` / `["_creator_identity_keys"]` /
`["_content_text"]` 三个下划线"暗键"传这批值，而全仓 grep 得到的事实是：
`publication_match` 在 V1 里**没有任何调用方**（只有定义处那份 `.pyc`）——
那半截契约从来只写在纸上。所以这里把它落成显式字段：调用方（T4.2 / T6.6）
从 `videos` + `video_creators` + `creators` 拼出 `Publication` 再进来。

## 与 V1 故意不一样的四处

1. **时间帧统一到 UTC**。V1 把一切都折成"本机 naive 墙上时钟"，于是 SQLite 读回的
   aware 值与采集来的 naive 值一相减就 `TypeError: can't subtract offset-naive and
   offset-aware datetimes` —— 而且炸点在"跑了很久之后算发布间隔"那里，离原因很远。
   V2 的 `storage/schema.py::UTCDateTime` 已经定了唯一口径（naive 一律按 UTC 解释，
   **不猜本地时区，猜错就是永久性的数据损坏**），这里照同一条：`parse_datetime()`
   与 `publication_match()` 出口都是 aware-UTC。后果是差值与机器所在时区无关，
   用例在任意时区跑都是同一个数。
2. **简繁转换从写入侧挪到匹配侧**。V1 在入库时就转（`local_store.py:396` 那批
   `_to_simplified_zh(...)`），所以它的匹配代码里看不到繁转简 —— 库里已经是简体了。
   V2 的 `videos.title` 存原文（`storage/` 里没有任何转换），所以转换必须住在归一函数
   里，否则 B站 的繁体标题永远比不上同一条内容的简体标题。`zhconv` 在 `media` 这个
   可选 extra 里，缺库时按原文继续（V1 `utils.py:78` 同一处理），**降级不是失败**，
   但会掉匹配质量 —— 所以 `tests/unit/test_identity.py` 里钉了一条"繁简对必须判成
   同一串"的用例：装了才绿，没装是红，不给自己留 skip 这条路（V1 §7.14）。
3. **不放平台词表**。V1 靠 `platform_schema.video_platform()` 认平台，认不出返回空串，
   于是"平台值不认识"和"平台缺失"走同一条 ineligible。V2 的平台名有四份真相
   （`config/platforms.yaml` 的 key、`PLATFORM_CONFIG_SCHEMAS`、`PLATFORMS` 注册表、
   前端列表），在这里再抄一份第五份是 V1 §7.10/§7.11 那一族。所以 `platform` 就是
   `str`，只判"空值"与"两端相同"；**调用方必须传 V2 的 slug**（`douyin` /
   `bilibili` / `xiaohongshu` / `youtube`）。写成别名（`B站` 对 `bilibili`）会被
   判成跨平台，方向上是"多匹配"而不是"漏匹配"，接线时要在 API 边界用 Pydantic 收。
4. **`dict[str, Any]` → 冻结 dataclass**。V1 的两个入参是飞书记录原样，取值全靠
   `.get()` 拼写；类型化之后 `mypy strict` 能把"键名写错"从一次静默的 0 分变成红。

## 没搬进来的那半截（都不是漏）

`CONTENT_WORK_FIELDS` / `VIDEO_MODEL_FIELDS` / `SNAPSHOT_MODEL_FIELDS` 三张飞书字段规格、
`cell_text()` / `link_ids()` / `format_datetime()` 三个飞书单元格工具、
`DOWNLOAD_STRATEGY_*` / `CREATOR_STRATEGY_*` / `MATCH_STATUS_FIELD` 那批字段名常量 ——
全部是"往多维表格写行"的 IO 侧形状，归 T5.1（飞书全家，`ported/`）与 T5.5
（话题/草稿表 + API）管。把它们放进 `core/` 等于让配置层认识飞书。
CLI 部分 V1 这份文件里本来就没有（它是被 import 的库）。

**`compact_number_to_int` 没有「亿」这一档**，这是 V1 的原样行为，不是本模块的缺口：
小红书那侧（`platforms/xiaohongshu/`）的写法是"摘掉亿 → 调这里 → 再乘 1e8"，
`parse_metric_number` 的 V1 用例专门钉了这条"只有亿自己乘"的边界
（`Intelligence-Hub/tests/test_collectors.py::test_only_yi_suffix_is_scaled_locally`）。
"""

from __future__ import annotations

import hashlib
import math
import re
import unicodedata
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from difflib import SequenceMatcher
from typing import Any, Final

__all__ = [
    "CHECKPOINT_DAYS",
    "CHECKPOINT_INITIAL",
    "CHECKPOINT_LIVE",
    "CHECKPOINT_T3",
    "CHECKPOINT_T7",
    "DEFAULT_CHECKPOINT_MAX_LAG_HOURS",
    "MAX_CONTENT_COMPARE_CHARS",
    "MIN_CONTENT_COMPARE_CHARS",
    "PUBLISH_GAP_MAX_HOURS",
    "SNAPSHOT_KEY_PREFIX",
    "MatchEvidence",
    "Publication",
    "checkpoint_target",
    "compact_number_to_int",
    "normalize_content_text",
    "normalize_creator_name",
    "normalize_title",
    "parse_datetime",
    "publication_match",
    "snapshot_unique_key",
    "to_simplified_zh",
    "work_key_from_video",
]

# ---------------------------------------------------------------------------
# 检查点词表（T4.2 要用）
# ---------------------------------------------------------------------------

CHECKPOINT_INITIAL: Final = "初始"
CHECKPOINT_T3: Final = "T+3"
CHECKPOINT_T7: Final = "T+7"
CHECKPOINT_LIVE: Final = "即时"

CHECKPOINT_DAYS: Final[dict[str, int]] = {CHECKPOINT_T3: 3, CHECKPOINT_T7: 7}
"""**可调度**的检查点 → 距发布的天数。

`初始` 与 `即时` 不在这里：前者是"第一次抓到就算"，后者是"每次抓都新开一行"，
两者都没有"发布后第几天"这个目标时刻。`checkpoint_target()` 因此对它们抛，
而不是返回一个看起来能用的数（V1 同一道闸）。
"""

DEFAULT_CHECKPOINT_MAX_LAG_HOURS: Final = 26
"""一次采集还算不算得上某个检查点的延迟预算（26 小时 = 一天 + 一天的余量）。

**今天全仓没有调用方**，V1 那份也没有（实测：只有定义处）。留着是因为 T4.2 的
enrich 判据要用它，而把它写成"到 T4.2 再随手发明一个 26"会丢了这个数的出处。
"""

SNAPSHOT_KEY_PREFIX: Final = "metric"

# ---------------------------------------------------------------------------
# 匹配的三道公共闸
# ---------------------------------------------------------------------------

PUBLISH_GAP_MAX_HOURS: Final = 7 * 24
"""发布间隔超过 7 天就不算同一条内容。

V1 里这个数字出现在三处（硬闸、`time_score` 的衰减分母、`timely` 的重述），
它们必须是同一个数 —— 否则会出现"已经判成不 eligible，分数却还在按别的窗口算"。
"""

MIN_CONTENT_COMPARE_CHARS: Final = 40
"""归一后的正文短于这个字数就不产生证据。

一句"赚钱干货分享"式的摘要，什么内容都能对上 0.9 —— 短文本的相似度没有信息量。
返回 `None`（缺证据）而不是 0（否证）是关键的差别：0 会把总分拉下来。
"""

MAX_CONTENT_COMPARE_CHARS: Final = 6000
"""正文参与比对的上限。`SequenceMatcher` 是 O(n·m)，整篇口播稿进来会跑不完。"""

_EPOCH_MILLIS_CUTOFF: Final = 10_000_000_000
"""大于它按毫秒解释。V1 同一个数；写成常量是因为 `PLR2004` 不接受裸数字。"""

_WORK_KEY_PREFIX: Final = "work_"
_WORK_KEY_DIGEST_CHARS: Final = 16

# ---------------------------------------------------------------------------
# 归一
# ---------------------------------------------------------------------------

_TITLE_PLATFORM_SUFFIX_RE = re.compile(
    r"\s*-\s*(抖音|哔哩哔哩|bilibili|小红书|youtube)\s*$",
    flags=re.IGNORECASE,
)
"""标题尾巴上的平台名。抓来的标题常是 `xxx - YouTube` 这种，不去掉就永远比不上
另一平台那条干净标题。**必须在丢掉非字母数字之前剥**：过滤之后 " - 抖音" 已经和
正文粘成一串，没有任何边界可依。三套归一里只有标题表带这一项。
"""

_HASHTAG_RE = re.compile(r"#[^#\s]+")
r"""话题标签：同一期内容在不同平台的 tag 策略不同，留着等于制造差异。

V1 那个 `[^#\s]+` 的字符类遇到连续标签（`#A#B`）是从左往右一段一段吃，实测两段
都去得掉，与更宽松的 `#\S*` 在这里等价；照抄原样只为不引入第二个口径。
"""

_CREATOR_SUFFIX_RE = re.compile(r"(?:官方|聊赚钱|的抖音|的小红书|b站)$")
"""博主名尾巴。V1 原样保留。

`聊赚钱` 那一项看着像对某一批账号的过拟合，但它的分量是实测出来的：
「张三聊赚钱」对「张三」在去掉这条规则后只剩 **0.5714**，而五条自动匹配路里
creator 的下限最低是 0.72 —— 这一个后缀直接决定"自动合并"还是"待人工确认"。
"""


def to_simplified_zh(text: str) -> str:
    """繁体 → 简体。`zhconv` 不在场就原样返回（V1 `utils.py:78` 同一处理）。

    放在这里而不是入库时，理由见模块 docstring 第 2 条。
    """
    if not text:
        return ""
    try:
        import zhconv  # noqa: PLC0415 - `media` 是可选 extra，顶层 import 会让没装它的环境连导入模块都炸
    except ImportError:
        return text
    return str(zhconv.convert(text, "zh-hans"))


def _keep_alnum(text: str) -> str:
    """只留字母数字（含中日韩字），其余一律丢掉。

    用 `str.isalnum()` 而不是正则字符集：繁转简之后仍可能有全角拉丁字母、emoji、
    带圈数字这些杂七杂八的字符，`isalnum()` 的判据跟 NFKC 之后的 Unicode 类别一致，
    而手写 `[a-z0-9\u4e00-\u9fff]` 会把 CJK 扩展区的字（`㙆`）当噪音吃掉。
    """
    return "".join(char for char in text if char.isalnum())


def normalize_title(value: str) -> str:
    """标题归一：NFKC → 小写 → 去平台尾巴 → 去话题标签 → 只留字母数字。

    NFKC 是这里"空白归一"的那一半：全角空格 U+3000、全角数字 `１２３`、
    非断行空格都会被打回半角，之后的 `isalnum()` 过滤才有统一的输入。
    """
    text = to_simplified_zh(unicodedata.normalize("NFKC", str(value or "")).lower())
    text = _TITLE_PLATFORM_SUFFIX_RE.sub("", text)
    text = _HASHTAG_RE.sub("", text)
    return _keep_alnum(text)


def normalize_creator_name(value: str) -> str:
    """博主名归一：NFKC → 小写 → 去平台/口播尾巴 → 只留字母数字。**不去话题标签**。

    与 `normalize_title()` 不同三件事：没有平台尾巴那一步（账号名不会自报平台），
    尾巴表换成了 `_CREATOR_SUFFIX_RE`，顺序也不能对齐 —— 三套归一不是一个函数的
    三个参数，别顺手合并。
    """
    text = to_simplified_zh(unicodedata.normalize("NFKC", str(value or "")).lower())
    text = _CREATOR_SUFFIX_RE.sub("", text)
    return _keep_alnum(text)


def normalize_content_text(value: str) -> str:
    """正文/口播稿归一：先去 URL 再去话题标签，最后截到 `MAX_CONTENT_COMPARE_CHARS`。

    比标题多去 URL 这一步：搬运到小红书/B站 时链接会换一条，而正文其余部分照抄。
    截断在归一之后：上限管的是"喂给 `SequenceMatcher` 的量"，不是"存多少字"。
    """
    text = to_simplified_zh(unicodedata.normalize("NFKC", str(value or "")).lower())
    text = re.sub(r"https?://\S+", "", text)
    text = _HASHTAG_RE.sub("", text)
    return _keep_alnum(text)[:MAX_CONTENT_COMPARE_CHARS]


# ---------------------------------------------------------------------------
# 计数口径
# ---------------------------------------------------------------------------

_COMPACT_NUMBER_RE = re.compile(r"([0-9]+(?:\.[0-9]+)?)\s*([万wWkK]?)\+?")

_COMPACT_MULTIPLIERS: Final[dict[str, int]] = {"万": 10_000, "w": 10_000, "k": 1_000}
"""`万`/`w`/`W` 一档、`k`/`K` 一档。表里没有的后缀（含空串）乘 1。"""


def compact_number_to_int(value: object) -> int | None:
    """页面计数（`1.2万` / `12w` / `1,234` / `3k+`）→ 整数。

    全仓唯一口径，V1 同名函数的判据逐条保留：

    - `None` / 空串 / `-` / `--` → `None`：这是"没有这个指标"。
    - **脏值抛 `ValueError`，不返回 0、也不返回 `None`**：把"点赞失败"读成 0
      是最难发现的一种数据损坏（AGENTS.md §1.3）。采集侧要降级成 `None`
      的话由调用方 try/except（V1 小红书的 `parse_metric_number` 就是这么做的）。
    - `bool` 单独挡掉：`True` 是 `int` 的子类，不挡会静默变成 1。
    - 负数与非有限值（NaN/inf）抛：计数没有负数，出现即说明上游算错了。
    - 数字一律 `round(float(x))`：浮点入参的小数取舍走 `round` 的
      **banker's rounding**（`2.5 → 2`、`3.5 → 4`），V1 同一条，别改成 floor。
    - **没有「亿」这一档**，`3亿` 会抛。亿由调用方摘（模块 docstring 末段）。
    """
    if value is None:
        return None
    if isinstance(value, bool):
        # TRY004 想在这里看到 TypeError：不对。`True` 在类型上**就是** int，
        # 要报的是"这个值不属于计数域"，与 `1e400` 那条同一族，所以同一异常类型。
        msg = f"Boolean is not a metric count: {value!r}"
        raise ValueError(msg)  # noqa: TRY004
    if isinstance(value, (int, float)):
        number = float(value)
        if not math.isfinite(number) or number < 0:
            msg = f"Invalid metric count: {value!r}"
            raise ValueError(msg)
        return round(number)
    text = unicodedata.normalize("NFKC", str(value)).strip().replace(",", "")
    if not text or text in {"-", "--"}:
        return None
    match = _COMPACT_NUMBER_RE.fullmatch(text)
    if match is None:
        msg = f"Unsupported compact metric count: {value!r}"
        raise ValueError(msg)
    base = float(match.group(1))
    multiplier = _COMPACT_MULTIPLIERS.get(match.group(2).lower(), 1)
    return round(base * multiplier)


# ---------------------------------------------------------------------------
# 时间
# ---------------------------------------------------------------------------


def parse_datetime(value: object) -> datetime | None:
    """把库里/页面上那些形状的时间收成 aware-UTC；**认不出抛 `ValueError`**。

    与 `platforms/xiaohongshu/urls.py::parse_publish_time()` 是两个方向，别合并：
    那一份在采集边界上，认不出要返回 `None`（一条脏时间不该炸掉整轮）；
    这一份在记账边界上，认不出必须响 —— 静默 `None` 会让"发布时间"这一列
    在清单里显示成"没有"，而真正的原因是数据坏了（AGENTS.md §1.3）。
    两份各自存在不是依赖方向的问题（`core/` 引得到 `platforms/`），是契约不同：
    合到"返回 None"就丢掉了响，合到"抛"就会让一条脏时间炸掉一整轮采集。

    吃的形状（V1 同一批）：`datetime` 本身、epoch 秒/毫秒（int 或 float）、
    ISO 文本（`T` 分隔与 `Z` 后缀都认）、`%Y-%m-%d %H:%M:%S` / `%Y-%m-%d %H:%M` /
    `%Y-%m-%d`。`None` 与空串是"没有"，返回 `None`。

    naive 按 UTC 解释（`UTCDateTime` 同一判据），aware 折到 UTC。
    """
    if value is None:
        return None
    if isinstance(value, datetime):
        return _as_utc(value)
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        stamp = float(value)
        if stamp > _EPOCH_MILLIS_CUTOFF:  # 毫秒
            stamp /= 1000
        return datetime.fromtimestamp(stamp, tz=UTC)
    text = str(value or "").strip()
    if not text:
        return None
    text = text.replace("T", " ").replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
            try:
                # DTZ007 想看到 `%z`：裸日期本来就没有时区，出口处 `_as_utc()` 按 UTC 贴上，
                # 与 `UTCDateTime.process_bind_param` 同一判据（不猜本机时区）。
                parsed = datetime.strptime(text, fmt)  # noqa: DTZ007
                break
            except ValueError:
                continue
        else:
            msg = f"Unsupported datetime value: {value!r}"
            raise ValueError(msg) from None
    return _as_utc(parsed)


def _as_utc(value: datetime) -> datetime:
    """任何 datetime → aware-UTC。naive 按 UTC 解释，不猜本机时区。"""
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _publish_delta_hours(left: datetime | None, right: datetime | None) -> float | None:
    """两条发布时间的间隔（小时）。任一侧缺失返回 `None`。

    先各自 `_as_utc()` 再相减 —— 这就是 V1 那处会 `TypeError` 的地方：
    SQLite 读回的是 aware，页面抓来的常是 naive，混着减等于把整个任务炸在
    "算时长"这一步。
    """
    if left is None or right is None:
        return None
    seconds = abs((_as_utc(left) - _as_utc(right)).total_seconds())
    return seconds / 3600


# ---------------------------------------------------------------------------
# 快照与作品键
# ---------------------------------------------------------------------------


def checkpoint_target(published_at: datetime, checkpoint: str) -> datetime:
    """某检查点的目标时刻 = 发布时间 + N 天。

    只接受 `CHECKPOINT_DAYS` 里的检查点。`初始`/`即时`/拼错的名字一律抛：
    返回 `published_at` 本身（"看起来合理"的那个值）会让 T4.2 把"没有目标时刻"
    和"目标时刻就是发布那一刻"混成一件事。
    """
    days = CHECKPOINT_DAYS.get(checkpoint)
    if days is None:
        msg = f"Unsupported scheduled checkpoint: {checkpoint}"
        raise ValueError(msg)
    return published_at + timedelta(days=days)


def snapshot_unique_key(
    video_record_id: str, checkpoint: str, captured_at: datetime | None = None
) -> str:
    """快照唯一键。`即时` 按**小时**分桶，其余检查点一条内容一个键。

    `T+3` 的键不含采集时间，正是它让"重跑 enrich 幂等"成立：同一检查点抓十次
    落同一行，而不是十行快照把指标趋势淹掉。`即时` 反过来必须含小时，
    否则第二次采集会覆盖第一次（"这一小时涨了多少"就没了）。

    `captured_at` 只在 `即时` 下必填，缺了抛而不是用 `now()` —— 键里塞进
    进程的当前时间等于让同一批数据每次跑出不同的一行。
    分桶用 UTC 小时（与 `parse_datetime()` 同一个帧），任何一侧换成 localtime
    都会让同一天的桶整体漂移。
    """
    if checkpoint == CHECKPOINT_LIVE:
        if captured_at is None:
            msg = "captured_at is required for live snapshot keys"
            raise ValueError(msg)
        hour_bucket = _as_utc(captured_at).strftime("%Y%m%d%H")
        return f"{SNAPSHOT_KEY_PREFIX}:{video_record_id}:{checkpoint}:{hour_bucket}"
    return f"{SNAPSHOT_KEY_PREFIX}:{video_record_id}:{checkpoint}"


def work_key_from_video(video_record_id: str) -> str:
    """由视频记录号派生"内容作品"键（`work_<16 hex>`）。

    哈希而不是直接用记录号：四个平台的 ID 形状完全不同（`BV…`、纯数字长串、
    24 位 hex、11 位字母数字），作品键要能被跨平台合并，就不能长得像某一个来源平台。

    `usedforsecurity=False` 不是敷衍 ruff：这里要的是**稳定指纹**，不是密码学强度，
    明写出来顺便挡住"以后有人拿它当校验和/当令牌"的误读。截 16 位 = 64 bit，
    按生日界要到 2^32 条（四十亿）才开始大概率撞一次，本机单库差着好几个数量级。
    """
    digest = hashlib.sha1(str(video_record_id).encode("utf-8"), usedforsecurity=False).hexdigest()
    return f"{_WORK_KEY_PREFIX}{digest[:_WORK_KEY_DIGEST_CHARS]}"


# ---------------------------------------------------------------------------
# 判定
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Publication:
    """一条"某平台上的一次发布"，join 已经解好。

    八个字段与 V1 那两条记录里被读到的键一一对应：

    ====================  =============================================
    V2 字段               V1 读的键
    ====================  =============================================
    `platform`            `ps.video_platform(row)`
    `title`               `视频标题`
    `published_at`        `发布时间`（经 `parse_datetime`）
    `duration_seconds`    `时长秒`
    `creator_record_ids`  `关联博主`（经 `link_ids`）
    `creator_identity_keys` `_creator_identity_keys`（暗键）
    `creator_names`       `_creator_names`（暗键）
    `content_text`        `_content_text`（暗键）
    ====================  =============================================

    三个集合/序列字段允许"什么都不知道"（空），**不允许用空串占位**：
    两侧都放了 `""` 的集合会相交，于是两条毫无关系的记录被认成同一主体。
    `_non_blank()` 兜掉了这一类，但那是兜底不是许可。
    """

    platform: str
    title: str
    published_at: datetime | None = None
    duration_seconds: float | None = None
    creator_record_ids: frozenset[str] = frozenset()
    creator_identity_keys: frozenset[str] = frozenset()
    creator_names: tuple[str, ...] = ()
    content_text: str = ""


@dataclass(frozen=True)
class MatchEvidence:
    """两条发布记录的同源判定结果。字段顺序与含义与 V1 一致（V3 的契约）。"""

    eligible: bool
    """这对记录**能不能**拿来判（同平台/缺标题/超 7 天都是 False）。"""

    auto_match: bool
    """是否自动判成同一条内容。False 而 eligible 为 True 就是"待人工确认"。"""

    score: float
    """加权和，只用于排序与展示 —— **不参与判定**（V1 同一形状，别拿它当阈值用）。"""

    title_score: float
    creator_score: float
    creator_identity_match: bool
    content_score: float | None
    duration_score: float | None
    publish_delta_hours: float | None
    reason: str
    """ineligible 时是短码（`same_or_missing_platform` / `missing_title` /
    `publish_gap_over_7_days`）；eligible 时是 `k=v;...` 的审计串。人看的字段。
    """

    def to_dict(self) -> dict[str, Any]:
        """给清单 / 事件 payload 用（两者都是 `dict[str, Any]`）。"""
        return asdict(self)


# 五条自动匹配路的阈值：**全部是 V1 原值，一格没动**。
# `tests/unit/test_identity.py::_v1_route_verdict` 里用字面量又抄了一份当预言，
# 所以动这里任何一个数都会立刻红在那条用例上。这个双写是刻意的：阈值是全套逻辑里
# 最容易被"顺手调一格"的东西，而它决定的是要不要**自动**合并两个博主的数据。
_EXACT_TITLE_MIN_CHARS: Final = 8
"""完全相同也要够长：`local_store.py:398` 那个占位标题「精选自媒体作品」是 7 个字，
归一后正好被这道门挡住。短通用标题（「新手入门」4 字）同理。
"""

_STRONG_TITLE_MIN_CHARS: Final = 12
_STRONG_TITLE_MIN_SIMILARITY: Final = 0.96
_SAME_CREATOR_MIN_CREATOR: Final = 0.82
_SAME_CREATOR_MIN_TITLE: Final = 0.84
_DURATION_MIN_CREATOR: Final = 0.72
_DURATION_MIN_TITLE: Final = 0.74
_DURATION_MIN_SIMILARITY: Final = 0.9
_CONTENT_MIN_STRONG: Final = 0.9
_CONTENT_MIN_TITLE_FOR_STRONG: Final = 0.4
_CONTENT_MIN_WEAK: Final = 0.72
_CONTENT_MIN_TITLE_FOR_WEAK: Final = 0.55


def _non_blank(values: frozenset[str]) -> frozenset[str]:
    """去掉纯空白成员。见 `Publication` docstring 里那个"空串占位"的坑。"""
    return frozenset(value for value in values if value.strip())


def _best_similarity(left: tuple[str, ...], right: tuple[str, ...]) -> float:
    """两组名字里**最像的那一对**的相似度；两组里找不到非空对则 0.0。

    `max` 而不是 `mean`/`first`：一个博主可能有多个显示名（改名、带前缀），
    只要有一个对上了就是同一个人，别的名字不像不构成反证。
    """
    return max(
        (
            SequenceMatcher(None, a, b).ratio()
            for a in left
            for b in right
            if a and b  # 归一后为空（纯符号昵称）不算证据
        ),
        default=0.0,
    )


def _content_similarity(left: str, right: str) -> float | None:
    """正文相似度；任一侧归一后不足 `MIN_CONTENT_COMPARE_CHARS` 返回 `None`。"""
    left_text = normalize_content_text(left)
    right_text = normalize_content_text(right)
    if len(left_text) < MIN_CONTENT_COMPARE_CHARS or len(right_text) < MIN_CONTENT_COMPARE_CHARS:
        return None
    return SequenceMatcher(None, left_text, right_text).ratio()


def _duration_similarity(left: float | None, right: float | None) -> float | None:
    """时长一致度 `1 - |Δ|/max`；缺任一侧，或两边都不大于 0，返回 `None`。

    两条都是 0 秒**不给满分**：0 在这里是"没抓到时长"，不是"两条一样长"。
    给满分等于用缺失值加分，那正是 AGENTS.md §1.3 要挡的形状。
    """
    if left is None or right is None:
        return None
    longer = max(float(left), float(right))
    if longer <= 0:
        return None
    return 1 - abs(float(left) - float(right)) / longer


def _time_similarity(delta_hours: float | None) -> float:
    """时间接近度。缺发布时间给 **0.5 半分**：既不加分也不否决。

    为什么允许"缺时间"这条路走下去：`videos.published_at` 是可空列
    （`storage/schema.py:204`），V1 迁移来的老数据与只抓到列表页的记录都可能没有，
    把它判成 0 分等于让"缺一个字段"变成"反证"；而 7 天硬闸在间隔未知时
    本来就不可能触发，所以这一路仍可只凭标题 + 主体标识自动匹配。
    """
    if delta_hours is None:
        return 0.5
    return max(0.0, 1 - delta_hours / PUBLISH_GAP_MAX_HOURS)


def _score_parts(
    *,
    title_score: float,
    creator_score: float,
    time_score: float,
    content_score: float | None,
    duration_score: float | None,
) -> tuple[tuple[float, float], ...]:
    """四套权重，按"证据有多厚"换表。每套的权重和都是 1.0。

    为什么换表而不是一套固定权重：正文与时长是**稀缺**证据（只有转过稿的
    内容才有），把它们以 0.25/0.1 混进默认权重的话，缺证据的那一大批记录
    等于被白扣 0.35 分 —— 而 `score` 是要拿出来排序的。
    四套表与和为 1 这条不变量由
    `tests/unit/test_identity.py::test_score_is_a_weighted_mean_of_its_own_parts`
    逐路核。
    """
    if content_score is not None and duration_score is not None:
        return (
            (title_score, 0.35),
            (content_score, 0.25),
            (creator_score, 0.2),
            (time_score, 0.1),
            (duration_score, 0.1),
        )
    if content_score is not None:
        return (
            (title_score, 0.45),
            (content_score, 0.25),
            (creator_score, 0.2),
            (time_score, 0.1),
        )
    if duration_score is not None:
        return (
            (title_score, 0.5),
            (creator_score, 0.2),
            (time_score, 0.1),
            (duration_score, 0.2),
        )
    return ((title_score, 0.6), (creator_score, 0.25), (time_score, 0.15))


def _audit_reason(
    *,
    title_score: float,
    creator_score: float,
    creator_identity_match: bool,
    content_score: float | None,
    duration_score: float | None,
    publish_delta_hours: float | None,
) -> str:
    """`k=v;...` 的审计串。缺失项写字面 `n/a`，与 0 分区分开。"""

    def show(value: float | None) -> str:
        return "n/a" if value is None else str(value)

    return (
        f"title={title_score:.3f};creator={creator_score:.3f};"
        f"creator_identity_match={creator_identity_match};"
        f"content={show(content_score)};"
        f"duration={show(duration_score)};"
        f"publish_delta_hours={show(publish_delta_hours)}"
    )


def publication_match(left: Publication, right: Publication) -> MatchEvidence:
    """判定两条发布记录是否同一条内容。纯函数：不查库、不认平台名、不看时间现在几点。

    判定语义逐条对齐 V1 `publication_match()`，四道闸 + 五条自动匹配路：

    **闸（顺序即优先级，V1 一致）**

    1. 平台缺失或两端相同 → `same_or_missing_platform`。放在最前是因为它最便宜，
       且"同平台的两条"是采集去重要判的事，不是这里要判的事。
    2. 任一侧标题归一后为空 → `missing_title`。
    3. 发布间隔 > 7 天 → `publish_gap_over_7_days`。
       注意它排在 `title_score` 之后：`MatchEvidence` 里仍带着已经算出的标题分，
       人工回看时要看得见"差 8 天但标题一模一样"这种情形。
    4. `auto_match` 必须 `creator_identity_match` —— **没有共同的博主记录或共同的
       跨平台主体标识，就永远不自动合并**，标题再像也只是"待人工确认"。
       这一条是整个模块最硬的判据：合并错了会把两个博主的数据搅在一起，
       而 V1 的 `CREATOR_IDENTITY_FIELD` 说明写得很清楚"留空时不自动假定账号属于同一主体"。

    **五条自动匹配路**（`creator_identity_match` 成立且 7 天内，任一成立即自动匹配）

    - `exact`：归一后完全相同且长度 ≥ 8。长度门是给"精选自媒体作品"这类占位标题
      与短通用标题留的（V1 原值；`_keep_alnum` 之后 8 个字符已经很长了）。
    - `same_creator`：`creator ≥ 0.82` 且 `title ≥ 0.84`。
    - `duration`：`creator ≥ 0.72` 且 `title ≥ 0.74` 且 `duration ≥ 0.9` ——
      标题被平台改动较多时，靠"长度几乎一致"补证据。
    - `strong_title`：`title ≥ 0.96` 且两侧归一长度都 ≥ 12。
    - `content`：`content ≥ 0.9` 且 `title ≥ 0.4`，或 `content ≥ 0.72` 且
      `title ≥ 0.55` —— 口播稿对上基本就是同一条，标题只需弱证据。

    **实测出来的两路冗余（V1 的公式如此，本模块不"顺手修"）**：`auto_match` 要求
    `creator_identity_match`，而它成立时 `creator_score` 恒等于 1.0 ≥ 0.82，
    于是 `exact`（title=1.0）与 `strong_title`（title≥0.96）都被 `same_creator`
    完全覆盖 —— 这两路今天不改变任何判定结果。真正独立的只有 `same_creator` /
    `duration` / `content` 三路（后两路补的是 title 掉到 0.84 以下的区间）。
    留着它们是因为这两路的长度门是**独立于 creator** 的证据；把 creator 从
    "identity 命中即 1.0"改成 V1 之外的任何算法时，它们立刻开始起作用。
    `tests/unit/test_identity.py::test_exact_and_strong_routes_are_covered_by_same_creator`
    把这条覆盖关系钉住了（钉住冗余，而不是假装它不存在）。

    阈值全部是 V1 原值，动任何一格都要走 ADR 并同步
    `tests/unit/test_identity.py` 里那份 V1 基线预言。

    **`score` 不参与判定**：它只用于排序与展示。一条路都不成立但分数很高的情况
    是设计内的（比如标题 1.0 但缺主体标识）。
    """
    left_platform = str(left.platform or "").strip()
    right_platform = str(right.platform or "").strip()
    if not left_platform or not right_platform or left_platform == right_platform:
        return _ineligible("same_or_missing_platform")

    left_title = normalize_title(left.title)
    right_title = normalize_title(right.title)
    if not left_title or not right_title:
        return _ineligible("missing_title")

    title_score = SequenceMatcher(None, left_title, right_title).ratio()
    creator_identity_match = bool(
        _non_blank(left.creator_record_ids) & _non_blank(right.creator_record_ids)
    ) or bool(_non_blank(left.creator_identity_keys) & _non_blank(right.creator_identity_keys))
    creator_score = (
        1.0
        if creator_identity_match
        else _best_similarity(
            tuple(normalize_creator_name(name) for name in left.creator_names),
            tuple(normalize_creator_name(name) for name in right.creator_names),
        )
    )

    delta_hours = _publish_delta_hours(left.published_at, right.published_at)
    if delta_hours is not None and delta_hours > PUBLISH_GAP_MAX_HOURS:
        return MatchEvidence(
            eligible=False,
            auto_match=False,
            score=0.0,
            title_score=title_score,
            creator_score=creator_score,
            creator_identity_match=creator_identity_match,
            content_score=None,
            duration_score=None,
            publish_delta_hours=delta_hours,
            reason="publish_gap_over_7_days",
        )

    content_score = _content_similarity(left.content_text, right.content_text)
    duration_score = _duration_similarity(left.duration_seconds, right.duration_seconds)
    time_score = _time_similarity(delta_hours)
    score_parts = _score_parts(
        title_score=title_score,
        creator_score=creator_score,
        time_score=time_score,
        content_score=content_score,
        duration_score=duration_score,
    )
    score = sum(value * weight for value, weight in score_parts)

    exact_distinctive_title = (
        left_title == right_title and len(left_title) >= _EXACT_TITLE_MIN_CHARS
    )
    same_creator_route = (
        creator_score >= _SAME_CREATOR_MIN_CREATOR and title_score >= _SAME_CREATOR_MIN_TITLE
    )
    duration_route = (
        creator_score >= _DURATION_MIN_CREATOR
        and title_score >= _DURATION_MIN_TITLE
        and (duration_score or 0.0) >= _DURATION_MIN_SIMILARITY
    )
    strong_title_route = (
        title_score >= _STRONG_TITLE_MIN_SIMILARITY
        and min(len(left_title), len(right_title)) >= _STRONG_TITLE_MIN_CHARS
    )
    content_route = (
        (content_score or 0.0) >= _CONTENT_MIN_STRONG
        and title_score >= _CONTENT_MIN_TITLE_FOR_STRONG
    ) or (
        (content_score or 0.0) >= _CONTENT_MIN_WEAK and title_score >= _CONTENT_MIN_TITLE_FOR_WEAK
    )
    timely = delta_hours is None or delta_hours <= PUBLISH_GAP_MAX_HOURS
    auto_match = (
        timely
        and creator_identity_match
        and bool(
            exact_distinctive_title
            or same_creator_route
            or duration_route
            or strong_title_route
            or content_route
        )
    )
    return MatchEvidence(
        eligible=True,
        auto_match=auto_match,
        score=round(score, 4),
        title_score=title_score,
        creator_score=creator_score,
        creator_identity_match=creator_identity_match,
        content_score=content_score,
        duration_score=duration_score,
        publish_delta_hours=delta_hours,
        reason=_audit_reason(
            title_score=title_score,
            creator_score=creator_score,
            creator_identity_match=creator_identity_match,
            content_score=content_score,
            duration_score=duration_score,
            publish_delta_hours=delta_hours,
        ),
    )


def _ineligible(code: str) -> MatchEvidence:
    """前两道闸（平台、标题）共用的"零分零证据"结果。

    `reason` 只放短码，便于按原因聚合；第三道闸不走这里，因为它要保留已经算出来的
    标题分与间隔（见 `publication_match()` 的闸 3）。
    """
    return MatchEvidence(
        eligible=False,
        auto_match=False,
        score=0.0,
        title_score=0.0,
        creator_score=0.0,
        creator_identity_match=False,
        content_score=None,
        duration_score=None,
        publish_delta_hours=None,
        reason=code,
    )
