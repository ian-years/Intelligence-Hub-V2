#!/usr/bin/env python3
# ruff: noqa
# TODO(v2-adapt): V1 的飞书 Base 镜像库写入器，未适配 —— 它**就是**"独立镜像库"这件事本身：
# 目标库是第二个 SQLite（<ROOT>/downloads/feishu-base/feishu-base.sqlite3，故意不开 WAL，因为 V1 的
# launcher_server 以 mode=ro URI 读它），五张原始表 + *_readable 视图的 DDL 唯一真源在本文件里（V2
# 的真源是 storage/schema.py + Alembic），游标/历史写在自带的 sync_state / sync_runs（V2 已有
# task_runs + manifests），还有第三份 ProcessLock。适配要做的那件事就是 T5.1 的"废镜像库"：表进
# schema.py 出迁移、写入走 Repository、记账交回 TaskRunner，这里只留"读飞书分页 + 摊平字段"。
# 形状说明：ruff 的全局豁免写在第 2 行（第 1 行留给 shebang，否则 `./x.py` 跑不了）。
"""飞书 Base 五张表 -> 本地 SQLite 增量镜像（原始层 + AI 可读视图层）。

契约来源：``FEISHU_LOCAL_DATABASE.md``（唯一规格说明书）。

* 目标库：``downloads/feishu-base/feishu-base.sqlite3``
* 原始表：``creators`` / ``videos`` / ``favorites`` / ``video_comments`` /
  ``video_metric_snapshots``，公共列 ``record_id`` / ``fields_json`` / ``payload_hash`` /
  ``modified_at`` / ``first_seen_at`` / ``last_synced_at``（文档 :29-38）
* 可读视图：``*_readable``，每次运行都按本文件内的最新定义重建（文档 :27、:44-72）。
  **视图定义的唯一真源就在本文件**，不要在数据库里手工改视图。
* 状态表：``sync_state(table_key, cursor_modified_at, updated_at)``；
  历史表：``sync_runs(started_at, ended_at, status, mode, summary_json, manifest_path)``。
* 增量游标来自飞书 ``最后修改时间``，保留 2 分钟重叠窗口，重叠记录幂等 upsert（文档 :199）。
* 原子性：任一飞书表读取失败时，五张本地表和游标都不部分推进（文档 :229）——
  先把五张表全部读完，再在一个事务里统一提交。
* 飞书已删除的记录不会自动从本地删除（文档 :201）。

离线可验证（不需要飞书凭据）：``--init-schema`` 建表并重建全部视图，随后校验消费方
（``launcher/launcher_server.py`` 的 list_creators / list_videos / read_transcript，以及文档
:74-110 的示例查询）所需的列是否都存在。真实网络同步需要 ``feishu-base-config.json`` 与
``lark-cli``；两者缺失时本脚本给出可执行的修复提示并以非零码退出，绝不伪造同步成功。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sqlite3
import sys
import time
import uuid
from dataclasses import dataclass, field as dc_field
from datetime import datetime
from pathlib import Path
from typing import Any, Sequence

ROOT: Path = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from intelligence_hub_v2.ported.feishu import feishu_core as fc  # noqa: E402  (reused, never re-implemented)
from intelligence_hub_v2.ported.feishu import lark_cli_runtime  # noqa: E402
from intelligence_hub_v2.ported.v1_shared.durable_progress import emit_durable  # noqa: E402
from intelligence_hub_v2.ported.v1_shared.utils import now_str as _utils_now_str  # noqa: E402  (统一时间文本口径)
from intelligence_hub_v2.ported.v1_shared.utils import ts_slug as _utils_ts_slug  # noqa: E402  (统一 manifest 文件戳)

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

TIME_FORMAT = "%Y-%m-%d %H:%M:%S"
DB_RELATIVE_PATH = Path("downloads") / "feishu-base" / "feishu-base.sqlite3"
DEFAULT_DB_PATH: Path = ROOT / DB_RELATIVE_PATH
MANIFEST_ROOT: Path = ROOT / "downloads" / "manifests"
LOG_DIR: Path = ROOT / "downloads" / "logs"

#: 增量游标的重叠窗口（秒），文档 :199
OVERLAP_WINDOW_SECONDS = 120
#: 飞书系统"最后修改时间"字段的可能命名（按优先级）
MODIFIED_TIME_FIELD_CANDIDATES = ("最后修改时间", "修改时间", "最后编辑时间")
#: 单页记录数，与 :func:`feishu_core.list_records` 保持一致
PAGE_LIMIT = 200
#: lark-cli 单次请求超时（秒）
DEFAULT_REQUEST_TIMEOUT = 120
#: 相邻飞书请求之间的强制间隔（节流）
DEFAULT_THROTTLE_SECONDS = 0.25
#: 每 N 页打印一次进度
PROGRESS_EVERY_PAGES = 10

#: 视图公共列（文档 :42）
VIEW_TAIL_COLUMNS = (
    "modified_at AS feishu_modified_at",
    "first_seen_at",
    "last_synced_at",
    "fields_json",
)

#: 原始表公共列（文档 :29-38）
RAW_TABLE_COLUMNS_SQL = """
    record_id      TEXT PRIMARY KEY,
    fields_json    TEXT NOT NULL,
    payload_hash   TEXT NOT NULL,
    modified_at    TEXT,
    first_seen_at  TEXT NOT NULL,
    last_synced_at TEXT NOT NULL
