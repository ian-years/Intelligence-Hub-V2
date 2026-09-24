#!/usr/bin/env python3
# ruff: noqa
# TODO(v2-adapt): V1 的旧内容情报看板，未适配。它查的是那座独立镜像库的四个 *_readable 视图
# （视图名与列名只能来自本文件的常量元组，FORBIDDEN_SOURCES 明令不读半成品的 内容作品 与
# 爬取任务日志），窗口终点取库内最新 published_at；列漂移靠 PRAGMA table_info 现查，缺列渲染成
# 显式「缺失」标记。V2 主库既没有这些视图也不叫这些名（storage/schema.py）。还有一处真地雷：
# --db / --output 的相对路径在 main() 里是拼到 **ROOT** 上的，而 ROOT 搬进包里之后指向
# src/.../ported/reports/，不传绝对路径就等于把产物写进源码树。
# 适配要做的那件事：查询段换成主库的一次只读聚合，产物路径交 FileStorage（默认 data/outputs/）。
"""旧内容情报看板 · 单文件离线 HTML 生成器（严格只读本地镜像）

契约来源：`FEISHU_LOCAL_DATABASE.md` 的「旧内容情报看板」章节（174-194 行）。

    python .\\generate_creator_insight_dashboard.py
    python .\\generate_creator_insight_dashboard.py --days 7 --output .\\outputs\\creator-insight-dashboard.html

职责
----
1. 用 SQLite URI `mode=ro` 只读打开 `downloads/feishu-base/feishu-base.sqlite3`，
   只查四个 `*_readable` 视图：`videos_readable`、`creators_readable`、
   `video_comments_readable`、`video_metric_snapshots_readable`。
   **不读半成品的 `内容作品`，也不读 `爬取任务日志`**（见 `FORBIDDEN_SOURCES`；
   连 `video_metric_snapshots_readable.content_work_record_id` 都不在取用列里）。
2. 窗口终点 = 库内**最新一条** `videos_readable.published_at`（文档 :184，不是 `datetime.now()`），
   起点 = 终点 - `--days` 天。窗口内的视频归纳为五节：
   选题共振、可执行动作、数据表现、观众信号、值得带走的观点/工具。
3. 产出一个自包含、可双击打开的离线 HTML：内联 CSS + 内联 JS + 内嵌 JSON 数据；
   无 CDN、无外部字体、查看时零网络请求。

只读与列漂移
------------
- 不写 SQLite，不写 `downloads/` 下任何文件；唯一写入目标是 `--output`，且拒绝落进 `downloads/`。
- 视图名/列名只能来自本文件的常量元组，SQL 里的值全部参数化。
- 镜像库可能整体缺视图或缺列（文档 :204：旧记录该列为 `NULL` 是真实表达，不伪造默认值）。
  取列前先查 `sqlite_master` 与 `PRAGMA table_info(<视图>)`，只 SELECT「想要 ∩ 实有」的列；
  缺失的视图与列同时写进 HTML 的「数据可用性」一节和 stderr。
  `NULL` 与「本库无此列」一律渲染成显式「缺失」标记，绝不替换成 0 或空标题。
- 标题、博主昵称、评论内容、口播稿摘要、要点全部是外部不可信文本：进 HTML 的过 `html.escape`，
  进 `<script>` 的 JSON 额外转义 `< > &` 与 U+2028/U+2029，链接只接受 http/https scheme。
  客户端 JS 只用 `textContent` / `dataset` / DOM 重排，从不拼 `innerHTML`。

定位
----
这是**旧的独立展示工具**：不属于飞书本地同步管线，未注册进 `launcher/launcher_server.py`，
任何 Codex 同步或分析自动化都不得调用它；它的退出码不影响同步成功状态。

退出码
------
- `0` 看板已生成
- `1` 环境或契约缺失/损坏（镜像库不存在、缺 `REQUIRED_VIEWS` 里的只读视图、
      `videos_readable` 缺必备列、SQL 失败、输出不可写）
- `2` 无可用样本（库内一条视频都没有 / 所有 `published_at` 都无法解析 / `--days` 非法 / 窗口内 0 条）；
      此档**不写任何 HTML**，避免产出一份看着成功的空看板
"""

from __future__ import annotations

import argparse
import html
import json
import re
import sqlite3
import sys
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent

TS_FORMAT = "%Y-%m-%d %H:%M:%S"
DEFAULT_DB_PATH = ROOT / "downloads" / "feishu-base" / "feishu-base.sqlite3"
DEFAULT_OUTPUT_PATH = ROOT / "outputs" / "creator-insight-dashboard.html"
SYNC_COMMAND = "python run_feishu_local_sync.py"

EXIT_OK = 0
EXIT_ENVIRONMENT = 1
EXIT_NO_SAMPLE = 2

#: 本工具允许访问的全部视图（文档 :176-177）。视图名只能来自这个常量元组。
VIEWS = (
    "videos_readable",
    "creators_readable",
    "video_comments_readable",
    "video_metric_snapshots_readable",
)

#: 缺了就没有任何样本可谈的视图：直接按环境问题退出，而不是报成「一条视频都没有」。
#: 另外两个（评论 / 指标快照）缺了只是少一节证据，fetch_view 会记进 missing_views 并降级出图。
REQUIRED_VIEWS = ("videos_readable", "creators_readable")

#: 明确不许读的对象（文档 :179）。只用于查询自检，程序里不存在读它们的路径。
FORBIDDEN_SOURCES = ("内容作品", "爬取任务日志", "content_works", "crawl_job_logs")

# 想要的列（文档 :46、:50、:68、:72）。缺列不报错，只记进「数据可用性」。
# 刻意不取 content_work_record_id / related_content_works_json：那属于「内容作品」层。
VIDEO_COLUMNS = (
    "record_id",
    "video_title",
    "platform",
    "platform_video_id",
    "video_url",
    "creator_record_id",
    "published_at",
    "duration_seconds",
    "content_summary",
    "key_points",
    "high_like_comment_summary",
    "user_pain_points",
    "comment_controversies",
    "expandable_topics",
    "representative_comments",
    "transcript_status",
    "video_download_status",
    "comment_fetch_status",
    "fetched_comment_count",
    "related_metric_snapshot_count",
)
CREATOR_COLUMNS = (
    "record_id",
    "creator_name",
    "platform",
    "cross_platform_identity",
    "follower_count",
    "follower_count_display",
    "video_collection_strategy",
)
COMMENT_COLUMNS = (
    "record_id",
    "comment_id",
    "platform",
    "platform_video_id",
    "video_record_id",
    "comment_text",
    "user_name",
    "user_gender",
    "comment_level",
    "commented_at",
    "like_count",
    "reply_count",
    "is_high_value",
    "insight_notes",
)
SNAPSHOT_COLUMNS = (
    "record_id",
    "snapshot_key",
    "video_record_id",
    "platform",
    "video_title",
    "checkpoint",
    "snapshot_at",
    "hours_since_publish",
    "view_count",
    "like_count",
    "comment_count",
    "favorite_count",
    "share_count",
    "coin_count",
    "danmaku_count",
    "data_source",
)

#: 缺了就没法诚实出图的列：直接非零退出，而不是降级成一张空表。
MANDATORY_COLUMNS = {
    "videos_readable": ("record_id", "published_at"),
    "creators_readable": ("record_id",),
    "video_comments_readable": ("record_id",),
    "video_metric_snapshots_readable": ("record_id",),
}

# 展示上限：看板是给人扫读的，不是全库导出。
MAX_TOPICS = 12
MAX_TOPIC_EXAMPLES = 4
MAX_ACTION_ITEMS = 24
MAX_TOP_COMMENTS = 20
MAX_POINT_ITEMS = 40
MAX_TOOL_ITEMS = 25
MAX_UNPLACED_ROWS = 200
TEXT_SNIPPET = 150

# 状态列词表（采集端写入的飞书单选值，见 download_bili_following_latest.py 的字段定义）。
STATUS_DONE = {
    "已抓取",
    "已下载",
    "已转写",
    "无需转写",
    "完成",
    "成功",
    "跳过",
    "ok",
    "done",
    "success",
}
STATUS_FAILED = {"失败", "错误", "部分失败", "error", "failed"}

_IDENT_RE = re.compile(r"^[a-z][a-z0-9_]*$")
_CJK_RUN = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff]{2,}")
_LATIN_TOKEN = re.compile(r"[A-Za-z][A-Za-z0-9+._#-]{2,29}")
_QUOTED = re.compile(r"[《「【]([^》」】]{2,30})[》」】]")
_ITEM_SPLIT = re.compile(r"[\n\r]+|；|;|\|")
_SENTENCE_SPLIT = re.compile(r"(?<=[。！？!?])")
_URL_OK = re.compile(r"^https?://[^\s\"'<>\u4e00-\u9fff]+$", re.I)

# 中文 2-gram 里过于泛化的词，进共振榜只会淹没信号。
_TOPIC_STOPWORDS = frozenset(
    {
        "我们",
        "你们",
        "他们",
        "这个",
        "那个",
        "什么",
        "怎么",
        "可以",
        "因为",
        "所以",
        "但是",
        "如果",
        "已经",
        "还是",
        "就是",
        "不是",
        "一个",
        "这些",
        "那些",
        "自己",
        "现在",
        "时候",
        "问题",
        "东西",
        "其实",
        "真的",
        "觉得",
        "知道",
        "今天",
        "一下",
        "视频",
        "内容",
        "博主",
        "平台",
        "数据",
        "观众",
        "评论",
        "账号",
        "工作",
        "分享",
        "有关",
        "相关",
        "三种",
        "五个",
        "十大",
        "一直",
        "一样",
        "以上",
        "以下",
        "需要",
        "没有",
        "有的",
        "一款",
        "这套",
        "整个",
        "第一",
        "最后",
        "结果",
        "过程",
        "方式",
    }
)
_TOOL_STOPWORDS = frozenset(
    {
        "the",
        "and",
        "for",
        "you",
        "with",
        "http",
        "https",
        "www",
        "com",
        "cn",
        "json",
        "null",
        "true",
        "false",
        "nbsp",
        "amp",
        "quot",
        "utf",
        "idx",
        "select",
        "from",
    }
)

MISSING_HTML = '<span class="miss" title="该列在本库不存在，或该行该列为 NULL（文档 :204：缺失是真实表达）">缺失</span>'

SECTIONS = (
    ("all", "全部"),
    ("sec-topic", "一、选题共振"),
    ("sec-action", "二、可执行动作"),
    ("sec-metric", "三、数据表现"),
    ("sec-audience", "四、观众信号"),
    ("sec-takeaway", "五、值得带走"),
    ("sec-videolist", "附：窗口视频清单"),
    ("sec-availability", "数据可用性"),
)


class ContractError(RuntimeError):
    """镜像结构与文档契约不符（必备视图/列缺失、数据库打不开等）。"""


# --------------------------------------------------------------------------- #
# 基础工具
# --------------------------------------------------------------------------- #


def parse_ts(value: Any) -> datetime | None:
    """宽松解析镜像时间文本：`YYYY-MM-DD HH:MM:SS`、ISO `T`、带时区（`Z` / `±HH:MM`）、纯日期、epoch 毫秒。"""
    if value is None:
        return None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        number = float(value)
        if number > 1e11:
            number /= 1000.0
        if number > 1e9:
            try:
                return datetime.fromtimestamp(number)
            except (OverflowError, OSError, ValueError):
                return None
        return None
    text = str(value).strip()
    if not text:
        return None
    for candidate in (text, text.replace("T", " ")):
        for fmt in (TS_FORMAT, "%Y-%m-%d %H:%M", "%Y-%m-%d", "%Y/%m/%d %H:%M:%S", "%Y/%m/%d"):
            try:
                return datetime.strptime(candidate, fmt)
            except ValueError:
                continue
    # 镜像里 published_at 也写带时区的 ISO（`2026-09-18T13:09:03+08:00`、`…Z`，见 local_store 的
    # _normalize_published_at）；上面那些无时区格式吃不下，判成「缺失」会把整行踢出窗口。
    # 换成同一条时间轴上的本地墙钟，才敢和上面返回的 naive 值比大小。
    try:
        aware = datetime.fromisoformat(re.sub(r"[zZ]$", "+00:00", text))
    except ValueError:
        return None
    return aware.astimezone().replace(tzinfo=None) if aware.tzinfo else aware


def fmt_ts(value: datetime | None) -> str:
    return value.strftime(TS_FORMAT) if value else ""