"""

#: 允许的列取值形态（决定生成的 SQL 表达式）
TEXT_CONCAT = "text_concat"  # 富文本：拼接全部片段
TEXT_FIRST = "text_first"  # 纯文本/ID：取首个值
NUMBER = "number"  # 数字
BOOL = "bool"  # 复选框 -> 1/0/NULL
DATETIME = "datetime"  # 毫秒时间戳或日期文本 -> YYYY-MM-DD HH:MM:SS
SELECT_FIRST = "select_first"  # 多选字段便捷列：首个值（文档 :202）
URL = "url"  # 链接字段：优先 link（文档 :202）
LINK_FIRST = "link_first"  # 关联字段：首个记录 ID（文档 :203）
JSON_RAW = "json_raw"  # 完整值列（文档 :202、:203）
COUNT = "count"  # 关联记录数（文档 :203）
_IDENT_RE = re.compile(r"^[a-z][a-z0-9_]*$")


@dataclass(frozen=True)
class Field:
    """一个可读视图列 -> 一个飞书字段名的映射。

    ``aliases`` 只在飞书侧存在历史命名差异时使用；生成表达式会对各候选名做
    ``COALESCE``，全部缺失时列值为 ``NULL``（文档 :204：缺失是真实表达，不用默认值掩盖）。
    """

    name: str
    feishu_name: str
    kind: str
    aliases: tuple[str, ...] = ()

    @property
    def feishu_names(self) -> tuple[str, ...]:
        return (self.feishu_name,) + self.aliases


@dataclass(frozen=True)
class TableSpec:
    """一张飞书 Base 表在本地库中的完整定义。"""

    key: str  # sync_state.table_key / 本脚本的表标识
    table: str  # 本地原始表名
    view: str  # 本地可读视图名
    config_key: str  # feishu-base-config.json 的 tables.<key>
    label: str  # 人话名称，用于日志与 manifest
    fields: tuple[Field, ...] = dc_field(default_factory=tuple)

    @property
    def used_feishu_fields(self) -> tuple[str, ...]:
        seen: dict[str, None] = {}
        for item in self.fields:
            for name in item.feishu_names:
                seen.setdefault(name, None)
        return tuple(seen)


# ---------------------------------------------------------------------------
# 视图列清单：逐列对照 FEISHU_LOCAL_DATABASE.md :44-72
# ---------------------------------------------------------------------------

CREATORS_SPEC = TableSpec(
    key="creators",
    table="creators",
    view="creators_readable",
    config_key="creators",
    label="博主",
    fields=(
        Field("creator_name", "博主名称", TEXT_CONCAT),
        Field("platform", "平台", SELECT_FIRST),
        Field("platforms_json", "平台", JSON_RAW),  # 文档 :42 的 platform/platforms_json 示例
        Field("platform_user_id", "平台用户ID", TEXT_FIRST),
        Field("homepage_url", "主页链接", URL),
        Field("is_tracking", "是否持续跟踪", BOOL),
        Field("follower_count", "粉丝数", NUMBER, ("粉丝数量",)),
        Field("follower_count_display", "粉丝数展示", TEXT_CONCAT, ("粉丝数（展示）",)),
        Field("xhs_likes_and_favorites_count", "小红书点赞收藏数", NUMBER),
        Field("video_collection_strategy", "视频采集策略", SELECT_FIRST),
        Field("cross_platform_identity", "跨平台主体标识", TEXT_CONCAT),
        Field("category_tags_json", "分类标签", JSON_RAW),
        Field("sources_json", "来源", JSON_RAW),
        Field("notes", "备注", TEXT_CONCAT),
        Field("last_collected_at", "最近采集时间", DATETIME),
        Field("related_videos_json", "关联视频", JSON_RAW),
        Field("related_video_count", "关联视频", COUNT),
    ),
)

VIDEOS_SPEC = TableSpec(
    key="videos",
    table="videos",
    view="videos_readable",
    config_key="videos",
    label="视频",
    fields=(
        Field("video_title", "视频标题", TEXT_CONCAT),
        Field("platform", "平台", SELECT_FIRST),
        Field("platforms_json", "平台", JSON_RAW),
        Field("platform_video_id", "平台视频ID", TEXT_FIRST),
        Field("video_url", "视频链接", URL),
        Field("creator_record_id", "关联博主", LINK_FIRST),
        Field("published_at", "发布时间", DATETIME),
        Field("duration_seconds", "时长秒", NUMBER),
        Field("content_summary", "内容摘要", TEXT_CONCAT),
        Field("key_points", "关键要点", TEXT_CONCAT),
        Field("high_like_comment_summary", "高赞评论总结", TEXT_CONCAT, ("高赞评论归纳",)),
        Field("user_pain_points", "用户痛点", TEXT_CONCAT),
        Field("comment_controversies", "评论争议点", TEXT_CONCAT),
        Field("expandable_topics", "可拓展选题", TEXT_CONCAT),
        Field("representative_comments", "代表评论", TEXT_CONCAT),
        Field("transcript_document_url", "视频口播稿", URL, ("口播稿链接",)),
        Field("cleaned_transcript_path", "清洗文案路径", TEXT_FIRST),
        Field("transcript_status", "转写状态", SELECT_FIRST),
        Field("video_download_status", "视频下载状态", SELECT_FIRST),
        Field("comment_fetch_status", "评论抓取状态", SELECT_FIRST),
        Field("fetched_comment_count", "已抓评论数", NUMBER),
        Field("related_metric_snapshots_json", "关联数据快照", JSON_RAW),
        Field("related_metric_snapshot_count", "关联数据快照", COUNT),
    ),
)

FAVORITES_SPEC = TableSpec(
    key="favorites",
    table="favorites",
    view="favorites_readable",
    config_key="favorites",
    label="收藏",
    fields=(
        Field("favorite_name", "收藏夹名称", TEXT_CONCAT),
        Field("platform", "平台", SELECT_FIRST),
        Field("platforms_json", "平台", JSON_RAW),
        Field("content_type", "内容类型", SELECT_FIRST),
        Field("favorite_url", "收藏夹链接", URL),
        Field("is_enabled", "是否启用", BOOL),
        Field("extraction_batch_size", "提取批量", NUMBER),
        Field("extraction_status", "提取状态", SELECT_FIRST),
        Field("extracted_count", "已提取数", NUMBER),
        Field("new_pending_download_count", "新增待下载数", NUMBER),
        Field("last_extracted_at", "最近提取时间", DATETIME),
        Field("extraction_manifest_path", "提取清单路径", TEXT_FIRST),
        Field("error_message", "错误信息", TEXT_CONCAT),
        Field("notes", "备注", TEXT_CONCAT),
        Field("related_videos_json", "关联视频", JSON_RAW),
        Field("related_video_count", "关联视频", COUNT),
    ),
)

VIDEO_COMMENTS_SPEC = TableSpec(
    key="video_comments",
    table="video_comments",
    view="video_comments_readable",
    config_key="video_comments",
    label="视频评论",
    fields=(
        Field("comment_id", "评论ID", TEXT_FIRST),
        Field("platform", "平台", SELECT_FIRST),
        Field("platforms_json", "平台", JSON_RAW),
        Field("platform_video_id", "平台视频ID", TEXT_FIRST),
        Field("video_record_id", "关联视频", LINK_FIRST),
        Field("comment_text", "评论内容", TEXT_CONCAT),
        Field("user_id", "用户ID", TEXT_FIRST),
        Field("user_name", "用户名", TEXT_FIRST),
        Field("user_gender", "用户性别", SELECT_FIRST),
        Field("user_signature", "用户签名", TEXT_CONCAT),
        Field("avatar_url", "头像链接", URL),
        Field("comment_level", "评论层级", NUMBER),
        Field("root_comment_id", "根评论ID", TEXT_FIRST),
        Field("parent_comment_id", "父评论ID", TEXT_FIRST),
        Field("commented_at", "评论时间", DATETIME),
        Field("like_count", "点赞数", NUMBER),
        Field("reply_count", "回复数", NUMBER),
        Field("is_high_value", "是否高价值", BOOL),
        Field("comment_tags_json", "评论标签", JSON_RAW),
        Field("insight_notes", "洞察备注", TEXT_CONCAT),
        Field("collected_at", "采集时间", DATETIME),
    ),
)

METRIC_SNAPSHOTS_SPEC = TableSpec(
    key="video_metric_snapshots",
    table="video_metric_snapshots",
    view="video_metric_snapshots_readable",
    config_key="video_metric_snapshots",
    label="视频数据快照",
    fields=(
        Field("snapshot_key", "快照唯一键", TEXT_FIRST),
        Field("video_record_id", "关联视频", LINK_FIRST),
        Field("content_work_record_id", "关联内容作品", LINK_FIRST),
        Field("platform", "平台", SELECT_FIRST),
        Field("platforms_json", "平台", JSON_RAW),
        Field("video_title", "视频标题", TEXT_CONCAT),
        Field("checkpoint", "检查点", SELECT_FIRST),
        Field("snapshot_at", "快照时间", DATETIME),
        Field("target_snapshot_at", "目标快照时间", DATETIME),
        Field("hours_since_publish", "发布后小时数", NUMBER),
        Field("collection_delay_minutes", "采集延迟分钟", NUMBER),
        Field("view_count", "播放量", NUMBER),
        Field("like_count", "点赞量", NUMBER),
        Field("comment_count", "评论数", NUMBER),
        Field("favorite_count", "收藏数", NUMBER),
        Field("share_count", "分享数", NUMBER),
        Field("coin_count", "投币数", NUMBER),
        Field("danmaku_count", "弹幕数", NUMBER),
        Field("follower_count_snapshot", "粉丝数快照", NUMBER),
        Field("data_source", "数据来源", SELECT_FIRST),
        Field("notes", "备注", TEXT_CONCAT),
        # 文档 :203：需要全部关联时使用 related_videos_json / related_content_works_json
        Field("related_videos_json", "关联视频", JSON_RAW),
        Field("related_content_works_json", "关联内容作品", JSON_RAW),
    ),
)

#: 口播稿可读版语料表（文档 :54-58）。它不是 Base 原始镜像层，
#: 但表结构与可读视图定义同样以本文件为唯一真源，由口播稿提取程序写数据。
TRANSCRIPT_MIRROR_TABLE = "video_transcript_mirrors"
TRANSCRIPT_MIRROR_VIEW = "video_transcript_mirrors_readable"
TRANSCRIPT_MIRROR_COLUMNS = (
    "video_record_id",
    "document_url",
    "document_id",
    "revision_id",
    "local_markdown_path",
    "content_scope",
    "extractor_version",
    "content_sha256",
    "content_bytes",
    "source_content_sha256",
    "source_content_bytes",
    "sync_status",
    "error_message",
    "checked_at",
    "mirrored_at",
    "source_video_modified_at",
)

TABLE_SPECS: tuple[TableSpec, ...] = (
    CREATORS_SPEC,
    VIDEOS_SPEC,
    FAVORITES_SPEC,
    VIDEO_COMMENTS_SPEC,
    METRIC_SNAPSHOTS_SPEC,
)
TABLE_SPECS_BY_KEY: dict[str, TableSpec] = {spec.key: spec for spec in TABLE_SPECS}


# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------


def now_text() -> str:
    """统一时间文本口径 ``YYYY-MM-DD HH:MM:SS``（本地时间），复用 utils.now_str。"""
    return _utils_now_str()


def ts_slug() -> str:
    """manifest 文件名用的 ``YYYYMMDD-HHMMSS`` 戳，复用 utils.ts_slug。"""
    return _utils_ts_slug()


def relative_posix(path: Path) -> str:
    """相对仓库根的 POSIX 路径；不在仓库内则退回绝对 POSIX 路径。"""
    resolved = Path(path).resolve()
    try:
        return resolved.relative_to(ROOT).as_posix()
    except ValueError:
        return resolved.as_posix()


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _ident(name: str, kind: str = "标识符") -> str:
    """DDL 标识符白名单校验：只允许代码内常量，不接受任何运行时/用户数据。"""
    if not _IDENT_RE.match(name):
        raise ValueError(f"非法{kind}: {name!r}")
    return name


def _sql_literal(text: str) -> str:
    """把常量转成 SQL 字符串字面量（单引号转义）。"""
    return "'" + str(text).replace("'", "''") + "'"


def json_path(feishu_name: str, *, index: int | None = None, key: str | None = None) -> str:
    """``fields_json`` 中某个飞书字段的 JSON path 常量（含 SQL 引号）。"""
    path = "$." + json.dumps(feishu_name, ensure_ascii=False)
    if index is not None:
        path += f"[{index}]"
    if key is not None:
        path += "." + json.dumps(key, ensure_ascii=False)
    return _sql_literal(path)


# json_each 的当前元素 -> 文本。元素可能是字符串、数字，或 {text|name|link|value} 对象。
_ELEMENT_TEXT = (
    "CASE j.type "
    "WHEN 'object' THEN COALESCE("
    "json_extract(j.value, '$.text'), "
    "json_extract(j.value, '$.name'), "
    "json_extract(j.value, '$.link'), "
    "json_extract(j.value, '$.value')) "
    "ELSE CAST(j.value AS TEXT) END"
)


def _core_expr(
    feishu_name: str,
    *,
    array_mode: str,
    object_keys: Sequence[str] = ("text", "name", "link", "value"),
) -> str:
    """把飞书字段值的各种返回形态（标量 / 片段数组 / 对象）收敛成一段文本。

    ``array_mode`` 为 ``first``（取首个非空片段，用于多选/ID/链接）或
    ``concat``（拼接全部片段，用于富文本正文）。
    """
    path = json_path(feishu_name)
    if array_mode == "first":
        array_branch = (
            f"(SELECT {_ELEMENT_TEXT} FROM json_each(fields_json, {path}) AS j "
            f"WHERE COALESCE({_ELEMENT_TEXT}, '') <> '' LIMIT 1)"
        )
    else:
        array_branch = (
            f"(SELECT group_concat({_ELEMENT_TEXT}, '') FROM json_each(fields_json, {path}) AS j)"
        )
    object_branch = (
        "COALESCE("
        + ", ".join(
            f"json_extract(fields_json, {json_path(feishu_name, key=key)})" for key in object_keys
        )
        + ")"
    )
    return (
        f"CASE json_type(fields_json, {path}) "
        f"WHEN 'array' THEN {array_branch} "
        f"WHEN 'object' THEN {object_branch} "
        f"WHEN 'text' THEN json_extract(fields_json, {path}) "
        f"WHEN 'integer' THEN CAST(json_extract(fields_json, {path}) AS TEXT) "
        f"WHEN 'real' THEN CAST(json_extract(fields_json, {path}) AS TEXT) "
        f"WHEN 'true' THEN '1' "
        f"WHEN 'false' THEN '0' "
        "ELSE NULL END"
    )


def _scalar_number_branch(feishu_name: str) -> str:
    path = json_path(feishu_name)
    return (
        f"CASE json_type(fields_json, {path}) "
        f"WHEN 'integer' THEN CAST(json_extract(fields_json, {path}) AS NUMERIC) "
        f"WHEN 'real' THEN CAST(json_extract(fields_json, {path}) AS NUMERIC) "
        "ELSE NULL END"
    )


def _numeric_from_text(text_expr: str) -> str:
    """从展示文本反解数字：只接受纯数字（含千分位、负号、小数点），否则 NULL。"""
    cleaned = (
        f"REPLACE(REPLACE(REPLACE(REPLACE(TRIM({text_expr}), ',', ''), '，', ''), ' ', ''), "
        f"{_sql_literal(chr(0xA0))}, '')"
    )
    guard = f"{cleaned} <> '' AND REPLACE(REPLACE({cleaned}, '-', ''), '.', '') NOT GLOB '*[^0-9]*'"
    return f"CASE WHEN {guard} THEN CAST({cleaned} AS NUMERIC) END"


# 时间戳合理区间（秒）：1973-01-01 到 2100-01-01。
EPOCH_SECONDS_FLOOR = 100000000
EPOCH_SECONDS_CEIL = 4102444800


def _epoch_to_text(value_expr: str) -> str:
    """毫秒（飞书）或秒时间戳 -> 本机时区 YYYY-MM-DD HH:MM:SS；不像时间戳则 NULL。

    没有下限的时候，999 这类杂散整数会被 ``datetime()`` 渲染成 1970-01-01 的假时间，
    比 NULL 更糟：假时间照样能进时间窗口。
    """
    seconds = (
        f"(CASE WHEN {value_expr} > 100000000000 THEN ({value_expr}) / 1000 ELSE {value_expr} END)"
    )
    return (
        f"CASE WHEN {seconds} BETWEEN {EPOCH_SECONDS_FLOOR} AND {EPOCH_SECONDS_CEIL} "
        f"THEN datetime({seconds}, 'unixepoch', 'localtime') END"
    )


def _text_to_timestamp(text_expr: str) -> str:
    """日期文本归一化为 YYYY-MM-DD HH:MM:SS；无法识别则 NULL（不用假值掩盖）。"""
    trimmed = f"TRIM({text_expr})"
    return (
        f"CASE WHEN {trimmed} IS NULL THEN NULL "
        f"WHEN length({trimmed}) >= 19 AND substr({trimmed}, 5, 1) = '-' AND substr({trimmed}, 8, 1) = '-' "
        f"THEN replace(substr({trimmed}, 1, 19), 'T', ' ') "
        f"WHEN length({trimmed}) = 10 AND substr({trimmed}, 5, 1) = '-' AND substr({trimmed}, 8, 1) = '-' "
        f"THEN {trimmed} "
        "ELSE NULL END"
    )


def _epoch_from_digits(text_expr: str) -> str:
    """纯数字文本形式的 epoch（10 位秒 / 13 位毫秒）-> 本地时间文本，其余 NULL。

    飞书日期字段通常是裸整数，但公式/查找引用会包成 ``{"value": [...]}``，历史导入也可能
    留下数字字符串；只看 ``json_type`` 的话这些一律静默变 NULL，时间窗口因此少掉整行。
    长度门限把 ``YYYYMMDD``（8 位）挡在外面，``2026-09-18`` 则因为带连字符不是纯数字。
    """
    trimmed = f"TRIM({text_expr})"
    return (
        f"CASE WHEN {trimmed} <> '' AND {trimmed} NOT GLOB '*[^0-9]*' "
        f"AND length({trimmed}) BETWEEN 10 AND 13 "
        f"THEN {_epoch_to_text(f'CAST({trimmed} AS INTEGER)')} END"
    )


def column_expression(item: Field) -> str:
    """一个可读视图列的 SQL 表达式（可能是多个飞书候选名的 COALESCE）。"""
    arms: list[str] = []
    for name in item.feishu_names:
        path = json_path(name)
        if item.kind == TEXT_CONCAT:
            arms.append(f"NULLIF({_core_expr(name, array_mode='concat')}, '')")
        elif item.kind in (TEXT_FIRST, SELECT_FIRST):
            arms.append(f"NULLIF({_core_expr(name, array_mode='first')}, '')")
        elif item.kind == URL:
            arms.append(
                f"NULLIF({_core_expr(name, array_mode='first', object_keys=('link', 'text'))}, '')"
            )
        elif item.kind == LINK_FIRST:
            # 关联字段：[{"record_id": ...}] / [{"id": ...}] / ["rec..."] / {"link": ...}（文档 :203）
            arms.append(
                "COALESCE("
                f"json_extract(fields_json, {json_path(name, index=0, key='record_id')}), "
                f"json_extract(fields_json, {json_path(name, index=0, key='id')}), "
                f"json_extract(fields_json, {json_path(name, index=0, key='text')}), "
                f"json_extract(fields_json, {json_path(name, index=0)}), "
                f"json_extract(fields_json, {json_path(name, key='record_id')}), "
                f"json_extract(fields_json, {json_path(name, key='id')}), "
                f"json_extract(fields_json, {json_path(name, key='link')}), "
                f"NULLIF({_core_expr(name, array_mode='first')}, '')"
                ")"
            )
        elif item.kind == NUMBER:
            arms.append(
                f"COALESCE({_scalar_number_branch(name)}, "
                f"{_numeric_from_text(_core_expr(name, array_mode='first'))})"
            )
        elif item.kind == DATETIME:
            epoch = f"CAST(json_extract(fields_json, {path}) AS INTEGER)"
            core = _core_expr(name, array_mode="first")
            arms.append(
                f"COALESCE("
                f"CASE json_type(fields_json, {path}) "
                f"WHEN 'integer' THEN {_epoch_to_text(epoch)} "
                f"WHEN 'real' THEN {_epoch_to_text(epoch)} "
                "ELSE NULL END, "
                f"{_epoch_from_digits(core)}, "
                f"{_text_to_timestamp(core)})"
            )
        elif item.kind == BOOL:
            text_arm = f"LOWER(TRIM(NULLIF({_core_expr(name, array_mode='first')}, '')))"
            arms.append(
                f"CASE json_type(fields_json, {path}) "
                "WHEN 'true' THEN 1 WHEN 'false' THEN 0 "
                f"WHEN 'integer' THEN CASE WHEN CAST(json_extract(fields_json, {path}) AS INTEGER) <> 0 THEN 1 ELSE 0 END "
                f"WHEN 'real' THEN CASE WHEN CAST(json_extract(fields_json, {path}) AS REAL) <> 0 THEN 1 ELSE 0 END "
                f"ELSE CASE WHEN {text_arm} IN ('1', 'true', 'yes', 'y', '是', '已', '开启', '启用') THEN 1 "
                f"WHEN {text_arm} IN ('0', 'false', 'no', 'n', '否', '未', '关闭', '停用') THEN 0 END END"
            )
        elif item.kind == JSON_RAW:
            # 完整值列统一输出合法 JSON：数组/对象原样，标量用 json_quote 包一层，
            # 字段缺失或 JSON null 才是 NULL（文档 :42、:204）。
            arms.append(
                f"CASE WHEN json_type(fields_json, {path}) IS NULL "
                f"OR json_type(fields_json, {path}) = 'null' THEN NULL "
                f"WHEN json_type(fields_json, {path}) IN ('array', 'object') "
                f"THEN json_extract(fields_json, {path}) "
                f"ELSE json_quote(json_extract(fields_json, {path})) END"
            )
        elif item.kind == COUNT:
            arms.append(
                f"CASE WHEN json_type(fields_json, {path}) = 'array' "
                f"THEN json_array_length(fields_json, {path}) END"
            )
        else:  # pragma: no cover - 防御：未知 kind 必须由代码审查发现
            raise ValueError(f"未知字段类型: {item.kind}")
    if len(arms) == 1:
        return arms[0]
    return "COALESCE(" + ", ".join(arms) + ")"


def build_view_sql(spec: TableSpec) -> str:
    """按本文件的定义生成 ``*_readable`` 视图 DDL。"""
    table = _ident(spec.table, "表名")
    view = _ident(spec.view, "视图名")
    select_terms = ["record_id"]
    seen: set[str] = {"record_id"}
    for item in spec.fields:
        name = _ident(item.name, "列名")
        if name in seen:
            raise ValueError(f"视图 {view} 列名重复: {name}")
        seen.add(name)
        select_terms.append(f"{column_expression(item)} AS {name}")
    select_terms.extend(VIEW_TAIL_COLUMNS)
    body = ",\n  ".join(select_terms)
    return f"CREATE VIEW {view} AS\nSELECT\n  {body}\nFROM {table}"


def build_mirror_view_sql() -> str:
    view = _ident(TRANSCRIPT_MIRROR_VIEW, "视图名")
    table = _ident(TRANSCRIPT_MIRROR_TABLE, "表名")
    cols = ", ".join(_ident(name, "列名") for name in TRANSCRIPT_MIRROR_COLUMNS)
    return f"CREATE VIEW {view} AS\nSELECT\n  {cols}\nFROM {table}"


# ---------------------------------------------------------------------------
# DDL
# ---------------------------------------------------------------------------

SYNC_STATE_SQL = """
CREATE TABLE IF NOT EXISTS sync_state (
    table_key          TEXT PRIMARY KEY,
    cursor_modified_at TEXT,
    updated_at         TEXT NOT NULL
)
"""

SYNC_RUNS_SQL = """
CREATE TABLE IF NOT EXISTS sync_runs (
    run_id        TEXT PRIMARY KEY,
    started_at    TEXT NOT NULL,
    ended_at      TEXT,
    status        TEXT NOT NULL,
    mode          TEXT NOT NULL,
    summary_json  TEXT,
    manifest_path TEXT
)
"""

TRANSCRIPT_MIRROR_SQL = (
    "CREATE TABLE IF NOT EXISTS "
    + TRANSCRIPT_MIRROR_TABLE
    + " (\n    "
    + ",\n    ".join(
        [
            "video_record_id      TEXT PRIMARY KEY",
            "document_url         TEXT",
            "document_id          TEXT",
            "revision_id          TEXT",
            "local_markdown_path  TEXT",
            "content_scope        TEXT",
            "extractor_version    INTEGER",
            "content_sha256       TEXT",
            "content_bytes        INTEGER",
            "source_content_sha256 TEXT",
            "source_content_bytes INTEGER",
            "sync_status          TEXT NOT NULL",
            "error_message        TEXT",
            "checked_at           TEXT",
            "mirrored_at          TEXT",
            "source_video_modified_at TEXT",
        ]
    )
    + "\n)"
)


def raw_table_sql(spec: TableSpec) -> str:
    table = _ident(spec.table, "表名")
    return f"CREATE TABLE IF NOT EXISTS {table} (\n{RAW_TABLE_COLUMNS_SQL}\n)"


def raw_index_sql(spec: TableSpec) -> str:
    table = _ident(spec.table, "表名")
    return f"CREATE INDEX IF NOT EXISTS idx_{table}_modified ON {table}(modified_at)"


def ensure_schema(conn: sqlite3.Connection) -> list[str]:
    """建/校验全部原始表、状态表、历史表、语料表，并按本文件定义重建所有 ``*_readable`` 视图。

    视图每次都用 ``DROP VIEW`` + ``CREATE VIEW`` 重建，因此改这里的定义即可生效，
    无需迁移脚本（文档 :27）。
    """
    created: list[str] = []
    for spec in TABLE_SPECS:
        conn.execute(raw_table_sql(spec))
        conn.execute(raw_index_sql(spec))
        created.append(spec.table)
    conn.execute(SYNC_STATE_SQL)
    conn.execute(SYNC_RUNS_SQL)
    conn.execute(TRANSCRIPT_MIRROR_SQL)
    created += ["sync_state", "sync_runs", TRANSCRIPT_MIRROR_TABLE]
    rebuild_readable_views(conn)
    return created


def rebuild_readable_views(conn: sqlite3.Connection) -> list[str]:
    """重建全部可读视图；返回视图名清单。视图定义唯一真源在本文件。"""
    rebuilt: list[str] = []
    for spec in TABLE_SPECS:
        view = _ident(spec.view, "视图名")
        conn.execute(f"DROP VIEW IF EXISTS {view}")
        conn.execute(build_view_sql(spec))
        rebuilt.append(view)
    mirror_view = _ident(TRANSCRIPT_MIRROR_VIEW, "视图名")
    conn.execute(f"DROP VIEW IF EXISTS {mirror_view}")
    conn.execute(build_mirror_view_sql())
    rebuilt.append(mirror_view)
    return rebuilt


def connect_rw(db_path: Path) -> sqlite3.Connection:
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path), timeout=30.0)
    conn.row_factory = sqlite3.Row
    # 故意不开 WAL：消费方（launcher_server）以 SQLite URI mode=ro 打开同一库，
    # WAL 的 -shm/-wal 在只读场景下会增加打开失败的风险（文档 :119-127）。
    conn.execute("PRAGMA journal_mode = DELETE")
    conn.execute("PRAGMA synchronous = NORMAL")
    conn.execute("PRAGMA busy_timeout = 30000")
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def connect_readonly(db_path: Path) -> sqlite3.Connection:
    """只读分析连接（文档 :119-127 的 URI 模式）。"""
    resolved = Path(db_path).resolve()
    conn = sqlite3.connect(resolved.as_uri() + "?mode=ro", uri=True, timeout=30.0)
    conn.row_factory = sqlite3.Row
    return conn


# ---------------------------------------------------------------------------
# 游标与 upsert（纯 SQLite 逻辑，可离线复用与验证）
# ---------------------------------------------------------------------------


def get_cursor(conn: sqlite3.Connection, table_key: str) -> str | None:
    row = conn.execute(
        "SELECT cursor_modified_at FROM sync_state WHERE table_key = ?", (table_key,)
    ).fetchone()
    if row is None:
        return None
    value = row["cursor_modified_at"]
    return str(value) if value else None


def set_cursor(
    conn: sqlite3.Connection, table_key: str, cursor_modified_at: str | None, updated_at: str
) -> None:
    conn.execute(
        """
        INSERT INTO sync_state (table_key, cursor_modified_at, updated_at)
        VALUES (?, ?, ?)
        ON CONFLICT(table_key) DO UPDATE SET
            cursor_modified_at = excluded.cursor_modified_at,
            updated_at = excluded.updated_at
        """,
        (table_key, cursor_modified_at, updated_at),
    )


def parse_time_text(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    for fmt in (TIME_FORMAT, "%Y-%m-%d %H:%M:%S.%f", "%Y-%m-%d", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(text[:26], fmt)
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(text)
    except ValueError:
        return None


def to_epoch_ms(value: Any) -> int | None:
    """飞书时间值（毫秒整数 / 秒整数 / 日期文本）-> 毫秒整数。"""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        number = int(value)
        if number <= 0:
            return None
        return number if number > 100000000000 else number * 1000
    text = str(value).strip()
    if not text:
        return None
    if re.fullmatch(r"-?\d{10,16}", text):
        return to_epoch_ms(int(text))
    parsed = parse_time_text(text.replace("T", " ").replace("Z", ""))
    if parsed is None:
        return None
    try:
        return int(parsed.timestamp() * 1000)
    except (OverflowError, OSError):
        return None


def ms_to_time_text(ms: int | None) -> str | None:
    if not ms:
        return None
    return datetime.fromtimestamp(ms / 1000).strftime(TIME_FORMAT)


def time_text_to_ms(text: str | None) -> int | None:
    parsed = parse_time_text(text)
    if parsed is None:
        return None
    try:
        return int(parsed.timestamp() * 1000)
    except (OverflowError, OSError):
        return None


@dataclass
class RecordPayload:
    """一条已经规范化、准备写入原始表的记录。"""

    record_id: str
    fields: dict[str, Any]
    fields_json: str
    payload_hash: str
    modified_ms: int | None


def normalize_record(
    record_id: str, fields: dict[str, Any], modified_field: str | None
) -> RecordPayload:
    """把飞书一行记录转成原始表的一行。``fields_json`` 保持无损（完整字段 JSON）。"""
    payload = dict(fields)
    fields_json = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    modified_ms: int | None = None
    if modified_field:
        modified_ms = to_epoch_ms(payload.get(modified_field))
    if modified_ms is None:
        for candidate in MODIFIED_TIME_FIELD_CANDIDATES:
            if candidate in payload:
                modified_ms = to_epoch_ms(payload.get(candidate))
                if modified_ms is not None:
                    break
    return RecordPayload(
        record_id=str(record_id),
        fields=payload,
        fields_json=fields_json,
        payload_hash=sha256_text(fields_json),
        modified_ms=modified_ms,
    )


def plan_table_sync(
    conn: sqlite3.Connection, spec: TableSpec, records: Sequence[RecordPayload]
) -> dict[str, Any]:
    """统计 created / updated / unchanged，不写库。用于幂等判定与 manifest。"""
    table = _ident(spec.table, "表名")
    existing: dict[str, str] = {
        str(row["record_id"]): str(row["payload_hash"])
        for row in conn.execute(f"SELECT record_id, payload_hash FROM {table}")
    }
    created = updated = unchanged = 0
    for record in records:
        current = existing.get(record.record_id)
        if current is None:
            created += 1
        elif current != record.payload_hash:
            updated += 1
        else:
            unchanged += 1
    return {
        "created": created,
        "updated": updated,
        "unchanged": unchanged,
        "existing_before": len(existing),
        "duplicate_incoming": len(records) - len({record.record_id for record in records}),
    }


def upsert_table_records(
    conn: sqlite3.Connection,
    spec: TableSpec,
    records: Sequence[RecordPayload],
    synced_at: str,
) -> dict[str, int]:
    """幂等 upsert：主键 record_id，payload_hash 相同则只刷新 last_synced_at。

    ``first_seen_at`` 一旦写入就不再覆盖，用于表达"首次进入本地库"。
    """
    table = _ident(spec.table, "表名")
    plan = plan_table_sync(conn, spec, records)
    sql = (
        f"INSERT INTO {table} (record_id, fields_json, payload_hash, modified_at, first_seen_at, last_synced_at)\n"
        "VALUES (?, ?, ?, ?, ?, ?)\n"
        "ON CONFLICT(record_id) DO UPDATE SET\n"
        "    fields_json = excluded.fields_json,\n"
        "    payload_hash = excluded.payload_hash,\n"
        "    modified_at = COALESCE(excluded.modified_at, " + table + ".modified_at),\n"
        "    last_synced_at = excluded.last_synced_at\n"
    )
    batch: list[tuple[Any, ...]] = []
    for record in records:
        batch.append(
            (
                record.record_id,
                record.fields_json,
                record.payload_hash,
                ms_to_time_text(record.modified_ms),
                synced_at,
                synced_at,
            )
        )
    for start in range(0, len(batch), 500):
        conn.executemany(sql, batch[start : start + 500])
    return plan


def advance_cursor(
    conn: sqlite3.Connection,
    spec: TableSpec,
    fetched: Sequence[RecordPayload],
    *,
    run_started_ms: int,
) -> dict[str, Any]:
    """把游标推进到本次读到的最大 ``最后修改时间``，永不回退（文档 :199、:229）。"""
    before = get_cursor(conn, spec.key)
    before_ms = time_text_to_ms(before)
    seen_ms = [record.modified_ms for record in fetched if record.modified_ms]
    observed_ms = max(seen_ms) if seen_ms else None
    candidate_ms = max(
        [value for value in (observed_ms, before_ms) if value is not None], default=None
    )
    after_text = ms_to_time_text(candidate_ms)
    if after_text is not None:
        set_cursor(conn, spec.key, after_text, now_text())
    return {
        "cursor_before": before,
        "cursor_after": after_text,
        "overlap_window_seconds": OVERLAP_WINDOW_SECONDS,
        "observed_modified_at": ms_to_time_text(observed_ms),
        "run_started_at": ms_to_time_text(run_started_ms),
    }


# ---------------------------------------------------------------------------
# 消费方视图契约校验（离线即可跑，防止改视图列名把工作台写坏）
# ---------------------------------------------------------------------------

CONSUMER_QUERIES: tuple[tuple[str, str], ...] = (
    (
        "launcher_server.list_creators",
        """
        SELECT record_id, creator_name, platform, homepage_url, is_tracking,
               video_collection_strategy, cross_platform_identity, follower_count,
               last_collected_at
        FROM creators_readable
        WHERE 1 = 1 AND platform = ? AND CAST(is_tracking AS INTEGER) = 1
        ORDER BY last_collected_at DESC
        LIMIT ?
        """,
    ),
    (
        "launcher_server.list_videos",
        """
        SELECT v.record_id, v.video_title, v.platform, c.creator_name, v.video_url, v.published_at,
               v.video_download_status, v.transcript_status,
               v.content_summary, v.key_points, v.user_pain_points, v.expandable_topics,
               v.representative_comments, v.duration_seconds
        FROM videos_readable v
        LEFT JOIN creators_readable c ON c.record_id = v.creator_record_id
        WHERE 1 = 1 AND v.platform = ?
        ORDER BY v.published_at DESC
        LIMIT ?
        """,
    ),
    (
        "launcher_server.read_transcript",
        """
        SELECT local_markdown_path, content_sha256
        FROM video_transcript_mirrors_readable
        WHERE video_record_id = ? AND sync_status = 'ok'
        ORDER BY mirrored_at DESC
        LIMIT 1
        """,
    ),
    (
        "doc:74-110 高价值评论",
        """
        SELECT platform, platform_video_id, user_name, comment_text, like_count, commented_at
        FROM video_comments_readable
        WHERE is_high_value = 1
        ORDER BY commented_at DESC
        LIMIT 20
        """,
    ),
    (
        "doc:84-92 口播稿镜像联表",
        """
        SELECT v.record_id, v.platform, v.video_title, v.published_at, v.content_summary,
               v.key_points, m.local_markdown_path, m.revision_id
        FROM videos_readable v
        JOIN video_transcript_mirrors_readable m ON m.video_record_id = v.record_id
        WHERE m.sync_status = 'ok' AND m.content_scope = 'readable_transcript'
        ORDER BY published_at DESC
        LIMIT 20
        """,
    ),
    (
        "doc:94-99 评论聚合",
        """
        SELECT platform, COUNT(*) AS comment_count,
               SUM(CASE WHEN is_high_value = 1 THEN 1 ELSE 0 END) AS high_value_count
        FROM video_comments_readable
        GROUP BY platform
        """,
    ),
    (
        "doc:101-105 快照序列",
        """
        SELECT snapshot_at, checkpoint, view_count, like_count, comment_count, favorite_count
        FROM video_metric_snapshots_readable
        WHERE video_record_id = ?
        ORDER BY snapshot_at
        """,
    ),
    (
        "favorites_readable",
        """
        SELECT favorite_name, platform, content_type, favorite_url, is_enabled, extraction_batch_size,
               extraction_status, extracted_count, new_pending_download_count, last_extracted_at,
               extraction_manifest_path, error_message, notes, related_videos_json, related_video_count
        FROM favorites_readable
        LIMIT 1
        """,
    ),
    (
        "creators_readable 完整列",
        """
        SELECT creator_name, platform, platform_user_id, homepage_url, is_tracking, follower_count,
               follower_count_display, xhs_likes_and_favorites_count, video_collection_strategy,
               cross_platform_identity, category_tags_json, sources_json, notes, last_collected_at,
               related_videos_json, related_video_count, record_id, feishu_modified_at, first_seen_at,
               last_synced_at, fields_json
        FROM creators_readable
        LIMIT 1
        """,
    ),
    (
        "videos_readable 完整列",
        """
        SELECT video_title, platform, platform_video_id, video_url, creator_record_id, published_at,
               duration_seconds, content_summary, key_points, high_like_comment_summary, user_pain_points,
               comment_controversies, expandable_topics, representative_comments, transcript_document_url,
               cleaned_transcript_path, transcript_status, video_download_status, comment_fetch_status,
               fetched_comment_count, related_metric_snapshots_json, related_metric_snapshot_count,
               record_id, feishu_modified_at, first_seen_at, last_synced_at, fields_json
        FROM videos_readable
        LIMIT 1
        """,
    ),
    (
        "video_comments_readable 完整列",
        """
        SELECT comment_id, platform, platform_video_id, video_record_id, comment_text, user_id, user_name,
               user_gender, user_signature, avatar_url, comment_level, root_comment_id, parent_comment_id,
               commented_at, like_count, reply_count, is_high_value, comment_tags_json, insight_notes,
               collected_at, record_id, feishu_modified_at, first_seen_at, last_synced_at, fields_json
        FROM video_comments_readable
        LIMIT 1
        """,
    ),
    (
        "video_metric_snapshots_readable 完整列",
        """
        SELECT snapshot_key, video_record_id, content_work_record_id, platform, video_title, checkpoint,
               snapshot_at, target_snapshot_at, hours_since_publish, collection_delay_minutes, view_count,
               like_count, comment_count, favorite_count, share_count, coin_count, danmaku_count,
               follower_count_snapshot, data_source, notes, record_id, feishu_modified_at, first_seen_at,
               last_synced_at, fields_json
        FROM video_metric_snapshots_readable
        LIMIT 1
        """,
    ),
    (
        "video_transcript_mirrors_readable 完整列",
        """
        SELECT video_record_id, document_url, document_id, revision_id, local_markdown_path, content_scope,
               extractor_version, content_sha256, content_bytes, source_content_sha256, source_content_bytes,
               sync_status, error_message, checked_at, mirrored_at, source_video_modified_at
        FROM video_transcript_mirrors_readable
        LIMIT 1
        """,
    ),
    (
        "doc:107-109 原始 JSON 兜底",
        "SELECT record_id, json_extract(fields_json, '$.\"备注\"') AS new_field FROM creators LIMIT 1",
    ),
)


def validate_views(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """把消费方/文档里的每条查询真的跑一遍，列名对不上就在这里暴露。"""
    results: list[dict[str, Any]] = []
    for label, sql in CONSUMER_QUERIES:
        params: tuple[Any, ...] = ()
        question_marks = sql.count("?")
        if question_marks == 2:
            params = ("B站", 50)
        elif question_marks == 1:
            params = ("rec000000000000",)
        try:
            conn.execute(sql, params).fetchall()
        except sqlite3.Error as exc:
            results.append({"query": label, "ok": False, "error": f"{type(exc).__name__}: {exc}"})
        else:
            results.append({"query": label, "ok": True, "error": ""})
    return results


# ---------------------------------------------------------------------------
# 进程锁（跨平台，供管线与单脚本共用）
# ---------------------------------------------------------------------------

LOCK_DIR = ROOT / "downloads" / "launcher-state" / "locks"
LOCK_STALE_SECONDS = 6 * 3600


@dataclass
class ProcessLock:
    """基于 ``O_CREAT | O_EXCL`` 的跨平台进程锁，带陈旧锁自愈。"""

    path: Path
    handle: int | None = None

    @property
    def acquired(self) -> bool:
        return self.handle is not None

    def release(self) -> None:
        if self.handle is None:
            return
        try:
            os.close(self.handle)
        except OSError:
            pass
        self.handle = None
        try:
            self.path.unlink()
        except OSError:
            pass

    def __enter__(self) -> "ProcessLock":
        return self

    def __exit__(self, *_exc: Any) -> None:
        self.release()


def _pid_alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if os.name == "nt":
        try:
            import ctypes

            still_active = 259
            PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
            kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
            handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
            if not handle:
                return False
            try:
                exit_code = ctypes.c_ulong()
                if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
                    return False
                return exit_code.value == still_active
            finally:
                kernel32.CloseHandle(handle)
        except Exception:  # pragma: no cover - 极端环境下降级为按时间判定
            return True
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def _read_lock_owner(path: Path) -> tuple[int | None, str]:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None, "unreadable"
    pid: int | None = None
    label = text.strip()[:200]
    match = re.search(r"pid=(\d+)", text)
    if match:
        pid = int(match.group(1))
    return pid, label


def acquire_lock(name: str, *, stale_seconds: int = LOCK_STALE_SECONDS) -> ProcessLock | None:
    """尝试获取锁；已被占用返回 ``None``。持锁进程已消失或锁文件过期时抢占陈旧锁。"""
    _ident(re.sub(r"[^a-z0-9_]", "_", name), "锁名")
    LOCK_DIR.mkdir(parents=True, exist_ok=True)
    path = LOCK_DIR / f"{re.sub(r'[^a-z0-9_]', '_', name)}.lock"
    payload = f"pid={os.getpid()} acquired_at={now_text()} cwd={Path.cwd()}\n"
    for attempt in (0, 1):
        try:
            handle = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
        except FileExistsError:
            if attempt == 1:
                return None
            pid, _label = _read_lock_owner(path)
            age = 0.0
            try:
                age = time.time() - path.stat().st_mtime
            except OSError:
                age = stale_seconds + 1
            stale = (
                (pid is not None and not _pid_alive(pid))
                or (pid is None and age > stale_seconds)
                or age > stale_seconds
            )
            if not stale:
                return None
            try:
                path.unlink()
            except OSError:
                return None
            continue
        except OSError as exc:
            print(f"[warn] 无法创建进程锁 {path}: {exc}", file=sys.stderr)
            return None
        try:
            os.write(handle, payload.encode("utf-8"))
        except OSError:
            pass
        return ProcessLock(path=path, handle=handle)
    return None


# ---------------------------------------------------------------------------
# 飞书读取
# ---------------------------------------------------------------------------


class FeishuSetupError(RuntimeError):
    """配置 / lark-cli 缺失，属于可修复的环境问题。"""


class IncrementalSearchUnavailable(RuntimeError):
    """lark-cli 不支持按最后修改时间过滤，需要退回全量读取。"""


REMEDINATION_BASE = (
    "修复提示：\n"
    f"  1) 复制 {relative_posix(ROOT / 'feishu-base-config.example.json')} 为 "
    f"{relative_posix(fc.CONFIG_PATH)}，填好 profile / base_token 与 tables 下五张表的 table_id"
    "（creators、videos、favorites、video_comments、video_metric_snapshots）；\n"
    "  2) 安装并登录飞书命令行：npm install -g @larksuite/cli && lark-cli --profile <profile> auth login；"
    "非默认 PATH 可设置环境变量 LARK_CLI_BINARY；\n"
    "  3) 离线只想建库/校验视图：python sync_feishu_base_to_local.py --init-schema"
)


def preflight() -> dict[str, Any]:
    """缺少配置或 lark-cli 时给出可执行修复提示，不伪造成功。"""
    problems: list[str] = []
    if not fc.CONFIG_PATH.is_file():
        problems.append(f"缺少飞书配置文件 {relative_posix(fc.CONFIG_PATH)}")
        config: dict[str, Any] = {}
    else:
        try:
            config = fc.load_config()
        except (OSError, json.JSONDecodeError) as exc:
            raise FeishuSetupError(
                f"飞书配置文件无法解析：{relative_posix(fc.CONFIG_PATH)} -> {exc}"
            ) from exc
        if not str(config.get("base_token") or "").strip():
            problems.append("配置缺少 base_token")
        if not str(config.get("profile") or "").strip():
            problems.append("配置缺少 profile")
    try:
        lark_cli_runtime.resolve_lark_cli_binary()
    except Exception as exc:
        problems.append(f"找不到 lark-cli 可执行文件（{exc}）")
    tables = config.get("tables") if isinstance(config.get("tables"), dict) else {}
    missing_tables = [
        spec.config_key
        for spec in TABLE_SPECS
        if not str((tables.get(spec.config_key) or {}).get("table_id") or "").strip()
    ]
    if config and missing_tables:
        problems.append("配置缺少这些表的 table_id: " + ", ".join(missing_tables))
    if problems:
        raise FeishuSetupError("；".join(problems))
    return config


def resolve_table_id(config: dict[str, Any], spec: TableSpec) -> str:
    tables = config.get("tables") or {}
    entry = tables.get(spec.config_key) or {}
    table_id = str(entry.get("table_id") or "").strip()
    if table_id:
        return table_id
    name = str(entry.get("name") or spec.label).strip()
    for value in tables.values():
        if (
            isinstance(value, dict)
            and str(value.get("name") or "").strip() == name
            and value.get("table_id")
        ):
            return str(value["table_id"])
    raise FeishuSetupError(f"配置里找不到表 {spec.config_key}（{spec.label}）的 table_id")


def list_base_fields(config: dict[str, Any], table_id: str) -> set[str] | None:
    """读取飞书表字段清单用于映射体检；失败不致命（返回 None 表示未知）。"""
    try:
        return set(fc.field_names(config, table_id))
    except Exception as exc:
        print(
            f"[warn] 无法读取字段清单（table_id={table_id}）：{str(exc).splitlines()[-1]}",
            file=sys.stderr,
        )
        return None


def _unsupported_command_error(text: str) -> bool:
    lowered = str(text or "").lower()
    markers = (
        "unknown flag",
        "unknown command",
        "unexpected argument",
        "no such command",
        "unrecognized",
        "is not a v1 command",
        "unknown option",
    )
    return any(marker in lowered for marker in markers)


def _filter_payload(modified_field: str, since_ms: int) -> dict[str, Any]:
    return {
        "filter": {
            "conjunction": "and",
            "conditions": [
                {
                    "field_name": modified_field,
                    "operator": "isGreater",
                    "value": [f"ExactDate({int(since_ms)})"],
                }
            ],
        }
    }


def fetch_records_filtered(
    config: dict[str, Any],
    table_id: str,
    modified_field: str,
    since_ms: int,
    *,
    throttle: float,
) -> list[dict[str, Any]]:
    """按最后修改时间增量拉取。lark-cli 不支持该命令时抛 IncrementalSearchUnavailable。"""
    import tempfile

    rows: list[dict[str, Any]] = []
    offset = 0
    tmp_dir = ROOT / ".tmp-lark"
    tmp_dir.mkdir(exist_ok=True)
    while True:
        with tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", suffix=".json", dir=tmp_dir, delete=False
        ) as handle:
            json.dump(_filter_payload(modified_field, since_ms), handle, ensure_ascii=False)
            payload_path = Path(handle.name)
        args = [
            "+record-search",
            "--as",
            "user",
            "--base-token",
            config["base_token"],
            "--table-id",
            table_id,
            "--limit",
            str(PAGE_LIMIT),
            "--offset",
            str(offset),
            "--json",
            f"@{payload_path.relative_to(ROOT)}",
        ]
        try:
            try:
                data = fc.run_lark(config, args, timeout=DEFAULT_REQUEST_TIMEOUT)
            except Exception as exc:
                message = str(exc)
                if _unsupported_command_error(message):
                    raise IncrementalSearchUnavailable(message) from exc
                raise
            payload = data["data"]
            names = payload.get("fields") or []
            items = payload.get("data") or []
            record_ids = payload.get("record_id_list") or []
            for index, values in enumerate(items):
                row = dict(zip(names, values))
                row["_record_id"] = (
                    record_ids[index]
                    if index < len(record_ids)
                    else (values or {}).get("record_id", "")
                )
                rows.append(row)
            if not payload.get("has_more"):
                break
            offset += PAGE_LIMIT
        finally:
            payload_path.unlink(missing_ok=True)
        time.sleep(max(0.0, throttle))
    return rows


def fetch_records_full(
    config: dict[str, Any], table_id: str, *, throttle: float
) -> list[dict[str, Any]]:
    """全量分页读取（首次初始化或 --full，或 lark-cli 不支持过滤时）。"""
    rows: list[dict[str, Any]] = []
    offset = 0
    while True:
        args = [
            "+record-list",
            "--as",
            "user",
            "--base-token",
            config["base_token"],
            "--table-id",
            table_id,
            "--limit",
            str(PAGE_LIMIT),
            "--offset",
            str(offset),
        ]
        data = fc.run_lark(config, args, timeout=DEFAULT_REQUEST_TIMEOUT)
        payload = data["data"]
        names = payload.get("fields") or []
        for record_id, values in zip(
            payload.get("record_id_list") or [], payload.get("data") or []
        ):
            row = dict(zip(names, values))
            row["_record_id"] = record_id
            rows.append(row)
        if not payload.get("has_more"):
            break
        offset += PAGE_LIMIT
        if offset % (PAGE_LIMIT * PROGRESS_EVERY_PAGES) == 0:
            print(f"  ... {table_id} 已读取 {len(rows)} 条", flush=True)
        time.sleep(max(0.0, throttle))
    return rows


def fetch_table(
    config: dict[str, Any],
    spec: TableSpec,
    cursor_text: str | None,
    *,
    force_full: bool,
    throttle: float,
) -> dict[str, Any]:
    """读取一张飞书表并规范化，不落库。失败直接抛出，由上层保证不部分推进。"""
    table_id = resolve_table_id(config, spec)
    available = list_base_fields(config, table_id)
    mapping_missing = [
        name
        for spec_field in spec.fields
        for name in (spec_field.feishu_names,)
        if available is not None
        and not any(candidate in available for candidate in spec_field.feishu_names)
    ]
    modified_field: str | None = None
    for candidate in MODIFIED_TIME_FIELD_CANDIDATES:
        if available is None or candidate in available:
            modified_field = candidate
            break
    since_ms = 0
    mode = "full"
    notes: list[str] = []
    if force_full:
        notes.append("--full 指定，执行全量读取")
    elif cursor_text and modified_field:
        cursor_ms = time_text_to_ms(cursor_text)
        if cursor_ms:
            since_ms = max(0, cursor_ms - OVERLAP_WINDOW_SECONDS * 1000)
            mode = "incremental"
        else:
            notes.append(f"游标文本无法解析（{cursor_text!r}），本次全量读取")
    elif cursor_text and not modified_field:
        notes.append("飞书表缺少最后修改时间字段，无法增量，本次全量读取")

    raw_rows: list[dict[str, Any]] = []
    if mode == "incremental":
        try:
            raw_rows = fetch_records_filtered(
                config, table_id, str(modified_field), since_ms, throttle=throttle
            )
        except IncrementalSearchUnavailable as exc:
            notes.append(
                "lark-cli 不支持 +record-search 过滤，退回全量分页读取；"
                f"末行={str(exc).splitlines()[-1][:200]}"
            )
            mode = "incremental_fallback_full_scan"
            raw_rows = fetch_records_full(config, table_id, throttle=throttle)
    else:
        raw_rows = fetch_records_full(config, table_id, throttle=throttle)

    records: list[RecordPayload] = []
    skipped_no_id = 0
    for row in raw_rows:
        record_id = str(row.get("_record_id") or "").strip()
        fields = {key: value for key, value in row.items() if key != "_record_id"}
        if not record_id:
            skipped_no_id += 1
            continue
        records.append(normalize_record(record_id, fields, modified_field))
    return {
        "table_key": spec.key,
        "local_table": spec.table,
        "table_id": table_id,
        "mode": mode,
        "modified_field": modified_field,
        "since_ms": since_ms or None,
        "records": records,
        "fetched": len(records),
        "skipped_no_record_id": skipped_no_id,
        "mapping_missing_fields": sorted(set(mapping_missing)),
        "base_fields_known": available is not None,
        "notes": notes,
    }


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------


def record_sync_run(
    conn: sqlite3.Connection,
    *,
    run_id: str,
    started_at: str,
    ended_at: str,
    status: str,
    mode: str,
    summary: dict[str, Any],
    manifest_path: str,
) -> None:
    conn.execute(
        """
        INSERT INTO sync_runs (run_id, started_at, ended_at, status, mode, summary_json, manifest_path)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(run_id) DO UPDATE SET
            ended_at = excluded.ended_at,
            status = excluded.status,
            mode = excluded.mode,
            summary_json = excluded.summary_json,
            manifest_path = excluded.manifest_path
        """,
        (run_id, started_at, ended_at, status, mode, canonical_json(summary), manifest_path),
    )


def sync_all(
    config: dict[str, Any] | None,
    *,
    db_path: Path,
    force_full: bool,
    throttle: float,
    log_path: Path,
    manifest_path: Path,
) -> int:
    started_perf = time.perf_counter()
    started_at = now_text()
    run_started_ms = int(started_perf * 0) + int(
        datetime.strptime(started_at, TIME_FORMAT).timestamp() * 1000
    )
    run_id = f"{ts_slug()}-{uuid.uuid4().hex[:8]}"
    mode_label = "full" if force_full else "incremental"
    notes: list[str] = []
    tables_report: dict[str, Any] = {}
    summary = {"created": 0, "updated": 0, "skipped_existing": 0, "failed": 0, "fetched": 0}

    emit_durable(
        f"飞书 Base 本地同步开始 run_id={run_id} db={relative_posix(db_path)}", log_path=log_path
    )

    # 阶段一：只读飞书，五张表全部拉完才允许写库（文档 :229）
    try:
        if config is None:  # pragma: no cover - 由 main 保证
            raise FeishuSetupError("缺少飞书配置")
        conn_probe = connect_rw(db_path)
        try:
            ensure_schema(conn_probe)
            conn_probe.commit()
            cursors = {spec.key: get_cursor(conn_probe, spec.key) for spec in TABLE_SPECS}
        finally:
            conn_probe.close()
        for spec in TABLE_SPECS:
            emit_durable(
                f"读取飞书表 {spec.label}（{spec.key}），游标={cursors.get(spec.key) or '无'}",
                log_path=log_path,
            )
            result = fetch_table(
                config, spec, cursors.get(spec.key), force_full=force_full, throttle=throttle
            )
            notes.extend(result["notes"])
            mode_label = (
                result["mode"] if result["mode"] == "incremental_fallback_full_scan" else mode_label
            )
            tables_report[spec.key] = result
            summary["fetched"] += int(result["fetched"])
            emit_durable(f"  {spec.label} 读取 {result['fetched']} 条", log_path=log_path)
    except Exception as exc:
        error_text = f"{type(exc).__name__}: {exc}"
        emit_durable(f"飞书读取失败，本地表与游标未做任何修改：{error_text}", log_path=log_path)
        if isinstance(exc, FeishuSetupError):
            emit_durable(REMEDINATION_BASE, log_path=log_path)
        ended_at = now_text()
        manifest = build_manifest(
            run_id=run_id,
            started_at=started_at,
            ended_at=ended_at,
            status="failed",
            mode=mode_label,
            db_path=db_path,
            tables_report=tables_report,
            summary=dict(summary, failed=1),
            notes=notes,
            error=error_text,
            duration_seconds=round(time.perf_counter() - started_perf, 3),
        )
        write_manifest(
            manifest_path,
            manifest,
            run_id=run_id,
            started_at=started_at,
            ended_at=ended_at,
            status="failed",
            mode=mode_label,
            summary=manifest["summary"],
        )
        print(json.dumps(manifest, ensure_ascii=False)[:4000], file=sys.stderr)
        return 1

    # 阶段二：单事务统一提交
    conn = connect_rw(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        ensure_schema(conn)
        synced_at = now_text()
        for spec in TABLE_SPECS:
            result = tables_report[spec.key]
            plan = upsert_table_records(conn, spec, result["records"], synced_at)
            cursor_info = advance_cursor(
                conn, spec, result["records"], run_started_ms=run_started_ms
            )
            result["rows_before"] = plan["existing_before"]
            result["created"] = plan["created"]
            result["updated"] = plan["updated"]
            result["unchanged"] = plan["unchanged"]
            result["duplicate_incoming"] = plan["duplicate_incoming"]
            result.update(cursor_info)
            result["rows_after"] = int(
                conn.execute(f"SELECT COUNT(*) AS n FROM {_ident(spec.table, '表名')}").fetchone()[
                    "n"
                ]
            )
            summary["created"] += plan["created"]
            summary["updated"] += plan["updated"]
            summary["skipped_existing"] += plan["unchanged"]
            emit_durable(
                f"提交 {spec.label}: 新增 {plan['created']} 变更 {plan['updated']} 未变 {plan['unchanged']} "
                f"游标 {cursor_info['cursor_before'] or '无'} -> {cursor_info['cursor_after'] or '无'}",
                log_path=log_path,
            )
        rebuilt = rebuild_readable_views(conn)
        view_checks = validate_views(conn)
        bad_views = [item for item in view_checks if not item["ok"]]
        if bad_views:
            raise RuntimeError(
                "视图契约校验失败: " + json.dumps(bad_views, ensure_ascii=False)[:2000]
            )
        record_sync_run(
            conn,
            run_id=run_id,
            started_at=started_at,
            ended_at=now_text(),
            status="success",
            mode=mode_label,
            summary={
                "tables": {
                    key: {
                        f: value[f]
                        for f in ("fetched", "created", "updated", "unchanged", "cursor_after")
                        if f in value
                    }
                    for key, value in tables_report.items()
                }
            },
            manifest_path=relative_posix(manifest_path),
        )
        conn.commit()
    except Exception as exc:
        conn.rollback()
        error_text = f"{type(exc).__name__}: {exc}"
        emit_durable(f"本地提交失败，已回滚：{error_text}", log_path=log_path)
        ended_at = now_text()
        try:
            conn.execute("BEGIN IMMEDIATE")
            ensure_schema(conn)
            record_sync_run(
                conn,
                run_id=run_id,
                started_at=started_at,
                ended_at=ended_at,
                status="failed",
                mode=mode_label,
                summary={"error": error_text[:1000]},
                manifest_path=relative_posix(manifest_path),
            )
            conn.commit()
        except Exception:  # pragma: no cover - 记账失败不掩盖主错误
            conn.rollback()
        manifest = build_manifest(
            run_id=run_id,
            started_at=started_at,
            ended_at=ended_at,
            status="failed",
            mode=mode_label,
            db_path=db_path,
            tables_report=tables_report,
            summary=dict(summary, failed=1),
            notes=notes,
            error=error_text,
            duration_seconds=round(time.perf_counter() - started_perf, 3),
        )
        write_manifest(
            manifest_path,
            manifest,
            run_id=run_id,
            started_at=started_at,
            ended_at=ended_at,
            status="failed",
            mode=mode_label,
            summary=manifest["summary"],
        )
        return 1
    finally:
        conn.close()

    ended_at = now_text()
    manifest = build_manifest(
        run_id=run_id,
        started_at=started_at,
        ended_at=ended_at,
        status="success",
        mode=mode_label,
        db_path=db_path,
        tables_report=tables_report,
        summary=dict(summary, failed=0),
        notes=notes,
        error=None,
        duration_seconds=round(time.perf_counter() - started_perf, 3),
        views_rebuilt=rebuilt,
        view_checks=view_checks,
    )
    write_manifest(
        manifest_path,
        manifest,
        run_id=run_id,
        started_at=started_at,
        ended_at=ended_at,
        status="success",
        mode=mode_label,
        summary=manifest["summary"],
    )
    emit_durable(
        f"飞书 Base 本地同步完成：新增 {summary['created']} 变更 {summary['updated']} 未变 {summary['skipped_existing']}",
        log_path=log_path,
    )
    return 0


def build_manifest(
    *,
    run_id: str,
    started_at: str,
    ended_at: str,
    status: str,
    mode: str,
    db_path: Path,
    tables_report: dict[str, Any],
    summary: dict[str, Any],
    notes: Sequence[str],
    error: str | None,
    duration_seconds: float,
    views_rebuilt: Sequence[str] = (),
    view_checks: Sequence[dict[str, Any]] = (),
) -> dict[str, Any]:
    tables: dict[str, Any] = {}
    for key, result in tables_report.items():
        tables[key] = {
            k: v
            for k, v in result.items()
            if k
            in (
                "table_id",
                "local_table",
                "mode",
                "modified_field",
                "since_ms",
                "fetched",
                "created",
                "updated",
                "unchanged",
                "rows_before",
                "rows_after",
                "duplicate_incoming",
                "skipped_no_record_id",
                "cursor_before",
                "cursor_after",
                "overlap_window_seconds",
                "mapping_missing_fields",
                "base_fields_known",
                "notes",
            )
        }
    return {
        "kind": "feishu_local_sync",
        "task": "sync_feishu_base_to_local",
        "run_id": run_id,
        "status": status,
        "mode": mode,
        "started_at": started_at,
        "ended_at": ended_at,
        "duration_seconds": duration_seconds,
        "db_path": relative_posix(db_path),
        "target_tables": [spec.table for spec in TABLE_SPECS],
        "views_rebuilt": list(views_rebuilt),
        "view_checks": list(view_checks),
        "tables": tables,
        "summary": {
            "created": summary.get("created", 0),
            "updated": summary.get("updated", 0),
            "skipped_existing": summary.get("skipped_existing", 0),
            "failed": summary.get("failed", 0),
            "fetched": summary.get("fetched", 0),
            "tables": len(TABLE_SPECS),
        },
        "notes": list(notes),
        "error": error,
    }


def write_manifest(
    path: Path,
    manifest: dict[str, Any],
    *,
    run_id: str,
    started_at: str,
    ended_at: str,
    status: str,
    mode: str,
    summary: dict[str, Any],
) -> None:
    fc.write_manifest(path, manifest)
    try:
        conn = connect_rw(Path(manifest["db_path"])) if manifest.get("db_path") else None
    except Exception:  # pragma: no cover
        conn = None
    if conn is not None:
        try:
            conn.execute("BEGIN IMMEDIATE")
            ensure_schema(conn)
            record_sync_run(
                conn,
                run_id=run_id,
                started_at=started_at,
                ended_at=ended_at,
                status=status,
                mode=mode,
                summary=summary,
                manifest_path=relative_posix(path),
            )
            conn.commit()
        except Exception:
            conn.rollback()
        finally:
            conn.close()


def init_schema_only(db_path: Path, log_path: Path, manifest_path: Path | None = None) -> int:
    """离线建库：建表、重建视图并校验消费方查询。不做任何飞书读取，也不写 sync_runs。"""
    conn = connect_rw(db_path)
    try:
        created = ensure_schema(conn)
        rebuilt = rebuild_readable_views(conn)
        checks = validate_views(conn)
        conn.commit()
    finally:
        conn.close()
    bad = [item for item in checks if not item["ok"]]
    payload = {
        "kind": "feishu_local_sync",
        "action": "init_schema",
        "status": "success" if not bad else "failed",
        "started_at": now_text(),
        "ended_at": now_text(),
        "db_path": relative_posix(db_path),
        "objects_created": created,
        "views_rebuilt": rebuilt,
        "view_checks": checks,
        "summary": {
            "created": 0,
            "updated": 0,
            "skipped_existing": 0,
            "failed": len(bad),
            "views": len(rebuilt),
        },
        "error": None if not bad else json.dumps(bad, ensure_ascii=False)[:2000],
        "note": "只建 schema/视图，未读取飞书；不产生同步游标与 sync_runs 记录",
    }
    if manifest_path is not None:
        # 管线要求「退出码 + 本次 manifest 状态」双判定，离线模式同样落一份 manifest 才不自相矛盾。
        # 这里用 fc.write_manifest 而不是本模块的 write_manifest：离线建库不该产生 sync_runs 记账。
        try:
            fc.write_manifest(Path(manifest_path), payload)
            payload["manifest_path"] = relative_posix(Path(manifest_path))
        except OSError as exc:  # pragma: no cover
            payload["manifest_write_error"] = str(exc)
    emit_durable(f"离线建库完成：{len(rebuilt)} 个视图，失败校验 {len(bad)} 项", log_path=log_path)
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0 if not bad else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="sync_feishu_base_to_local.py",
        description=(
            "把飞书 Base 的博主/视频/收藏/视频评论/视频数据快照五张表增量镜像到本地 SQLite，"
            "并按本脚本内的唯一真源重建 *_readable 视图。"
        ),
    )
    parser.add_argument("--full", action="store_true", help="忽略游标，强制全量读取（文档 :200）")
    parser.add_argument(
        "--init-schema",
        action="store_true",
        help="离线模式：只建表/重建视图并校验消费方查询，不访问飞书",
    )
    parser.add_argument("--db-path", default=str(DEFAULT_DB_PATH), help="目标 SQLite 路径")
    parser.add_argument("--manifest", default=None, help="指定本次 manifest 输出路径（管线会传入）")
    parser.add_argument(
        "--throttle-seconds", type=float, default=DEFAULT_THROTTLE_SECONDS, help="相邻飞书请求间隔"
    )
    parser.add_argument(
        "--log-path",
        default=None,
        help="持久进度日志路径（默认 downloads/logs/feishu-base-sync-<date>.log）",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(list(argv) if argv is not None else None)
    db_path = Path(args.db_path).expanduser().resolve()
    stamp = ts_slug()
    manifest_path = (
        Path(args.manifest).expanduser().resolve()
        if args.manifest
        else MANIFEST_ROOT / f"{stamp}-feishu-local-sync.json"
    )
    log_path = (
        Path(args.log_path).expanduser().resolve()
        if args.log_path
        else LOG_DIR / f"feishu-base-sync-{datetime.now():%Y-%m-%d}.log"
    )

    if args.init_schema:
        return init_schema_only(db_path, log_path, manifest_path)

    lock = acquire_lock("feishu_base_sync")
    if lock is None:
        message = "另一个 sync_feishu_base_to_local 进程正在运行，拒绝重叠执行"
        emit_durable(message, log_path=log_path)
        print(
            json.dumps(
                {
                    "kind": "feishu_local_sync",
                    "status": "locked",
                    "error": message,
                    "summary": {"created": 0, "updated": 0, "skipped_existing": 0, "failed": 1},
                },
                ensure_ascii=False,
            ),
            file=sys.stderr,
        )
        return 3
    try:
        try:
            config = preflight()
        except (FeishuSetupError, RuntimeError) as exc:
            emit_durable(f"环境预检失败：{exc}", log_path=log_path)
            print(f"{REMEDINATION_BASE}", file=sys.stderr)
            manifest = build_manifest(
                run_id=f"{stamp}-preflight",
                started_at=now_text(),
                ended_at=now_text(),
                status="failed",
                mode="preflight",
                db_path=db_path,
                tables_report={},
                summary={
                    "created": 0,
                    "updated": 0,
                    "skipped_existing": 0,
                    "failed": 1,
                    "fetched": 0,
                },
                notes=["未访问飞书，本地库未改动"],
                error=str(exc),
                duration_seconds=0.0,
            )
            manifest_path.parent.mkdir(parents=True, exist_ok=True)
            fc.write_manifest(manifest_path, manifest)
            result = dict(manifest, manifest_path=relative_posix(manifest_path))
            print(json.dumps(result, ensure_ascii=False))
            return 1
        code = sync_all(
            config,
            db_path=db_path,
            force_full=args.full,
            throttle=max(0.0, float(args.throttle_seconds)),
            log_path=log_path,
            manifest_path=manifest_path,
        )
        manifest = load_manifest_for_exit(manifest_path)
        manifest["manifest_path"] = relative_posix(manifest_path)
        manifest["exit_code"] = code
        manifest.setdefault(
            "summary", {"created": 0, "updated": 0, "skipped_existing": 0, "failed": 1}
        )
        print(json.dumps(manifest, ensure_ascii=False)[:20000])
        return code
    finally:
        lock.release()


def load_manifest_for_exit(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
        return (
            payload
            if isinstance(payload, dict)
            else {"status": "failed", "error": "manifest 不是对象"}
        )
    except (OSError, json.JSONDecodeError) as exc:
        return {"status": "failed", "error": f"读取 manifest 失败: {exc}"}


if __name__ == "__main__":  # pragma: no cover
    for stream_name in ("stdout", "stderr"):
        stream = getattr(sys, stream_name)
        if hasattr(stream, "reconfigure"):
            try:
                stream.reconfigure(encoding="utf-8", errors="replace")
            except Exception:  # pragma: no cover
                pass
    sys.exit(main())