def to_int(value: Any) -> tuple[int | None, bool]:
    """返回 (整数值或 None, 是否「有值但解析不出数」)。NULL -> (None, False)。"""
    if value is None:
        return None, False
    if isinstance(value, bool):
        return int(value), False
    if isinstance(value, (int, float)):
        return int(value), False
    text = str(value).strip().replace(",", "").replace(" ", "")
    if not text:
        return None, False
    try:
        return int(float(text)), False
    except ValueError:
        return None, True


def plain(value: Any) -> str:
    """给 HTML 属性 / data-search 用的纯文本：NULL -> 空串（属性里不能塞标记）。"""
    return "" if value is None else str(value).strip()


def esc(value: Any) -> str:
    """外部不可信文本 -> 安全 HTML 文本节点；NULL -> 显式「缺失」。"""
    if value is None:
        return MISSING_HTML
    return html.escape(str(value), quote=True)


def esc_attr(value: Any) -> str:
    return html.escape(plain(value), quote=True)


def esc_num(value: Any) -> str:
    number, bad = to_int(value)
    if number is None:
        if bad:
            return (
                '<span class="miss">' + html.escape(str(value), quote=True) + "（无法解析）</span>"
            )
        return MISSING_HTML
    if isinstance(value, float):
        return html.escape(str(value), quote=True)
    return html.escape(f"{number:,}", quote=True)


def td(value: Any, numeric: bool = False) -> str:
    """可排序单元格：data-v 供 JS 比较，正文一律服务端转义。NULL 保留缺失标记。"""
    data_v = "" if value is None else html.escape(str(value), quote=True)
    klass = ' class="num"' if numeric else ""
    shown = esc_num(value) if numeric else esc(value)
    return "<td" + klass + ' data-v="' + data_v + '">' + shown + "</td>"


def th(label: str, numeric: bool = False) -> str:
    return (
        '<th class="num">' + html.escape(label, quote=True) + "</th>"
        if numeric
        else "<th>" + html.escape(label, quote=True) + "</th>"
    )


def safe_url(value: Any) -> str:
    """只接受 http/https 外链，其余一律不当链接渲染（挡 javascript: / data: / 引号注入）。"""
    text = plain(value)
    return text if len(text) <= 500 and _URL_OK.match(text) else ""


def snippet(value: Any, limit: int = TEXT_SNIPPET) -> str:
    text = plain(value)
    return text if len(text) <= limit else text[:limit] + "…"


def json_for_script(obj: Any) -> str:
    """内嵌进 <script> 的 JSON：中文不转义，但 `< > &` 与行分隔符必须转义。"""
    text = json.dumps(obj, ensure_ascii=False, separators=(",", ":"))
    return (
        text.replace("<", "\\u003c")
        .replace(">", "\\u003e")
        .replace("&", "\\u0026")
        .replace("\u2028", "\\u2028")
        .replace("\u2029", "\\u2029")
    )


def split_items(value: Any) -> list[str]:
    """把飞书富文本拼接出的长字段切成条目（按行/分号/竖线，超长单行再按句号兜底）。"""
    if value is None:
        return []
    text = str(value).strip()
    if not text:
        return []
    pieces = [p.strip(" \t-*·\u3000\u2022") for p in _ITEM_SPLIT.split(text)]
    out: list[str] = []
    for piece in pieces:
        if not piece or len(piece) < 2 or not re.search(r"[\u4e00-\u9fffA-Za-z0-9]", piece):
            continue
        if len(piece) > 46 and len(_SENTENCE_SPLIT.split(piece)) > 2:
            for sub in _SENTENCE_SPLIT.split(piece):
                sub = sub.strip(" \t-*·\u3000\u2022")
                if len(sub) >= 6:
                    out.append(sub[:TEXT_SNIPPET])
        else:
            out.append(piece[:TEXT_SNIPPET])
    return out


def topic_terms(text: str) -> set[str]:
    """一个视频的主题线索：中文 2-gram + 拉丁 token（确定性，不做任何推测）。"""
    terms: set[str] = set()
    for run in _CJK_RUN.findall(text):
        for i in range(len(run) - 1):
            gram = run[i : i + 2]
            if gram not in _TOPIC_STOPWORDS:
                terms.add(gram)
    for token in _LATIN_TOKEN.findall(text):
        low = token.lower()
        if low not in _TOOL_STOPWORDS:
            terms.add(low)
    return terms


# --------------------------------------------------------------------------- #
# 只读连接与镜像体检
# --------------------------------------------------------------------------- #


def assert_ident(name: str) -> None:
    if not _IDENT_RE.match(name):
        raise ContractError("内部常量表里出现非法标识符，拒绝执行 SQL：" + repr(name))


def assert_no_forbidden(sql: str) -> None:
    lowered = sql.lower()
    for banned in FORBIDDEN_SOURCES:
        if banned.lower() in lowered:
            raise ContractError("本工具禁止读取「" + banned + "」层的数据，已拒绝执行该查询")


def connect_readonly(path: Path) -> sqlite3.Connection:
    if not path.exists():
        raise ContractError(
            "本地镜像库不存在："
            + str(path)
            + "\n       → 先运行 `"
            + SYNC_COMMAND
            + "` 生成飞书 Base 的本地 SQLite 镜像，再重跑本工具。"
        )
    if not path.is_file():
        raise ContractError("--db 指向的不是文件：" + str(path))
    uri = "file:" + path.resolve().as_posix() + "?mode=ro"
    try:
        conn = sqlite3.connect(uri, uri=True)
    except sqlite3.Error as exc:
        raise ContractError(
            "以只读 URI 打开镜像库失败：" + str(path) + "（" + str(exc) + "）\n"
            "       → 确认文件没被占用或损坏；必要时重跑 `" + SYNC_COMMAND + "`。"
        ) from exc
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("SELECT 1 FROM sqlite_master LIMIT 1").fetchone()
    except sqlite3.Error as exc:
        conn.close()
        raise ContractError(
            "镜像库读不出 sqlite_master，可能不是 SQLite 数据库："
            + str(path)
            + "（"
            + str(exc)
            + "）\n"
            "       → 检查 --db 是否指错文件；正常镜像库由 `" + SYNC_COMMAND + "` 生成。"
        ) from exc
    return conn


def view_exists(conn: sqlite3.Connection, name: str) -> bool:
    assert_ident(name)
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type IN ('table','view') AND name = ? LIMIT 1",
        (name,),
    ).fetchone()
    return row is not None


def view_columns(conn: sqlite3.Connection, view: str) -> set[str]:
    assert_ident(view)
    try:
        rows = conn.execute("PRAGMA table_info('" + view + "')").fetchall()
    except sqlite3.Error:
        return set()
    return {str(row[1]) for row in rows}


def count_rows(conn: sqlite3.Connection, view: str) -> int | None:
    if not view_exists(conn, view):
        return None
    assert_ident(view)
    sql = "SELECT COUNT(*) FROM " + view
    assert_no_forbidden(sql)
    try:
        return int(conn.execute(sql).fetchone()[0])
    except sqlite3.Error:
        return None


def fetch_view(
    conn: sqlite3.Connection,
    view: str,
    wanted: tuple[str, ...],
    availability: "Availability",
) -> list[dict[str, Any]]:
    """只读取一张视图：列取「想要 ∩ 实有」交集，缺的记台账，不伪造。"""
    assert_ident(view)
    if view not in VIEWS:  # 双保险：视图名只可能来自常量元组
        raise ContractError("试图查询未授权的视图：" + view)
    if not view_exists(conn, view):
        availability.missing_views.append(view)
        return []
    available = view_columns(conn, view)
    if not available:
        availability.missing_views.append(view)
        availability.notes.append(view + " 存在但 PRAGMA table_info 读不出任何列，已按缺失处理")
        return []
    availability.columns[view] = available
    missing = [c for c in wanted if c not in available]
    if missing:
        availability.missing_columns[view] = missing
    hard_missing = [c for c in MANDATORY_COLUMNS.get(view, ()) if c not in available]
    if hard_missing:
        raise ContractError(
            "视图 " + view + " 缺少必备列：" + "、".join(hard_missing) + "\n"
            "       → *_readable 视图每次同步都会按最新定义重建，请重跑 `" + SYNC_COMMAND + "`；"
            "仍缺则说明镜像版本与本工具契约不符，需要改 sync_feishu_base_to_local.py 并补测试。"
        )
    usable = [c for c in wanted if c in available]
    for column in usable:
        assert_ident(column)
    sql = "SELECT " + (", ".join(usable) if usable else "record_id") + " FROM " + view
    assert_no_forbidden(sql)
    try:
        rows = [dict(row) for row in conn.execute(sql).fetchall()]
    except sqlite3.Error as exc:
        raise ContractError("只读查询 " + view + " 失败：" + str(exc)) from exc
    availability.fetched_rows[view] = len(rows)
    return rows


# --------------------------------------------------------------------------- #
# 数据可用性台账
# --------------------------------------------------------------------------- #


class Availability:
    """把「本库到底有什么、缺什么、哪里是 NULL」记全，供 HTML 与 stderr 双向输出。"""

    def __init__(self) -> None:
        self.db_path: Path = DEFAULT_DB_PATH
        self.missing_views: list[str] = []
        self.missing_columns: dict[str, list[str]] = {}
        self.columns: dict[str, set[str]] = {}
        self.fetched_rows: dict[str, int] = {}
        self.total_rows: dict[str, int | None] = {}
        self.null_counts: dict[str, Counter] = defaultdict(Counter)
        self.bad_numbers: dict[str, Counter] = defaultdict(Counter)
        self.unplaced_videos: list[dict[str, Any]] = []
        self.orphan_comments: int = 0
        self.orphan_snapshots: int = 0
        self.notes: list[str] = []

    def has_column(self, view: str, column: str) -> bool:
        return column in self.columns.get(view, set())

    def record_row(self, view: str, row: dict[str, Any]) -> None:
        for column, value in row.items():
            if value is None:
                self.null_counts[view][column] += 1

    def record_number(self, view: str, column: str, bad: bool) -> None:
        if bad:
            self.bad_numbers[view][column] += 1

    def all_notes(self) -> list[str]:
        notes = list(self.notes)
        for view in self.missing_views:
            notes.append(
                "缺少视图 " + view + "：依赖它的那一节是空的（不是 0 条，是查不到）。"
                "请重跑 `" + SYNC_COMMAND + "` 让同步程序按最新定义重建视图。"
            )
        for view, columns in sorted(self.missing_columns.items()):
            notes.append(
                "视图 "
                + view
                + " 缺少列："
                + "、".join(columns)
                + "。相关统计已跳过并把值标成「缺失」，没有用 0 或空字符串顶替。"
            )
        if self.unplaced_videos:
            notes.append(
                str(len(self.unplaced_videos)) + " 条视频的 published_at 为 NULL 或无法解析，"
                "无法判定是否落在窗口内，已单独列出而不是丢掉。"
            )
        if self.orphan_comments:
            notes.append(
                str(self.orphan_comments)
                + " 条评论按 (platform, platform_video_id) 或 video_record_id "
                "对不上窗口内任何视频，未计入观众信号。"
            )
        if self.orphan_snapshots:
            notes.append(
                str(self.orphan_snapshots)
                + " 条快照的 video_record_id / (platform, platform_video_id) "
                "对不上窗口内任何视频，未计入数据表现。"
            )
        return notes


# --------------------------------------------------------------------------- #
# 取数与窗口
# --------------------------------------------------------------------------- #


def load_dataset(conn: sqlite3.Connection, days: int) -> tuple[dict[str, Any], Availability]:
    availability = Availability()
    for view in VIEWS:
        availability.total_rows[view] = count_rows(conn, view)

    videos_all = fetch_view(conn, "videos_readable", VIDEO_COLUMNS, availability)
    creators = fetch_view(conn, "creators_readable", CREATOR_COLUMNS, availability)
    comments = fetch_view(conn, "video_comments_readable", COMMENT_COLUMNS, availability)
    snapshots = fetch_view(conn, "video_metric_snapshots_readable", SNAPSHOT_COLUMNS, availability)

    # 窗口终点：库内最新一条 published_at（文档 :184）。绝不用 datetime.now()。
    stamps: list[tuple[datetime, dict[str, Any]]] = []
    for row in videos_all:
        stamp = parse_ts(row.get("published_at"))
        if stamp is None:
            raw = row.get("published_at")
            availability.unplaced_videos.append(
                {
                    "record_id": row.get("record_id"),
                    "video_title": row.get("video_title"),
                    "platform": row.get("platform"),
                    "published_at": raw,
                    "reason": "published_at 为 NULL"
                    if raw is None
                    else "published_at 格式无法解析",
                }
            )
        else:
            stamps.append((stamp, row))
    availability.unplaced_videos.sort(key=lambda r: plain(r.get("record_id")))

    window_end = max((s for s, _ in stamps), default=None)
    window_start = window_end - timedelta(days=days) if window_end else None
    window_videos = [
        row for stamp, row in stamps if window_start and window_start <= stamp <= window_end
    ]
    window_videos.sort(key=lambda r: parse_ts(r.get("published_at")) or datetime.min, reverse=True)
    for row in window_videos:
        availability.record_row("videos_readable", row)

    creator_map = {
        str(row.get("record_id")): row for row in creators if row.get("record_id") is not None
    }
    video_keys = {
        str(row.get("record_id")): row for row in window_videos if row.get("record_id") is not None
    }
    # 裸 platform_video_id 当键会把抖音和 B站的同号作品并成一条（两个平台的 ID 命名空间互不相干），
    # 所以按 (platform, id) 归位；平台对不上就退化成「该 ID 在窗口内唯一」才敢认。
    id_keys: dict[tuple[str, str], dict[str, Any]] = {}
    id_by_count: Counter = Counter()
    if availability.has_column("videos_readable", "platform_video_id") and availability.has_column(
        "videos_readable", "platform"
    ):
        for row in window_videos:
            vid = plain(row.get("platform_video_id"))
            if vid:
                id_keys[(plain(row.get("platform")), vid)] = row
                id_by_count[vid] += 1
    unique_id_keys = {
        vid: row for (_platform, vid), row in id_keys.items() if id_by_count[vid] == 1
    }

    def attach(
        rows: list[dict[str, Any]],
        view: str,
        orphan_attr: str,
    ) -> dict[str, list[dict[str, Any]]]:
        buckets: dict[str, list[dict[str, Any]]] = defaultdict(list)
        orphans = 0
        by_record = availability.has_column(view, "video_record_id")
        by_platform = availability.has_column(view, "platform_video_id")
        by_site = by_platform and availability.has_column(view, "platform")
        for row in rows:
            host = None
            if by_record and row.get("video_record_id") is not None:
                host = video_keys.get(str(row["video_record_id"]))
            vid = plain(row.get("platform_video_id")) if by_platform else ""
            if host is None and vid:
                plat = plain(row.get("platform")) if by_site else ""
                # 评论侧没写平台（或视图压根没这列）时，只在该 ID 窗口内唯一时才敢认，宁可算孤儿也不并到别家。
                host = id_keys.get((plat, vid)) if plat else unique_id_keys.get(vid)
            if host is None:
                orphans += 1
                continue
            availability.record_row(view, row)
            buckets[plain(host.get("record_id"))].append(row)
        setattr(availability, orphan_attr, orphans)
        return buckets

    dataset: dict[str, Any] = {
        "videos": window_videos,
        "videos_all": videos_all,
        "creator_map": creator_map,
        "comments_by_video": attach(comments, "video_comments_readable", "orphan_comments"),
        "snapshots_by_video": attach(
            snapshots, "video_metric_snapshots_readable", "orphan_snapshots"
        ),
        "window_start": window_start,
        "window_end": window_end,
        "days": days,
    }
    return dataset, availability


# --------------------------------------------------------------------------- #
# 五节归纳（全部是确定性聚合，不写推测性结论）
# --------------------------------------------------------------------------- #


def video_label(video: dict[str, Any]) -> str:
    return (
        plain(video.get("video_title")) or plain(video.get("platform_video_id")) or "（标题缺失）"
    )


def creator_label(video: dict[str, Any], creator_map: dict[str, dict[str, Any]]) -> str:
    linked = plain(video.get("creator_record_id"))
    if not linked:
        return ""
    creator = creator_map.get(linked)
    if creator is None:
        return "（关联博主记录 " + linked + " 不在 creators_readable）"
    return plain(creator.get("creator_name")) or ("（record_id=" + linked + "）")


def source_of(video: dict[str, Any], creator_map: dict[str, dict[str, Any]]) -> dict[str, str]:
    return {
        "record_id": plain(video.get("record_id")),
        "title": video_label(video),
        "creator": creator_label(video, creator_map),
        "platform": plain(video.get("platform")),
        "published_at": plain(video.get("published_at")),
    }


def build_topic_resonance(
    dataset: dict[str, Any], availability: Availability
) -> list[dict[str, Any]]:
    """选题共振：同一主题词被窗口内 ≥2 条视频覆盖。口径写在节首提示里。"""
    videos = dataset["videos"]
    creator_map = dataset["creator_map"]
    fields = [
        f
        for f in ("key_points", "expandable_topics", "content_summary", "video_title")
        if availability.has_column("videos_readable", f)
    ]
    if not fields:
        availability.notes.append(
            "videos_readable 里没有任何可用于主题提取的列，选题共振一节为空。"
        )
        return []

    video_terms: dict[str, set[str]] = {}
    term_hits: dict[str, list[tuple[dict[str, Any], str, str]]] = defaultdict(list)
    term_freq: Counter = Counter()
    for video in videos:
        rid = plain(video.get("record_id"))
        terms: set[str] = set()
        for field in fields:
            for item in split_items(video.get(field)):
                found = topic_terms(item)
                terms |= found
                for term in found:
                    term_hits[term].append((video, item, field))
                    term_freq[term] += 1
        video_terms[rid] = terms

    grouped: dict[str, set[str]] = defaultdict(set)
    for rid, terms in video_terms.items():
        for term in terms:
            grouped[term].add(rid)

    candidates = sorted(
        ((term, rids) for term, rids in grouped.items() if len(rids) >= 2),
        key=lambda kv: (-len(kv[1]), -term_freq[kv[0]], kv[0]),
    )

    topics: list[dict[str, Any]] = []
    chosen: list[tuple[str, set[str]]] = []
    for term, rids in candidates:
        hit_videos = [v for v in videos if plain(v.get("record_id")) in rids]
        titles = sorted({video_label(v) for v in hit_videos})
        creators = {plain(v.get("creator_record_id")) for v in hit_videos}
        platforms = sorted(
            {plain(v.get("platform")) for v in hit_videos if plain(v.get("platform"))}
        )
        if any(term in done_term and rids == done_rids for done_term, done_rids in chosen):
            continue  # 已被更宽的同范围词组覆盖，不重复上榜
        chosen.append((term, rids))
        if len(creators) >= 2:
            kind = "多位博主撞同一选题"
        elif len(creators) <= 1 and len(platforms) >= 2:
            kind = "同一博主跨平台分发"
        else:
            kind = "同一博主多条视频"
        topics.append(
            {
                "term": term,
                "kind": kind,
                "video_count": len(rids),
                "creator_count": len(creators),
                "platforms": platforms or ["缺失"],
                "videos": titles,
                "frequency": term_freq[term],
                "examples": [
                    {"text": item, "field": field, **source_of(video, creator_map)}
                    for video, item, field in term_hits[term][:MAX_TOPIC_EXAMPLES]
                ],
            }
        )
        if len(topics) >= MAX_TOPICS:
            break
    return topics


def classify_status(value: Any) -> str:
    text = plain(value)
    if not text:
        return "missing"
    if text in STATUS_FAILED:
        return "failed"
    if text in STATUS_DONE:
        return "done"
    return "unknown"


def build_actions(dataset: dict[str, Any], availability: Availability) -> dict[str, Any]:
    """可执行动作：候选选题（可拓展选题 / 痛点）+ 数据补齐动作（状态列与关联结果）。"""
    videos = dataset["videos"]
    creator_map = dataset["creator_map"]
    out: dict[str, Any] = {"topics": [], "pain_points": [], "gaps": []}

    if availability.has_column("videos_readable", "expandable_topics"):
        acc: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for video in videos:
            for item in split_items(video.get("expandable_topics")):
                acc[item.strip()].append(source_of(video, creator_map))
        out["topics"] = [
            {"text": text, "sources": sources, "resonance": len(sources) >= 2}
            for text, sources in sorted(acc.items(), key=lambda kv: (-len(kv[1]), kv[0]))[
                :MAX_ACTION_ITEMS
            ]
        ]
        if not out["topics"]:
            availability.notes.append(
                "窗口内没有任何视频填写「可拓展选题」，选题动作只剩数据补齐类。"
            )
    else:
        availability.notes.append(
            "本库 videos_readable 无 expandable_topics 列，无法给出候选选题动作。"
        )

    if availability.has_column("videos_readable", "user_pain_points"):
        hits: dict[str, list[tuple[dict[str, Any], str]]] = defaultdict(list)
        for video in videos:
            for item in split_items(video.get("user_pain_points")):
                for term in topic_terms(item):
                    hits[term].append((video, item))
        ranked = sorted(
            hits.items(),
            key=lambda kv: (-len({plain(v.get("record_id")) for v, _ in kv[1]}), kv[0]),
        )
        for term, pairs in ranked[:8]:
            video_count = len({plain(v.get("record_id")) for v, _ in pairs})
            if video_count < 2:
                continue
            out["pain_points"].append(
                {
                    "term": term,
                    "video_count": video_count,
                    "items": [
                        {"text": item, **source_of(video, creator_map)}
                        for video, item in pairs[:MAX_TOPIC_EXAMPLES]
                    ],
                }
            )

    gap_specs = (
        ("comment_fetch_status", "评论抓取状态", "观众信号缺失，需补抓评论"),
        ("transcript_status", "转写状态", "口播稿语料缺失，需补转写与口播稿同步"),
        ("video_download_status", "视频下载状态", "素材未落地，需补下载"),
    )
    wording_map = {
        "missing": "该状态列为 NULL（旧记录没这个字段，文档 :204）",
        "failed": "状态值属于失败词表",
        "unknown": "状态值不在已知词表内（可能是新增选项，本工具不猜测含义）",
    }
    for column, label, action in gap_specs:
        if not availability.has_column("videos_readable", column):
            continue
        buckets: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for video in videos:
            buckets[classify_status(video.get(column))].append(video)
        for state in ("failed", "missing", "unknown"):
            members = buckets.get(state) or []
            if not members:
                continue
            values = sorted({plain(v.get(column)) or "NULL" for v in members})
            out["gaps"].append(
                {
                    "column": column,
                    "label": label,
                    "action": action,
                    "wording": wording_map[state],
                    "count": len(members),
                    "values": values,
                    "videos": [source_of(v, creator_map) for v in members[:MAX_TOPIC_EXAMPLES]],
                }
            )

    if availability.has_column("videos_readable", "fetched_comment_count"):
        zero = [
            v
            for v in videos
            if to_int(v.get("fetched_comment_count"))[0] in (0, None)
            and not dataset["comments_by_video"].get(plain(v.get("record_id")))
        ]
        if zero:
            out["gaps"].append(
                {
                    "column": "fetched_comment_count",
                    "label": "已抓评论数",
                    "action": "观众信号样本为 0 或未填，需补抓评论",
                    "wording": "fetched_comment_count 为 0 / NULL，且窗口内在 video_comments_readable 查不到该视频的评论",
                    "count": len(zero),
                    "values": sorted(
                        {plain(v.get("fetched_comment_count")) or "NULL" for v in zero}
                    ),
                    "videos": [source_of(v, creator_map) for v in zero[:MAX_TOPIC_EXAMPLES]],
                }
            )

    if availability.has_column("video_metric_snapshots_readable", "video_record_id"):
        no_snapshot = [
            v for v in videos if not dataset["snapshots_by_video"].get(plain(v.get("record_id")))
        ]
        if no_snapshot:
            out["gaps"].append(
                {
                    "column": "video_record_id@video_metric_snapshots_readable",
                    "label": "指标快照",
                    "action": "无 T+N 指标快照，需补采数据",
                    "wording": "video_metric_snapshots_readable 里查不到该视频的任何快照",
                    "count": len(no_snapshot),
                    "values": [],
                    "videos": [source_of(v, creator_map) for v in no_snapshot[:MAX_TOPIC_EXAMPLES]],
                }
            )
    return out


def latest_snapshot(
    snapshots: list[dict[str, Any]], availability: Availability
) -> dict[str, Any] | None:
    """一条视频的最新快照：按解析后的 snapshot_at 取最大值；时间全缺则返回 None 而不是猜。"""
    if not snapshots or not availability.has_column(
        "video_metric_snapshots_readable", "snapshot_at"
    ):
        return None
    timed = [(parse_ts(s.get("snapshot_at")), s) for s in snapshots]
    usable = [(t, s) for t, s in timed if t is not None]
    return max(usable, key=lambda pair: pair[0])[1] if usable else None


def build_metrics(dataset: dict[str, Any], availability: Availability) -> dict[str, Any]:
    videos = dataset["videos"]
    snap_view = "video_metric_snapshots_readable"
    numeric_columns = (
        "view_count",
        "like_count",
        "comment_count",
        "favorite_count",
        "share_count",
        "coin_count",
        "danmaku_count",
    )
    rows: list[dict[str, Any]] = []
    platform_acc: dict[str, dict[str, Any]] = defaultdict(
        lambda: {"videos": 0, "snapshots": 0, "views": [], "likes": []}
    )

    for video in videos:
        rid = plain(video.get("record_id"))
        snaps = dataset["snapshots_by_video"].get(rid, [])
        latest = latest_snapshot(snaps, availability)
        entry: dict[str, Any] = dict(source_of(video, dataset["creator_map"]))
        entry["snapshot_total"] = len(snaps)
        entry["snapshot_checkpoint"] = plain(latest.get("checkpoint")) if latest else None
        entry["snapshot_at"] = plain(latest.get("snapshot_at")) if latest else None
        entry["data_source"] = plain(latest.get("data_source")) if latest else None
        for column in numeric_columns:
            entry[column] = None
        entry["engagement"] = None

        key = entry["platform"] or "平台缺失"
        acc = platform_acc[key]
        acc["videos"] += 1
        acc["snapshots"] += len(snaps)

        if latest:
            for column in numeric_columns:
                if not availability.has_column(snap_view, column):
                    continue
                number, bad = to_int(latest.get(column))
                availability.record_number(snap_view, column, bad)
                entry[column] = number
                if number is not None:
                    if column == "view_count":
                        acc["views"].append(number)
                    elif column == "like_count":
                        acc["likes"].append(number)
            if entry["view_count"] and entry["like_count"] is not None:
                entry["engagement"] = round(entry["like_count"] / entry["view_count"] * 100, 2)
        rows.append(entry)

    checkpoints: Counter = Counter()
    sources: Counter = Counter()
    for snaps in dataset["snapshots_by_video"].values():
        for snap in snaps:
            if availability.has_column(snap_view, "checkpoint"):
                checkpoints[plain(snap.get("checkpoint")) or "NULL"] += 1
            if availability.has_column(snap_view, "data_source"):
                sources[plain(snap.get("data_source")) or "NULL"] += 1

    def median(values: list[int]) -> int | None:
        if not values:
            return None
        ordered = sorted(values)
        mid = len(ordered) // 2
        return ordered[mid] if len(ordered) % 2 else (ordered[mid - 1] + ordered[mid]) // 2

    platforms = [
        {
            "platform": name,
            "videos": acc["videos"],
            "snapshots": acc["snapshots"],
            "median_views": median(acc["views"]),
            "median_likes": median(acc["likes"]),
            "with_views": len(acc["views"]),
        }
        for name, acc in sorted(platform_acc.items(), key=lambda kv: (-kv[1]["videos"], kv[0]))
    ]
    ranked = sorted(
        (r for r in rows if r["view_count"] is not None), key=lambda r: -r["view_count"]
    )
    return {
        "videos": rows,
        "platforms": platforms,
        "checkpoints": [{"label": k, "count": v} for k, v in checkpoints.most_common()],
        "data_sources": [{"label": k, "count": v} for k, v in sources.most_common()],
        "top_by_views": ranked[:10],
        "numeric_columns_available": availability.has_column(snap_view, "view_count"),
    }


def build_audience(dataset: dict[str, Any], availability: Availability) -> dict[str, Any]:
    videos = dataset["videos"]
    creator_map = dataset["creator_map"]
    view = "video_comments_readable"
    pooled: list[tuple[dict[str, Any], dict[str, Any]]] = []
    for video in videos:
        for comment in dataset["comments_by_video"].get(plain(video.get("record_id")), []):
            pooled.append((video, comment))

    high_value_total = 0
    if availability.has_column(view, "is_high_value"):
        high_value_total = sum(1 for _, c in pooled if to_int(c.get("is_high_value"))[0] == 1)

    sort_column = "like_count" if availability.has_column(view, "like_count") else "commented_at"
    ordered: list[tuple[int, int, dict[str, Any], dict[str, Any]]] = []
    for video, comment in pooled:
        raw = comment.get(sort_column)
        number, bad = to_int(raw)
        availability.record_number(view, sort_column, bad)
        stamp = parse_ts(raw)
        rank = number if number is not None else (int(stamp.timestamp()) if stamp else -1)
        ordered.append((rank, id(video), video, comment))
    ordered.sort(key=lambda item: -item[0])
    top_comments = [
        {
            "text": snippet(comment.get("comment_text")),
            "comment_text_missing": comment.get("comment_text") is None,
            "user_name": comment.get("user_name"),
            "like_count": comment.get("like_count"),
            "reply_count": comment.get("reply_count"),
            "is_high_value": comment.get("is_high_value"),
            "commented_at": comment.get("commented_at"),
            **source_of(video, creator_map),
        }
        for _, __, video, comment in ordered[:MAX_TOP_COMMENTS]
    ]

    genders: Counter = Counter()
    if availability.has_column(view, "user_gender"):
        for _, comment in pooled:
            genders[plain(comment.get("user_gender")) or "NULL"] += 1
    levels: Counter = Counter()
    if availability.has_column(view, "comment_level"):
        for _, comment in pooled:
            levels[plain(comment.get("comment_level")) or "NULL"] += 1
    reply_total: int | None = None
    reply_missing = 0
    if availability.has_column(view, "reply_count"):
        reply_total = 0
        for _, comment in pooled:
            number, _bad = to_int(comment.get("reply_count"))
            if comment.get("reply_count") is None:
                reply_missing += 1
            reply_total += number or 0

    def collect_field(field: str) -> list[dict[str, Any]]:
        if not availability.has_column("videos_readable", field):
            return []
        acc: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for video in videos:
            for item in split_items(video.get(field)):
                acc[item].append(source_of(video, creator_map))
        return [
            {"text": text, "sources": sources}
            for text, sources in sorted(acc.items(), key=lambda kv: (-len(kv[1]), kv[0]))[
                :MAX_ACTION_ITEMS
            ]
        ]

    return {
        "comment_total": len(pooled),
        "high_value_total": high_value_total
        if availability.has_column(view, "is_high_value")
        else None,
        "sorted_by": sort_column,
        "videos_without_comments": [
            source_of(v, creator_map)
            for v in videos
            if not dataset["comments_by_video"].get(plain(v.get("record_id")))
        ],
        "top_comments": top_comments,
        "genders": [{"label": k, "count": v} for k, v in genders.most_common()],
        "levels": [{"label": k, "count": v} for k, v in levels.most_common()],
        "reply_total": reply_total,
        "reply_missing": reply_missing,
        "pain_points": collect_field("user_pain_points"),
        "controversies": collect_field("comment_controversies"),
        "high_like_summaries": collect_field("high_like_comment_summary"),
        "representative_comments": collect_field("representative_comments"),
    }


def build_takeaways(dataset: dict[str, Any], availability: Availability) -> dict[str, Any]:
    videos = dataset["videos"]
    creator_map = dataset["creator_map"]
    points_acc: dict[str, list[dict[str, Any]]] = defaultdict(list)
    if availability.has_column("videos_readable", "key_points"):
        for video in videos:
            for item in split_items(video.get("key_points")):
                points_acc[item].append(source_of(video, creator_map))
    points = [
        {"text": text, "sources": sources, "resonance": len(sources) >= 2}
        for text, sources in sorted(points_acc.items(), key=lambda kv: (-len(kv[1]), kv[0]))[
            :MAX_POINT_ITEMS
        ]
    ]

    tools: Counter = Counter()
    quoted: Counter = Counter()
    corpus_fields = [
        f
        for f in ("content_summary", "key_points", "representative_comments", "expandable_topics")
        if availability.has_column("videos_readable", f)
    ]
    for video in videos:
        for field in corpus_fields:
            text = plain(video.get(field))
            if not text:
                continue
            for token in _LATIN_TOKEN.findall(text):
                low = token.lower()
                if low not in _TOOL_STOPWORDS and not low.isdigit():
                    tools[token] += 1
            for group in _QUOTED.findall(text):
                quoted[group.strip()] += 1

    notes: list[dict[str, Any]] = []
    if availability.has_column("video_comments_readable", "insight_notes"):
        for video in videos:
            for comment in dataset["comments_by_video"].get(plain(video.get("record_id")), []):
                text = plain(comment.get("insight_notes"))
                if text:
                    notes.append({"text": snippet(text), **source_of(video, creator_map)})
        notes = notes[:MAX_POINT_ITEMS]

    return {
        "points": points,
        "fields_used": corpus_fields,
        "tools": [
            {"label": name, "count": count} for name, count in tools.most_common(MAX_TOOL_ITEMS)
        ],
        "quoted": [
            {"label": name, "count": count} for name, count in quoted.most_common(MAX_TOOL_ITEMS)
        ],
        "comment_notes": notes,
    }


# --------------------------------------------------------------------------- #
# HTML 渲染（内联 CSS + 内联 JS + 内嵌 JSON；零外部资源）
# --------------------------------------------------------------------------- #

CSS = r"""
:root {
  --bg:#0b1020; --bg2:#101a30; --card:rgba(19,29,51,.92); --line:rgba(120,160,220,.2);
  --ink:#eef2f8; --muted:#93a3bd; --cyan:#45d8ff; --violet:#a98bff; --coral:#ff7a5c;
  --green:#2fd39b; --amber:#ffc861;
  --mono:ui-monospace,Consolas,Menlo,monospace;
  --sans:-apple-system,BlinkMacSystemFont,"Segoe UI","Microsoft YaHei","PingFang SC",sans-serif;
}
* { box-sizing: border-box; }
body { margin:0; color:var(--ink); background:
  radial-gradient(1100px 560px at 12% -6%, rgba(69,216,255,.15), transparent 60%),
  radial-gradient(900px 520px at 92% 4%, rgba(169,139,255,.15), transparent 55%), var(--bg);
  font-family:var(--sans); line-height:1.62; }
header { padding:26px 5vw 18px; border-bottom:1px solid var(--line); }
h1 { margin:0 0 6px; font-size:clamp(1.35rem,2.5vw,2rem); letter-spacing:-.3px; }
h2 { margin:0; font-size:1.08rem; border-left:4px solid var(--cyan); padding-left:11px; }
h3 { margin:24px 0 10px; font-size:.95rem; color:var(--cyan); letter-spacing:.4px; }
h4 { margin:0 0 6px; font-size:.95rem; }
.sub { color:var(--muted); font-size:.85rem; }
.kv { display:flex; flex-wrap:wrap; gap:6px 20px; margin-top:12px; font-size:.83rem; color:var(--muted); }
.kv b { font-family:var(--mono); color:var(--ink); font-weight:600; }
.toolbar { position:sticky; top:0; z-index:5; display:flex; flex-wrap:wrap; gap:10px; align-items:center;
  padding:11px 5vw; background:rgba(11,16,32,.95); border-bottom:1px solid var(--line); }
.toolbar input, .toolbar select { background:var(--bg2); color:var(--ink); border:1px solid var(--line);
  border-radius:8px; padding:6px 10px; font:inherit; font-size:.83rem; }
.toolbar input[type=search] { min-width:230px; }
nav { display:flex; flex-wrap:wrap; gap:6px; }
nav button { background:transparent; color:var(--muted); border:1px solid var(--line); border-radius:999px;
  padding:5px 12px; font:inherit; font-size:.79rem; cursor:pointer; }
nav button.active { color:#06131f; background:var(--cyan); border-color:var(--cyan); font-weight:700; }
label.switch { color:var(--muted); font-size:.79rem; display:inline-flex; align-items:center; gap:6px; }
main { padding:20px 5vw 70px; }
.section { margin-bottom:34px; }
.section[hidden] { display:none; }
.lead { color:var(--muted); font-size:.84rem; margin:12px 0 14px; padding-left:10px; border-left:3px solid var(--line); }
.item { background:var(--card); border:1px solid var(--line); border-radius:12px; padding:13px 15px;
  margin:0 0 10px; break-inside:avoid; overflow-x:auto; }
.item[hidden] { display:none; }
.item.warn { border-color:rgba(255,200,97,.5); background:rgba(43,33,17,.75); }
.grid { display:grid; gap:10px; grid-template-columns:repeat(auto-fit,minmax(200px,1fr)); margin:0 0 12px; }
.stat { background:var(--card); border:1px solid var(--line); border-radius:12px; padding:11px 13px; }
.stat .val { font-family:var(--mono); font-size:1.45rem; font-weight:700; }
.stat small { color:var(--muted); }
ul { margin:6px 0 0; padding-left:20px; }
li { margin:2px 0; }
.meta { color:var(--muted); font-size:.76rem; }
.mono { font-family:var(--mono); }
.miss { color:var(--coral); background:rgba(255,122,92,.13); border:1px dashed rgba(255,122,92,.5);
  border-radius:4px; padding:0 5px; font-size:.75rem; font-family:var(--mono); }
.pill { display:inline-block; font-size:.72rem; font-family:var(--mono); padding:1px 8px; border-radius:999px;
  border:1px solid var(--line); color:var(--muted); margin:2px 6px 2px 0; }
.pill.hot { color:#0a1725; background:var(--amber); border-color:var(--amber); }
.pill.kind { color:var(--cyan); border-color:rgba(69,216,255,.45); }
.pill.bad { color:var(--coral); border-color:rgba(255,122,92,.45); }
.pill.good { color:var(--green); border-color:rgba(47,211,155,.45); }
table { width:100%; border-collapse:collapse; font-size:.82rem; }
th, td { text-align:left; padding:7px 9px; border-bottom:1px solid var(--line); vertical-align:top; }
th { color:var(--muted); font-weight:600; font-size:.73rem; letter-spacing:.4px; white-space:nowrap; cursor:pointer; }
td.num, th.num { font-family:var(--mono); text-align:right; }
tbody tr:hover { background:rgba(69,216,255,.06); }
a { color:var(--cyan); text-decoration:none; }
a:hover { text-decoration:underline; }
footer { padding:18px 5vw 42px; border-top:1px solid var(--line); color:var(--muted); font-size:.77rem; }
code { font-family:var(--mono); background:rgba(120,160,220,.13); padding:1px 5px; border-radius:4px; }
@media print { body { background:#fff; color:#111; } .toolbar { position:static; } }
"""

JS = r"""
(function () {
  "use strict";
  var nav = document.getElementById("nav");
  var sections = [].slice.call(document.querySelectorAll("main .section"));
  var items = [].slice.call(document.querySelectorAll(".item"));
  var search = document.getElementById("q");
  var platform = document.getElementById("platform");
  var missOnly = document.getElementById("missonly");
  var counter = document.getElementById("visible-count");
  var active = "all";

  function apply() {
    var q = (search.value || "").trim().toLowerCase();
    var p = platform.value || "";
    var onlyMiss = missOnly.checked;
    var shown = 0;
    items.forEach(function (el) {
      var inSection = active === "all" || el.getAttribute("data-section") === active;
      var own = el.getAttribute("data-platform") || "";
      var okPlatform = p === "" || own === "" || own === p;
      var hay = (el.getAttribute("data-search") || "").toLowerCase();
      var okQuery = q === "" || hay.indexOf(q) >= 0;
      var okMiss = !onlyMiss || el.getAttribute("data-missing") === "1";
      var show = inSection && okPlatform && okQuery && okMiss;
      el.hidden = !show;
      if (show) { shown += 1; }
    });
    sections.forEach(function (el) {
      if (el.getAttribute("data-static") === "1") { return; }
      el.hidden = !(active === "all" || el.id === active);
    });
    counter.textContent = String(shown);
  }

  nav.addEventListener("click", function (ev) {
    var btn = ev.target && ev.target.closest ? ev.target.closest("button[data-target]") : null;
    if (!btn) { return; }
    active = btn.getAttribute("data-target");
    [].slice.call(nav.querySelectorAll("button")).forEach(function (b) {
      if (b === btn) { b.classList.add("active"); } else { b.classList.remove("active"); }
    });
    apply();
  });
  search.addEventListener("input", apply);
  platform.addEventListener("change", apply);
  missOnly.addEventListener("change", apply);

  /* 排序：只重排服务端已经渲染好的行节点，不写 innerHTML */
  [].slice.call(document.querySelectorAll("table[data-sortable]")).forEach(function (table) {
    if (!table.tHead || !table.tBodies.length) { return; }
    [].slice.call(table.tHead.rows[0].cells).forEach(function (headCell, index) {
      headCell.addEventListener("click", function () {
        var asc = headCell.getAttribute("aria-sort") !== "ascending";
        [].slice.call(table.tHead.rows[0].cells).forEach(function (c) { c.removeAttribute("aria-sort"); });
        headCell.setAttribute("aria-sort", asc ? "ascending" : "descending");
        var body = table.tBodies[0];
        var rows = [].slice.call(body.rows);
        rows.sort(function (a, b) {
          var ca = a.cells[index], cb = b.cells[index];
          var av = ca ? (ca.getAttribute("data-v") || "") : "";
          var bv = cb ? (cb.getAttribute("data-v") || "") : "";
          if (av === "" && bv === "") { return 0; }
          if (av === "") { return 1; }
          if (bv === "") { return -1; }
          var an = parseFloat(av), bn = parseFloat(bv);
          var both = !isNaN(an) && !isNaN(bn) && /^\s*-?[\d.]+\s*$/.test(av) && /^\s*-?[\d.]+\s*$/.test(bv);
          var cmp = both ? (an - bn) : String(av).localeCompare(String(bv), "zh");
          return asc ? cmp : -cmp;
        });
        rows.forEach(function (r) { body.appendChild(r); });
      });
    });
  });

  /* 内嵌 JSON 只喂给这一张表：一律 textContent 写入，绝不用 innerHTML 拼外部文本 */
  var host = document.getElementById("video-list");
  var label = document.getElementById("video-list-count");
  try {
    var data = JSON.parse(document.getElementById("dashboard-data").textContent);
    var videos = data.videos || [];
    var table = document.createElement("table");
    table.setAttribute("data-sortable", "1");
    var head = table.createTHead().insertRow();
    ["标题", "平台", "博主", "发布时间", "评论数", "快照数", "最新播放", "最新点赞"].forEach(function (name) {
      var cell = head.insertCell();
      cell.textContent = name;
    });
    var body = table.createTBody();
    videos.forEach(function (video) {
      var row = body.insertRow();
      [video.title, video.platform, video.creator, video.published_at, video.comments,
       video.snapshots, video.views, video.likes].forEach(function (value) {
        var cell = row.insertCell();
        if (value === null || value === undefined || value === "") {
          cell.textContent = "缺失";
          cell.classList.add("miss-cell");
        } else {
          cell.textContent = String(value);
        }
      });
    });
    host.appendChild(table);
    label.textContent = String(videos.length);
  } catch (err) {
    while (host.firstChild) { host.removeChild(host.firstChild); }
    var warn = document.createElement("div");
    warn.textContent = "内嵌 JSON 解析失败，本节不可交互，但上方五节是服务端渲染的静态内容，不受影响：" + String(err);
    host.appendChild(warn);
  }

  apply();
})();
"""

METRIC_COLUMNS = (
    ("title", "标题", False),
    ("platform", "平台", False),
    ("creator", "博主", False),
    ("published_at", "发布时间", False),
    ("snapshot_checkpoint", "检查点", False),
    ("snapshot_at", "快照时间", False),
    ("view_count", "播放", True),
    ("like_count", "点赞", True),
    ("comment_count", "评论", True),
    ("favorite_count", "收藏", True),
    ("share_count", "分享", True),
    ("coin_count", "投币", True),
    ("danmaku_count", "弹幕", True),
    ("engagement", "互动率%", True),
    ("snapshot_total", "快照数", True),
    ("data_source", "数据来源", False),
)


def item_div(
    section: str, platform: str, search_terms: str, missing: Any, body: str, warn: bool = False
) -> str:
    klass = "item warn" if warn else "item"
    return (
        '<div class="'
        + klass
        + '" data-section="'
        + esc_attr(section)
        + '" data-platform="'
        + esc_attr(platform)
        + '" data-search="'
        + esc_attr(search_terms)
        + '" data-missing="'
        + ("1" if missing else "0")
        + '">'
        + body
        + "</div>"
    )


def render_sources(sources: list[dict[str, Any]], limit: int = 6) -> str:
    if not sources:
        return '<div class="meta">来源：' + MISSING_HTML + "</div>"
    parts = []
    for src in sources[:limit]:
        creator = (
            html.escape(src.get("creator") or "", quote=True)
            if src.get("creator")
            else MISSING_HTML
        )
        stamp = src.get("published_at") or ""
        tail = (
            ' <span class="meta mono">' + html.escape(stamp, quote=True) + "</span>"
            if stamp
            else ""
        )
        parts.append(creator + "：" + html.escape(str(src.get("title") or ""), quote=True) + tail)
    extra = (
        '<span class="meta"> 等 ' + str(len(sources)) + " 条</span>" if len(sources) > limit else ""
    )
    return '<div class="meta">来源：' + " ； ".join(parts) + extra + "</div>"


def pills(pairs: list[dict[str, Any]], klass: str = "") -> str:
    if not pairs:
        return MISSING_HTML
    joined = []
    for pair in pairs:
        label = html.escape(plain(pair.get("label")) or "NULL", quote=True)
        suffix = "" if klass == "" else " " + klass
        joined.append(
            '<span class="pill' + suffix + '">' + label + " · " + str(pair.get("count")) + "</span>"
        )
    return "".join(joined)


def shown(value: Any) -> str:
    """统计数字：None -> 显式「缺失」，不是 0。"""
    return MISSING_HTML if value is None else html.escape(str(value), quote=True)


def topic_html(topics: list[dict[str, Any]]) -> str:
    out = []
    for topic in topics:
        items = (
            "".join(
                "<li>"
                + html.escape(ex["text"], quote=True)
                + ' <span class="meta mono">['
                + html.escape(ex["field"], quote=True)
                + " · "
                + html.escape(ex["title"], quote=True)
                + "]</span></li>"
                for ex in topic["examples"]
            )
            or "<li>" + MISSING_HTML + "</li>"
        )
        chip = (
            '<span class="pill kind">'
            + html.escape(topic["kind"], quote=True)
            + "</span>"
            + '<span class="pill">视频 '
            + str(topic["video_count"])
            + "</span>"
            + '<span class="pill">博主 '
            + str(topic["creator_count"])
            + "</span>"
            + '<span class="pill">词频 '
            + str(topic["frequency"])
            + "</span>"
            + "".join(
                '<span class="pill">' + html.escape(p, quote=True) + "</span>"
                for p in topic["platforms"]
            )
        )
        body = (
            "<h4>"
            + html.escape(topic["term"], quote=True)
            + "</h4>"
            + chip
            + "<ul>"
            + items
            + "</ul>"
            + '<div class="meta">命中视频：'
            + html.escape("、".join(topic["videos"]), quote=True)
            + "</div>"
        )
        platform = topic["platforms"][0] if len(topic["platforms"]) == 1 else ""
        search = (
            topic["term"]
            + " "
            + " ".join(topic["videos"])
            + " "
            + " ".join(ex["text"] for ex in topic["examples"])
        )
        out.append(item_div("sec-topic", platform, search, 0, body))
    return "".join(out)


def action_html(actions: dict[str, Any]) -> str:
    out = []
    for topic in actions.get("topics") or []:
        badge = '<span class="pill hot">多条视频共有</span>' if topic["resonance"] else ""
        body = (
            "<h4>候选选题 · "
            + html.escape(topic["text"], quote=True)
            + "</h4>"
            + badge
            + render_sources(topic["sources"])
        )
        search = topic["text"] + " " + " ".join(s["title"] for s in topic["sources"])
        out.append(item_div("sec-action", "", search, 0, body))

    for pain in actions.get("pain_points") or []:
        items = "".join(
            "<li>"
            + html.escape(h["text"], quote=True)
            + ' <span class="meta">['
            + html.escape(h["title"], quote=True)
            + "]</span></li>"
            for h in pain["items"]
        )
        body = (
            "<h4>反复出现的痛点 · "
            + html.escape(pain["term"], quote=True)
            + "</h4>"
            + '<span class="pill">出现于 '
            + str(pain["video_count"])
            + " 条视频</span>"
            + "<ul>"
            + items
            + "</ul>"
            + '<div class="meta">口径：把 user_pain_points 切成条目后按主题词聚合，命中 ≥2 条视频才上榜。</div>'
        )
        search = pain["term"] + " " + " ".join(h["text"] for h in pain["items"])
        out.append(item_div("sec-action", "", search, 0, body))

    for gap in actions.get("gaps") or []:
        items = (
            "".join(
                "<li>"
                + html.escape(v["title"], quote=True)
                + ' <span class="meta mono">'
                + html.escape(v["platform"] or "平台缺失", quote=True)
                + " · "
                + html.escape(v["published_at"] or "", quote=True)
                + "</span></li>"
                for v in gap["videos"]
            )
            or "<li>" + MISSING_HTML + "</li>"
        )
        values = "、".join(gap["values"]) if gap["values"] else "—"
        body = (
            "<h4>补齐动作 · "
            + html.escape(gap["action"], quote=True)
            + "</h4>"
            + '<span class="pill bad">'
            + html.escape(gap["label"], quote=True)
            + "</span>"
            + '<span class="pill">'
            + str(gap["count"])
            + " 条</span>"
            + '<div class="meta">判定列 <code>'
            + html.escape(gap["column"], quote=True)
            + "</code>；"
            + html.escape(gap["wording"], quote=True)
            + "；实际取值 "
            + html.escape(values, quote=True)
            + "</div>"
            + "<ul>"
            + items
            + "</ul>"
            + '<div class="meta">本工具严格只读，只报缺口，不代为执行采集。</div>'
        )
        search = (
            gap["action"] + " " + gap["label"] + " " + " ".join(v["title"] for v in gap["videos"])
        )
        out.append(item_div("sec-action", "", search, 1, body, warn=True))

    if not out:
        return (
            '<div class="item warn" data-section="sec-action" data-missing="1">'
            + MISSING_HTML
            + "：窗口内既没有候选选题条目，也没有数据缺口动作。</div>"
        )
    return "".join(out)


def metric_head() -> str:
    return "".join(th(label, numeric) for _, label, numeric in METRIC_COLUMNS)


def metric_row(row: dict[str, Any]) -> str:
    return "".join(td(row.get(key), numeric) for key, _, numeric in METRIC_COLUMNS)


def metrics_html(metrics: dict[str, Any], availability: Availability) -> str:
    out = []
    if "video_metric_snapshots_readable" in availability.missing_views:
        out.append(
            item_div(
                "sec-metric",
                "",
                "视图缺失 video_metric_snapshots_readable",
                1,
                "<h4>视图 video_metric_snapshots_readable 不存在</h4>"
                + '<div class="meta">本库没有该视图，数据表现整节为空（不是全 0）。请运行 <code>'
                + html.escape(SYNC_COMMAND, quote=True)
                + "</code> 重建视图。</div>",
                warn=True,
            )
        )
    elif not any(r["snapshot_total"] for r in metrics["videos"]):
        out.append(
            item_div(
                "sec-metric",
                "",
                "无快照",
                1,
                "<h4>窗口内没有任何指标快照</h4><div class='meta'>"
                "下面每张表的播放/点赞等列都是「缺失」而不是 0：video_metric_snapshots_readable 里查不到"
                "这些视频的记录，需要先补采 T+N 指标。</div>",
                warn=True,
            )
        )
    if not metrics["numeric_columns_available"]:
        out.append(
            item_div(
                "sec-metric",
                "",
                "view_count 列缺失",
                1,
                "<h4>本库 video_metric_snapshots_readable 没有 view_count 等数值列</h4>"
                + '<div class="meta">这些列在本库不存在，相关统计已跳过并在表里标为缺失。请重跑 <code>'
                + html.escape(SYNC_COMMAND, quote=True)
                + "</code>。</div>",
                warn=True,
            )
        )

    platform_rows = (
        "".join(
            "<tr>"
            + td(p["platform"])
            + td(p["videos"], True)
            + td(p["snapshots"], True)
            + td(p["median_views"], True)
            + td(p["median_likes"], True)
            + td(p["with_views"], True)
            + "</tr>"
            for p in metrics["platforms"]
        )
        or "<tr><td colspan=6>" + MISSING_HTML + "</td></tr>"
    )
    out.append(
        "<h3>平台汇总（表头可点击排序）</h3><table data-sortable><thead><tr>"
        + th("平台")
        + th("窗口内视频", True)
        + th("快照数", True)
        + th("播放中位数", True)
        + th("点赞中位数", True)
        + th("有播放数值的视频", True)
        + "</tr></thead><tbody>"
        + platform_rows
        + "</tbody></table>"
    )

    out.append(
        item_div(
            "sec-metric",
            "",
            "检查点 数据来源 " + " ".join(c["label"] for c in metrics["checkpoints"]),
            0,
            "<h4>检查点与数据来源覆盖</h4><div class='meta'>检查点分布："
            + pills(metrics["checkpoints"])
            + "</div>"
            + '<div class="meta">数据来源分布：'
            + pills(metrics["data_sources"])
            + "</div>",
        )
    )

    out.append(
        "<h3>逐条视频的最新快照（缺失 ≠ 0）</h3><table data-sortable><thead><tr>"
        + metric_head()
        + "</tr></thead><tbody>"
        + (
            "".join("<tr>" + metric_row(r) + "</tr>" for r in metrics["videos"])
            or "<tr><td colspan=16>" + MISSING_HTML + "</td></tr>"
        )
        + "</tbody></table>"
    )

    if metrics["top_by_views"]:
        top = "".join(
            "<li>"
            + html.escape(r["title"], quote=True)
            + " <span class='meta mono'>"
            + esc_num(r["view_count"])
            + " 播放 / 互动率 "
            + (str(r["engagement"]) + "%" if r["engagement"] is not None else MISSING_HTML)
            + "（"
            + html.escape(r["platform"] or "平台缺失", quote=True)
            + "）</span></li>"
            for r in metrics["top_by_views"]
        )
        out.append(
            item_div(
                "sec-metric",
                "",
                "播放最高 " + " ".join(r["title"] for r in metrics["top_by_views"]),
                0,
                "<h4>窗口内播放量最高的 "
                + str(len(metrics["top_by_views"]))
                + " 条</h4><ul>"
                + top
                + "</ul>",
            )
        )
    return "".join(out)


def audience_html(audience: dict[str, Any], availability: Availability) -> str:
    out = []
    if not audience["comment_total"]:
        missing_view = "video_comments_readable" in availability.missing_views
        reason = (
            "本库缺少 video_comments_readable 视图"
            if missing_view
            else "窗口内视频没有关联到任何评论：评论表里 video_record_id / platform_video_id 都对不上窗口内的视频"
        )
        out.append(
            item_div(
                "sec-audience",
                "",
                "无评论样本",
                1,
                "<h4>观众信号没有评论样本</h4><div class='meta'>"
                + html.escape(reason, quote=True)
                + "。下面的痛点/争议仍来自视频记录自身的分析列。</div>",
                warn=True,
            )
        )

    high_value = (
        esc_num(audience["high_value_total"])
        if audience["high_value_total"] is not None
        else MISSING_HTML
    )
    out.append(
        '<div class="grid">'
        + '<div class="stat"><div class="val">'
        + str(audience["comment_total"])
        + "</div><small>窗口内评论条数</small></div>"
        + '<div class="stat"><div class="val">'
        + high_value
        + "</div><small>标记为高价值的评论</small></div>"
        + '<div class="stat"><div class="val">'
        + str(len(audience["videos_without_comments"]))
        + "</div><small>零评论的视频</small></div>"
        + '<div class="stat"><div class="val">'
        + (
            esc_num(audience["reply_total"])
            if audience["reply_total"] is not None
            else MISSING_HTML
        )
        + "</div><small>回复数合计（"
        + str(audience["reply_missing"])
        + " 条该列为 NULL）</small></div>"
        + "</div>"
    )

    if audience["videos_without_comments"]:
        items = "".join(
            "<li>"
            + html.escape(v["title"], quote=True)
            + " <span class='meta mono'>"
            + html.escape(v["platform"] or "平台缺失", quote=True)
            + "</span></li>"
            for v in audience["videos_without_comments"][:MAX_TOP_COMMENTS]
        )
        out.append(
            item_div(
                "sec-audience",
                "",
                "零评论 " + " ".join(v["title"] for v in audience["videos_without_comments"]),
                1,
                "<h4>零评论的视频（"
                + str(len(audience["videos_without_comments"]))
                + " 条）</h4><ul>"
                + items
                + "</ul>",
            )
        )

    out.append(
        item_div(
            "sec-audience",
            "",
            "性别 层级 " + " ".join(g["label"] for g in audience["genders"]),
            any(g["label"] == "NULL" for g in audience["genders"]),
            "<h4>观众构成（按评论列聚合）</h4>"
            + "<div class='meta'>user_gender："
            + pills(audience["genders"])
            + "</div>"
            + "<div class='meta'>comment_level："
            + pills(audience["levels"])
            + "</div>"
            + '<div class="meta">NULL 是旧记录没填，不是「未知性别」，两者不合并。</div>',
        )
    )

    rows = (
        "".join(
            "<tr>"
            + td(None if c["comment_text_missing"] else c["text"])
            + td(c["user_name"])
            + td(c["like_count"], True)
            + td(c["reply_count"], True)
            + td(c["is_high_value"], True)
            + td(c["commented_at"])
            + "<td class='meta'>"
            + html.escape(c["title"], quote=True)
            + (" · " + html.escape(c["creator"], quote=True) if c["creator"] else "")
            + "</td>"
            + "</tr>"
            for c in audience["top_comments"]
        )
        or "<tr><td colspan=7>" + MISSING_HTML + "</td></tr>"
    )
    out.append(
        item_div(
            "sec-audience",
            "",
            "高赞评论 " + " ".join(c["text"] for c in audience["top_comments"]),
            sum(1 for c in audience["top_comments"] if c["like_count"] is None),
            "<h4>评论样本（按 "
            + html.escape(audience["sorted_by"], quote=True)
            + " 降序，前 "
            + str(len(audience["top_comments"]))
            + " 条）</h4>"
            + "<table data-sortable><thead><tr>"
            + th("评论内容")
            + th("昵称")
            + th("点赞", True)
            + th("回复", True)
            + th("高价值", True)
            + th("评论时间")
            + th("所在视频")
            + "</tr></thead><tbody>"
            + rows
            + "</tbody></table>",
        )
    )

    for title, key in (
        ("用户痛点（videos_readable.user_pain_points）", "pain_points"),
        ("评论争议点（videos_readable.comment_controversies）", "controversies"),
        ("高赞评论总结（videos_readable.high_like_comment_summary）", "high_like_summaries"),
        ("代表评论（videos_readable.representative_comments）", "representative_comments"),
    ):
        entries = audience[key]
        if not entries:
            out.append(
                item_div(
                    "sec-audience",
                    "",
                    title,
                    1,
                    "<h4>" + html.escape(title, quote=True) + "</h4><div class='meta'>"
                    "窗口内没有任何视频填写该列（缺失，不是空字符串）。</div>",
                    warn=True,
                )
            )
            continue
        body_rows = "".join(
            "<li>" + html.escape(e["text"], quote=True) + "</li>" + render_sources(e["sources"], 4)
            for e in entries
        )
        out.append(
            item_div(
                "sec-audience",
                "",
                title + " " + " ".join(e["text"] for e in entries),
                0,
                "<h4>" + html.escape(title, quote=True) + "</h4><ul>" + body_rows + "</ul>",
            )
        )
    return "".join(out)


def takeaway_html(takeaways: dict[str, Any], availability: Availability) -> str:
    out = []
    if not takeaways["points"]:
        missing_col = not availability.has_column("videos_readable", "key_points")
        reason = (
            "本库 videos_readable 没有 key_points 列"
            if missing_col
            else "窗口内所有视频的 key_points 都是 NULL"
        )
        out.append(
            item_div(
                "sec-takeaway",
                "",
                "无要点",
                1,
                "<h4>没有可带走的观点条目</h4><div class='meta'>"
                + html.escape(reason, quote=True)
                + "。</div>",
                warn=True,
            )
        )
    for point in takeaways["points"]:
        badge = '<span class="pill hot">多条视频同观点</span>' if point["resonance"] else ""
        body = (
            "<h4>"
            + html.escape(point["text"], quote=True)
            + "</h4>"
            + badge
            + render_sources(point["sources"])
        )
        out.append(
            item_div(
                "sec-takeaway",
                "",
                point["text"] + " " + " ".join(s["title"] for s in point["sources"]),
                0,
                body,
            )
        )

    fields = "、".join(takeaways["fields_used"]) or "无可用列"
    out.append(
        item_div(
            "sec-takeaway",
            "",
            "工具 提及 " + " ".join(t["label"] for t in takeaways["tools"]),
            0 if takeaways["tools"] else 1,
            "<h4>被点名的工具 / 技术词</h4>"
            + pills(takeaways["tools"], "kind")
            + "<h3>书名号与引号里点名的对象</h3>"
            + pills(takeaways["quoted"])
            + '<div class="meta">口径：只统计窗口内视频的 '
            + html.escape(fields, quote=True)
            + " 文本里真实出现的拉丁 token 与 《》「」短语，次数是文本命中数，不代表推荐度。</div>",
        )
    )

    if takeaways["comment_notes"]:
        items = "".join(
            "<li>"
            + html.escape(n["text"], quote=True)
            + ' <span class="meta">['
            + html.escape(n["title"], quote=True)
            + "]</span></li>"
            for n in takeaways["comment_notes"]
        )
        out.append(
            item_div(
                "sec-takeaway",
                "",
                "观众洞察备注 " + " ".join(n["text"] for n in takeaways["comment_notes"]),
                0,
                "<h3>评论区的洞察备注（video_comments_readable.insight_notes）</h3><ul>"
                + items
                + "</ul>",
            )
        )
    return "".join(out)


def availability_html(availability: Availability, meta: dict[str, Any]) -> str:
    rows = []
    for view in VIEWS:
        present = view not in availability.missing_views
        missing_cols = availability.missing_columns.get(view, [])
        nulls = availability.null_counts.get(view, Counter())
        bad = availability.bad_numbers.get(view, Counter())
        null_text = (
            "、".join(html.escape(c, quote=True) + "×" + str(nulls[c]) for c in sorted(nulls))
            or "无"
        )
        bad_text = (
            "、".join(html.escape(c, quote=True) + "×" + str(bad[c]) for c in sorted(bad)) or "无"
        )
        total = availability.total_rows.get(view)
        badge = (
            '<span class="pill good">存在</span>'
            if present
            else '<span class="pill bad">缺失</span>'
        )
        rows.append(
            "<tr>"
            + td(view)
            + "<td>"
            + badge
            + "</td>"
            + td(total, True)
            + td(availability.fetched_rows.get(view, 0), True)
            + td("、".join(missing_cols) if missing_cols else None)
            + "<td class='meta'>"
            + null_text
            + "</td>"
            + "<td class='meta'>"
            + bad_text
            + "</td></tr>"
        )
    notes = availability.all_notes()
    note_html = (
        "".join("<li>" + html.escape(n, quote=True) + "</li>" for n in notes)
        or "<li>没有发现视图/列缺失，也没有无法定位的样本。</li>"
    )

    unplaced_html = ""
    if availability.unplaced_videos:
        unplaced_rows = "".join(
            "<tr>"
            + td(u.get("record_id"))
            + td(u.get("video_title"))
            + td(u.get("platform"))
            + td(u.get("published_at"))
            + td(u["reason"])
            + "</tr>"
            for u in availability.unplaced_videos[:MAX_UNPLACED_ROWS]
        )
        unplaced_html = (
            "<h3>无法定位到窗口的视频（"
            + str(len(availability.unplaced_videos))
            + " 条，最多列 "
            + str(MAX_UNPLACED_ROWS)
            + " 条，而不是丢掉）</h3><table data-sortable><thead><tr>"
            + th("record_id")
            + th("标题")
            + th("平台")
            + th("published_at 原值")
            + th("原因")
            + "</tr></thead><tbody>"
            + unplaced_rows
            + "</tbody></table>"
        )

    orphan_html = ""
    if availability.orphan_comments or availability.orphan_snapshots:
        orphan_html = item_div(
            "sec-availability",
            "",
            "关联不上 孤儿",
            1,
            "<h4>关联不上的证据</h4><div class='meta'>"
            + str(availability.orphan_comments)
            + " 条评论、"
            + str(availability.orphan_snapshots)
            + " 条快照的 video_record_id / platform_video_id 在窗口内找不到对应视频，"
            "未被计入上面五节。文档 :201 说明飞书删除的记录不会自动从本库清掉，孤儿是正常历史残留。</div>",
            warn=True,
        )

    summary = item_div(
        "sec-availability",
        "",
        "数据库 窗口 路径",
        0,
        '<div class="meta">镜像库：<code>'
        + html.escape(str(availability.db_path), quote=True)
        + "</code></div>"
        + '<div class="meta">窗口：<span class="mono">'
        + html.escape(meta["window_start"], quote=True)
        + '</span> → <span class="mono">'
        + html.escape(meta["window_end"], quote=True)
        + "</span>（"
        + str(meta["days"])
        + " 天，终点取自库内最新 published_at，不是系统时间）</div>"
        + '<div class="meta">窗口内视频 '
        + str(meta["video_count"])
        + " 条 / 库内视频总计 "
        + str(meta["video_total"])
        + " 条 / 涉及博主 "
        + shown(meta["creator_count"])
        + " 位</div>"
        + '<div class="meta">只读取的视图：'
        + html.escape("、".join(VIEWS), quote=True)
        + "</div>",
    )

    return (
        '<p class="lead">文档 :204 明确旧记录该列为 NULL 是缺失的真实表达，不该被默认值掩盖。'
        "所以本看板上「缺失」和 0 永远不是一回事。</p>"
        + summary
        + "<h3>视图与列体检</h3><table data-sortable><thead><tr>"
        + th("视图")
        + th("存在")
        + th("库内行数")
        + th("本工具读取行数", True)
        + th("缺失的期望列")
        + th("NULL 计数（已读取行）")
        + th("非 NULL 但数值解析失败")
        + "</tr></thead><tbody>"
        + "".join(rows)
        + "</tbody></table>"
        + "<h3>提示（同样已输出到 stderr）</h3>"
        + item_div(
            "sec-availability",
            "",
            "提示 availability notes",
            1 if notes else 0,
            "<ul>" + note_html + "</ul>",
            warn=bool(notes),
        )
        + orphan_html
        + unplaced_html
    )


def video_json_rows(dataset: dict[str, Any], metrics: dict[str, Any]) -> list[dict[str, Any]]:
    by_record = {row["record_id"]: row for row in metrics["videos"]}
    rows = []
    for video in dataset["videos"]:
        rid = plain(video.get("record_id"))
        metric = by_record.get(rid, {})
        rows.append(
            {
                "record_id": rid,
                "title": video.get("video_title"),
                "platform": video.get("platform"),
                "creator": creator_label(video, dataset["creator_map"]) or None,
                "published_at": video.get("published_at"),
                "comments": len(dataset["comments_by_video"].get(rid, [])),
                "snapshots": len(dataset["snapshots_by_video"].get(rid, [])),
                "views": metric.get("view_count"),
                "likes": metric.get("like_count"),
            }
        )
    return rows


def render_html(
    payload: dict[str, Any], availability: Availability, dataset: dict[str, Any]
) -> str:
    meta = payload["meta"]
    platforms = sorted(
        {plain(v.get("platform")) for v in dataset["videos"] if plain(v.get("platform"))}
    )
    options = "".join(
        '<option value="'
        + html.escape(p, quote=True)
        + '">'
        + html.escape(p, quote=True)
        + "</option>"
        for p in platforms
    )
    nav_buttons = "".join(
        '<button type="button" data-target="'
        + sid
        + ('" class="active">' if sid == "all" else '">')
        + html.escape(label, quote=True)
        + "</button>"
        for sid, label in SECTIONS
    )
    embedded = dict(payload)
    embedded["videos"] = video_json_rows(dataset, payload["metrics"])

    empty = (
        '<div class="item" data-section="sec-topic" data-missing="1">'
        + MISSING_HTML
        + "：窗口内没有跨视频共振的主题词（每条视频的主题词都只命中自己）。</div>"
    )

    parts = [
        '<!doctype html>\n<html lang="zh-CN">\n<head>\n<meta charset="utf-8">\n',
        '<meta name="viewport" content="width=device-width, initial-scale=1">\n',
        "<title>",
        html.escape("恐龙哥 · 内容情报看板", quote=True),
        "</title>\n",
        "<style>\n",
        CSS,
        "\n</style>\n</head>\n<body>\n",
        "<header>",
        "<h1>恐龙哥 · 内容情报看板</h1>",
        '<div class="sub">只读本地飞书 SQLite 镜像生成的旧版情报看板 · 生成于 <span class="mono">',
        html.escape(meta["generated_at"], quote=True),
        "</span> · 本文件自包含，双击即可离线打开</div>",
        '<div class="kv">',
        "<span>窗口 <b>",
        html.escape(meta["window_start"], quote=True),
        " → ",
        html.escape(meta["window_end"], quote=True),
        "</b>（",
        str(meta["days"]),
        " 天）</span>",
        "<span>窗口终点 <b>",
        html.escape(meta["window_end_source"], quote=True),
        "</b></span>",
        "<span>镜像库 <b>",
        html.escape(str(meta["db_path"]), quote=True),
        "</b></span>",
        "<span>数据可用性提示 <b>",
        str(meta["availability_notes"]),
        "</b> 条</span>",
        "</div>",
        '<div class="grid">',
        '<div class="stat"><div class="val">',
        str(meta["video_count"]),
        "</div><small>窗口内视频</small></div>",
        '<div class="stat"><div class="val">',
        shown(meta["creator_count"]),
        "</div><small>涉及博主主体</small></div>",
        '<div class="stat"><div class="val">',
        str(meta["comment_count"]),
        "</div><small>窗口内评论</small></div>",
        '<div class="stat"><div class="val">',
        str(meta["snapshot_count"]),
        "</div><small>窗口内指标快照</small></div>",
        '<div class="stat"><div class="val">',
        str(len(payload["topics"])),
        "</div><small>共振主题</small></div>",
        "</div>",
        "</header>\n",
        '<div class="toolbar">',
        '<nav id="nav">',
        nav_buttons,
        "</nav>",
        '<input id="q" type="search" placeholder="搜索标题 / 昵称 / 评论 / 要点" aria-label="搜索">',
        '<select id="platform" aria-label="按平台筛选"><option value="">全部平台</option>',
        options,
        "</select>",
        '<label class="switch"><input id="missonly" type="checkbox">只看含缺失的条目</label>',
        '<span class="meta">可见条目 <b id="visible-count">0</b> / ',
        str(len(dataset["videos"])),
        " 条视频</span>",
        "</div>\n<main>\n",
        '<section class="section" id="sec-topic"><h2>一、选题共振</h2>',
        '<p class="lead">口径：把窗口内每条视频的 关键要点 / 可拓展选题 / 内容摘要 / 标题 切成主题词',
        "（中文 2-gram + 拉丁 token，去停用词），同一主题词覆盖 ≥2 条视频即算共振，并区分",
        "「多位博主撞同一选题」与「同一博主跨平台分发」。这是确定性统计，不含推测。</p>",
        topic_html(payload["topics"]) or empty,
        "</section>\n",
        '<section class="section" id="sec-action"><h2>二、可执行动作</h2>',
        '<p class="lead">两类：候选选题（来自可拓展选题与反复出现的痛点）与数据补齐动作',
        "（来自状态列、评论计数和快照关联结果）。本工具只读，不会代为执行采集。</p>",
        action_html(payload["actions"]),
        "</section>\n",
        '<section class="section" id="sec-metric"><h2>三、数据表现</h2>',
        '<p class="lead">口径：每条视频取 video_metric_snapshots_readable 中 snapshot_at 最新的一条快照；',
        "「缺失」= 本库无此列或该行为 NULL，与 0 严格区分。互动率 = 点赞 / 播放，只在两者都有值时计算。</p>",
        metrics_html(payload["metrics"], availability),
        "</section>\n",
        '<section class="section" id="sec-audience"><h2>四、观众信号</h2>',
        '<p class="lead">口径：评论按 video_record_id 归到窗口内视频，匹配不上的一律不计入，',
        "并在「数据可用性」里报出条数。视频自身的痛点/争议列仍照常展示。</p>",
        audience_html(payload["audience"], availability),
        "</section>\n",
        '<section class="section" id="sec-takeaway"><h2>五、值得带走的观点 / 工具</h2>',
        '<p class="lead">观点取自关键要点并按来源去重；工具取自文本里真实出现的拉丁 token 与',
        "《》「」短语，次数是命中次数而非推荐度。</p>",
        takeaway_html(payload["takeaways"], availability),
        "</section>\n",
        '<section class="section" id="sec-videolist"><h2>附：窗口内视频清单（由内嵌 JSON 在浏览器里生成）</h2>',
        '<p class="lead">共 <b id="video-list-count">0</b> 条。本节只证明内嵌 JSON 可被直接消费：',
        "所有文本都通过 textContent 写入，未使用 innerHTML。禁用 JavaScript 时本节为空，上方五节仍完整可读。</p>",
        '<div class="item" id="video-list" data-section="sec-videolist" data-missing="0">',
        "<noscript>需要启用 JavaScript 才能生成本节；上方五节不依赖脚本。</noscript></div></section>\n",
        '<section class="section" id="sec-availability"><h2>数据可用性</h2>',
        availability_html(availability, meta),
        "</section>\n</main>\n",
        "<footer>",
        "<div>生成命令：<code>python generate_creator_insight_dashboard.py --days ",
        str(meta["days"]),
        ' --output "',
        html.escape(str(meta["output_path"]), quote=True),
        '"</code></div>',
        "<div>这是旧的独立展示工具：不属于飞书本地同步管线，未注册进 launcher，",
        "它的退出码不会影响 <code>",
        html.escape(SYNC_COMMAND, quote=True),
        "</code> 的成功状态。</div>",
        "<div>数据来源：",
        html.escape("、".join(VIEWS), quote=True),
        "（不读「内容作品」，不读「爬取任务日志」）。</div></footer>\n",
        '<script id="dashboard-data" type="application/json">',
        json_for_script(embedded),
        "</script>\n",
        "<script>\n",
        JS,
        "\n</script>\n</body>\n</html>\n",
    ]
    return "".join(parts)


# --------------------------------------------------------------------------- #
# 入口
# --------------------------------------------------------------------------- #


def build_payload(
    dataset: dict[str, Any],
    availability: Availability,
    output_path: Path,
) -> dict[str, Any]:
    videos = dataset["videos"]
    creator_ids = {
        plain(v.get("creator_record_id")) for v in videos if plain(v.get("creator_record_id"))
    }
    # 只数真正能在 creators_readable 里查到的：悬空的 creator_record_id 不是博主主体，计进来就是虚报。
    linked_creators = {cid for cid in creator_ids if cid in dataset["creator_map"]}
    has_creator_link = availability.has_column("videos_readable", "creator_record_id")
    if not has_creator_link:
        availability.notes.append(
            "videos_readable 没有 creator_record_id 列，无法把视频关联到博主主体，"
            "「涉及博主主体」计为缺失而不是 0。"
        )
    elif len(linked_creators) < len(creator_ids):
        availability.notes.append(
            str(len(creator_ids) - len(linked_creators))
            + " 个 creator_record_id 在 creators_readable 里查不到"
            "（博主记录没同步或链接已失效），「涉及博主主体」只数查得到的 "
            + str(len(linked_creators))
            + " 位。"
        )
    topics = build_topic_resonance(dataset, availability)
    actions = build_actions(dataset, availability)
    metrics = build_metrics(dataset, availability)
    audience = build_audience(dataset, availability)
    takeaways = build_takeaways(dataset, availability)
    meta = {
        "tool": "generate_creator_insight_dashboard.py",
        "contract": "FEISHU_LOCAL_DATABASE.md 「旧内容情报看板」（174-194 行）",
        "generated_at": datetime.now().strftime(TS_FORMAT),
        "db_path": str(availability.db_path),
        "output_path": str(output_path),
        "days": dataset["days"],
        "window_start": fmt_ts(dataset["window_start"]),
        "window_end": fmt_ts(dataset["window_end"]),
        "window_end_source": "库内最新 published_at",
        "video_count": len(videos),
        "video_total": len(dataset["videos_all"]),
        "creator_count": len(linked_creators) if has_creator_link else None,
        "comment_count": sum(len(v) for v in dataset["comments_by_video"].values()),
        "snapshot_count": sum(len(v) for v in dataset["snapshots_by_video"].values()),
        "views_read": list(VIEWS),
        "availability_notes": len(availability.all_notes()),
    }
    return {
        "meta": meta,
        "topics": topics,
        "actions": actions,
        "metrics": metrics,
        "audience": audience,
        "takeaways": takeaways,
        "availability": {
            "missing_views": availability.missing_views,
            "missing_columns": availability.missing_columns,
            "total_rows": availability.total_rows,
            "fetched_rows": availability.fetched_rows,
            "null_counts": {
                view: dict(counter) for view, counter in availability.null_counts.items()
            },
            "bad_numbers": {
                view: dict(counter) for view, counter in availability.bad_numbers.items()
            },
            "orphan_comments": availability.orphan_comments,
            "orphan_snapshots": availability.orphan_snapshots,
            "unplaced_videos": availability.unplaced_videos,
            "notes": availability.all_notes(),
        },
    }


def refuse_downloads(output_path: Path) -> str:
    """唯一允许写入的是 --output；downloads/ 是只读存档区，绝不落文件。"""
    try:
        output_path.relative_to((ROOT / "downloads").resolve())
        return "拒绝把产物写进只读区 downloads/：" + str(output_path)
    except ValueError:
        return ""


def emit_availability(availability: Availability) -> None:
    """不出图的路径也要把缺了什么说到：文档承诺缺失的视图/列同时进 HTML 与 stderr。"""
    for note in availability.all_notes():
        print("[数据可用性] " + note, file=sys.stderr)


def generate(db_path: Path, days: int, output_path: Path) -> int:
    blocked = refuse_downloads(output_path)
    if blocked:
        print("[错误] " + blocked, file=sys.stderr)
        return EXIT_ENVIRONMENT
    try:
        conn = connect_readonly(db_path)
    except ContractError as exc:
        print("[错误] " + str(exc), file=sys.stderr)
        return EXIT_ENVIRONMENT
    try:
        try:
            # 视图缺没缺要先问清楚：缺视图是环境坏了（退出码 1），和「库里真的没视频」（退出码 2）
            # 是两件事，混在一起会让人去查数据，而该查的是同步有没有跑过。
            missing_views = [view for view in VIEWS if not view_exists(conn, view)]
            hard_missing = [view for view in REQUIRED_VIEWS if view in missing_views]
            if hard_missing:
                print(
                    "[错误] 镜像库缺少必需的只读视图："
                    + "、".join(hard_missing)
                    + "\n       库路径："
                    + str(db_path)
                    + (
                        "\n       本工具要读的四个视图目前缺：" + "、".join(missing_views)
                        if len(missing_views) != len(hard_missing)
                        else ""
                    )
                    + "\n       → *_readable 视图每次同步都会按最新定义重建，请重跑 `"
                    + SYNC_COMMAND
                    + "`；"
                    '\n         可用 `sqlite3 -header "<db>" ".schema videos_readable"` 自查视图定义。'
                    + "\n       这是环境问题，不是「库里没有视频」，未写任何输出。",
                    file=sys.stderr,
                )
                return EXIT_ENVIRONMENT
            dataset, availability = load_dataset(conn, days)
        except ContractError as exc:
            print("[错误] " + str(exc), file=sys.stderr)
            return EXIT_ENVIRONMENT
        except sqlite3.Error as exc:
            print(
                "[错误] 只读查询镜像库失败：" + str(exc) + "\n"
                "       → 先确认 `" + SYNC_COMMAND + "` 最近一次是否成功，"
                "再看 downloads/manifests/ 最新的 *-feishu-local-pipeline.json。",
                file=sys.stderr,
            )
            return EXIT_ENVIRONMENT
        availability.db_path = db_path

        if not dataset["videos_all"]:
            emit_availability(availability)
            print(
                "[错误] " + str(db_path) + " 的 videos_readable 里一条视频都没有，"
                "无法按文档要求用「库内最新 published_at」作为窗口终点。\n"
                "       → 先运行 `" + SYNC_COMMAND + "` 完成飞书 Base 本地同步后重跑本工具；"
                "本工具不会输出一份看着成功的空看板。",
                file=sys.stderr,
            )
            return EXIT_NO_SAMPLE
        if dataset["window_end"] is None:
            emit_availability(availability)
            print(
                "[错误] videos_readable 有 "
                + str(len(dataset["videos_all"]))
                + " 条视频，但所有 published_at 都是 NULL 或无法解析的格式，无法确定窗口终点。\n"
                "       → 补齐飞书侧「发布时间」后重跑 `"
                + SYNC_COMMAND
                + "`；未写任何输出。前几条：",
                file=sys.stderr,
            )
            for item in availability.unplaced_videos[:5]:
                print(
                    "       - record_id="
                    + plain(item.get("record_id"))
                    + " 标题="
                    + (snippet(item.get("video_title"), 40) or "（缺失）")
                    + " published_at="
                    + repr(item.get("published_at")),
                    file=sys.stderr,
                )
            return EXIT_NO_SAMPLE
        if not dataset["videos"]:
            emit_availability(availability)
            print(
                "[错误] 窗口 "
                + fmt_ts(dataset["window_start"])
                + " → "
                + fmt_ts(dataset["window_end"])
                + "（"
                + str(days)
                + " 天）内没有命中任何视频。窗口终点本身就是库内最新一条视频的"
                "发布时间，正常不可能为空，请检查 --days（当前 " + str(days) + "）；未写任何输出。",
                file=sys.stderr,
            )
            return EXIT_NO_SAMPLE

        try:
            payload = build_payload(dataset, availability, output_path)
            document = render_html(payload, availability, dataset)
        except sqlite3.Error as exc:
            emit_availability(availability)
            print("[错误] 归纳阶段读库失败：" + str(exc), file=sys.stderr)
            return EXIT_ENVIRONMENT

        try:
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_text(document, encoding="utf-8")
        except OSError as exc:
            print(
                "[错误] 写入 HTML 失败：" + str(output_path) + "（" + str(exc) + "）",
                file=sys.stderr,
            )
            return EXIT_ENVIRONMENT

        for note in payload["availability"]["notes"]:
            print("[数据可用性] " + note, file=sys.stderr)
        meta = payload["meta"]
        action_total = (
            len(payload["actions"]["topics"])
            + len(payload["actions"]["pain_points"])
            + len(payload["actions"]["gaps"])
        )
        creator_text = "缺失" if meta["creator_count"] is None else str(meta["creator_count"])
        print(
            "[完成] 旧内容情报看板 · 窗口 "
            + meta["window_start"]
            + " → "
            + meta["window_end"]
            + "（"
            + str(meta["days"])
            + " 天，终点取自库内最新 published_at，非系统时间）"
        )
        print(
            "       样本 视频 "
            + str(meta["video_count"])
            + " / 博主 "
            + creator_text
            + " / 评论 "
            + str(meta["comment_count"])
            + " / 快照 "
            + str(meta["snapshot_count"])
            + "（库内视频总计 "
            + str(meta["video_total"])
            + "）"
        )
        print(
            "       归纳 共振主题 "
            + str(len(payload["topics"]))
            + " 组 / 动作条目 "
            + str(action_total)
            + " 项 / 评论样本 "
            + str(len(payload["audience"]["top_comments"]))
            + " 条 / 带走要点 "
            + str(len(payload["takeaways"]["points"]))
            + " 条"
        )
        print(
            "       数据可用性提示 "
            + str(meta["availability_notes"])
            + " 条（详见 HTML「数据可用性」一节，已同步输出到 stderr）"
        )
        print("       输出：" + str(output_path.resolve()))
        return EXIT_OK
    finally:
        conn.close()


def build_parser() -> argparse.ArgumentParser:
    epilog = (
        "示例：\n"
        "  python .\\generate_creator_insight_dashboard.py\n"
        "  python .\\generate_creator_insight_dashboard.py --days 7 "
        "--output .\\outputs\\creator-insight-dashboard.html\n\n"
        "窗口终点固定取库内最新 published_at（不是系统时间），起点 = 终点 - --days。\n"
        "只读保证：SQLite URI mode=ro；不写 SQLite、不写 downloads/，唯一写入目标是 --output。\n"
        "定位：旧的独立展示工具，不属于飞书本地同步管线，未注册进 launcher，退出码不影响同步成功状态。\n"
        "退出码：0 已生成 / 1 环境或契约缺失 / 2 无可用样本（不写 HTML）。"
    )
    parser = argparse.ArgumentParser(
        prog="generate_creator_insight_dashboard.py",
        description="旧内容情报看板：把本地飞书 SQLite 镜像最近 N 天的视频归纳成五节单文件离线 HTML（严格只读）。",
        epilog=epilog,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--days", type=int, default=7, help="回溯天数（默认 7）")
    parser.add_argument(
        "--output",
        default=str(DEFAULT_OUTPUT_PATH),
        help="HTML 输出路径（默认 "
        + str(DEFAULT_OUTPUT_PATH.relative_to(ROOT))
        + "；相对路径按项目根解析）",
    )
    parser.add_argument(
        "--db",
        default=str(DEFAULT_DB_PATH),
        help="飞书本地镜像库路径（默认 " + str(DEFAULT_DB_PATH.relative_to(ROOT)) + "）",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    days = int(args.days)
    if days <= 0:
        print(
            "[错误] --days 必须是正整数（当前 "
            + str(days)
            + "）。窗口终点是库内最新 published_at，"
            "0 或负数窗口没有意义。",
            file=sys.stderr,
        )
        return EXIT_NO_SAMPLE
    db_path = Path(args.db)
    if not db_path.is_absolute():
        db_path = ROOT / db_path
    output_path = Path(args.output)
    if not output_path.is_absolute():
        output_path = ROOT / output_path
    return generate(db_path.resolve(), days, output_path.resolve())


if __name__ == "__main__":
    raise SystemExit(main())
