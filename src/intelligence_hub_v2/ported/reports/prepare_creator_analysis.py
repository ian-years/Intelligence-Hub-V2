#!/usr/bin/env python3
# ruff: noqa
# TODO(v2-adapt): V1 内容分析层的输入包准备器，未适配。它读的不是 V2 主库而是一座独立镜像库：
# `--db` 以 mode=ro 打开 <ROOT>/downloads/feishu-base/feishu-base.sqlite3，只查四个 *_readable 视图
# 加 video_transcript_mirrors_readable（content_scope='readable_transcript' / extractor_version=1 /
# sync_status='ok'），每条样本的口播稿还要按 content_sha256 逐条对回 downloads/feishu-docs/ 下的
# Markdown；数据截止点取 <ROOT>/downloads/manifests/*-feishu-local-pipeline.json 里"当天最新成功"
# 那一份（文件名前 8 位是下游的解析协议）。V2 侧这些视图与那份 pipeline 清单都不存在。
# 适配要做的那件事：取数改走 storage/repositories，截止点改取 V2 清单（core/manifest.py）的完成时刻。
"""内容分析层 · 确定性输入包准备程序（严格只读）

契约来源：`FEISHU_LOCAL_DATABASE.md` 的「内容分析层」章节。

    python .\\prepare_creator_analysis.py prepare --window daily
    python .\\prepare_creator_analysis.py prepare --window three-day
    python .\\prepare_creator_analysis.py prepare --window weekly

职责
----
1. 以**当天最新成功的** `downloads/manifests/*-feishu-local-pipeline.json` 作为数据截止点。
2. 按窗口（daily=24h / three-day=72h / weekly=168h，终点为数据截止点）从本地飞书镜像的
   `*_readable` 视图取出待分析样本，并**逐条校验**：
   - 视图中存在该视频记录；
   - `video_transcript_mirrors_readable` 中存在 `content_scope='readable_transcript'`
     且 `extractor_version=1` 且 `sync_status='ok'` 的纯净口播稿；
   - 该口播稿本地文件的 SHA-256 与库中 `content_sha256` 一致、字节数与 `content_bytes` 一致。
3. 任何样本不完整都会**逐条列出缺哪条视频的什么**并返回非零状态；不允许分析任务静默漏掉。
4. 产出可被下游 AI 任务（`creator-daily-analysis` / `creator-three-day-analysis` /
   `creator-weekly-analysis` skill）直接消费的结构化 JSON 输入包：每视频带**可读语料引用**
   （本地 Markdown 路径 + 飞书文档链接 + revision + 哈希）和观众/指标证据。
5. 生成**按口播稿 SHA 缓存的视频级内容画像**：画像只含口播稿的可数结构事实（分句、字数、
   标题层级、高频字组、拉丁 token 等），不含任何推测性结论。同一 SHA 复用缓存文件，
   三个窗口共享同一份缓存目录。

写入边界
--------
本程序只用 SQLite URI 只读模式打开镜像库，**绝不向 SQLite 视图或飞书写回**；
唯一的写入目标是 `outputs/`（已被 .gitignore 排除）下的输入包与画像缓存。
它不生成、不修改任何 `downloads/manifests/*-feishu-*.json`，也不触碰 `sync_*` 表。

退出码
------
- `0` 样本完整，输入包已生成
- `1` 环境或契约缺失/损坏（镜像库不存在、`*_readable` 视图缺失、当天无成功 pipeline manifest、manifest 不可读）
- `2` 样本不完整（缺口播稿 / 提取器版本不符 / 本地 SHA 不匹配等），必须人工补齐后重跑
- `3` 窗口内确实没有任何视频样本（数据库正常但为空，属合法状态，不是数据损坏）

确定性
------
同一镜像库 + 同一 pipeline manifest + 同一缓存目录状态下重复运行，`pack_sha256` 不变；
只有 `generated_at` 与 `cache_reuse` 两个信息性字段会随运行时刻变化，它们不参与哈希。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sqlite3
import sys
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from intelligence_hub_v2.ported.v1_shared.utils import (  # noqa: E402
    now_str,
    write_json,
)

TS_FORMAT = "%Y-%m-%d %H:%M:%S"

DEFAULT_DB_PATH = ROOT / "downloads" / "feishu-base" / "feishu-base.sqlite3"
DEFAULT_MANIFEST_DIR = ROOT / "downloads" / "manifests"
PIPELINE_GLOB = "*-feishu-local-pipeline.json"
DEFAULT_OUTPUT_ROOT = ROOT / "outputs" / "creator-analysis"
CACHE_DIR_NAME = "profile-cache"

WINDOW_HOURS = {"daily": 24, "three-day": 72, "weekly": 168}
TRANSCRIPT_CONTENT_SCOPE = "readable_transcript"
TRANSCRIPT_EXTRACTOR_VERSION = 1

PACK_SCHEMA_VERSION = "creator-analysis-input-pack/v1"
PROFILE_SCHEMA_VERSION = "creator-video-content-profile/v1"

EXIT_OK = 0
EXIT_ENVIRONMENT = 1
EXIT_INCOMPLETE = 2
EXIT_EMPTY_WINDOW = 3

REQUIRED_VIEWS = (
    "videos_readable",
    "creators_readable",
    "video_transcript_mirrors_readable",
)

# 这些列在旧记录里可能为 NULL（文档「数据语义与限制」），缺失是真实表达，不伪造默认值。
VIDEO_COLUMNS = (
    "record_id",
    "platform",
    "platform_video_id",
    "video_title",
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
    "transcript_document_url",
    "transcript_status",
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
MIRROR_COLUMNS = (
    "record_id",
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
    "sync_status",
    "error_message",
    "checked_at",
    "mirrored_at",
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
    "checkpoint",
    "snapshot_at",
    "hours_since_publish",
    "view_count",
    "like_count",
    "comment_count",
    "favorite_count",
    "share_count",
    "follower_count_snapshot",
    "data_source",
)

MAX_EVIDENCE_COMMENTS = 20
MAX_EVIDENCE_SNAPSHOTS = 40

_CJK_RUN = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff]+")
_LATIN_TOKEN = re.compile(r"[A-Za-z][A-Za-z0-9+._#-]{1,29}")
_SENTENCE_SPLIT = re.compile(r"(?<=[。！？!?；;])|\n")
_NUMBER_TOKEN = re.compile(r"\d+(?:\.\d+)?\s*(?:%|％|万|亿|元|倍|秒|分钟|小时|天)?")

_BIGRAM_STOPWORDS = frozenset(
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
    }
)


# --------------------------------------------------------------------------- #
# 基础工具
# --------------------------------------------------------------------------- #


def parse_ts(value: Any) -> datetime | None:
    """宽松解析镜像里的时间文本：`YYYY-MM-DD HH:MM:SS`、ISO `T` 分隔、纯日期、epoch 毫秒。"""
    if value is None:
        return None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        number = float(value)
        if number > 1e11:  # epoch 毫秒
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
    candidates = [text, text.replace("T", " ")]
    for fmt in (TS_FORMAT, "%Y-%m-%d %H:%M", "%Y-%m-%d", "%Y/%m/%d %H:%M:%S", "%Y/%m/%d"):
        for cand in candidates:
            try:
                return datetime.strptime(cand, fmt)
            except ValueError:
                continue
    return None


def fmt_ts(value: datetime | None) -> str:
    return value.strftime(TS_FORMAT) if value else ""


def read_json_strict(path: Path) -> tuple[Any, str]:
    try:
        with path.open("r", encoding="utf-8") as handle:
            return json.load(handle), ""
    except OSError as exc:
        return None, f"读取失败：{exc}"
    except json.JSONDecodeError as exc:
        return None, f"JSON 解析失败：{exc}"


def connect_readonly(path: Path) -> sqlite3.Connection:
    """SQLite URI 只读模式：任何写操作都会在引擎层直接失败。"""
    uri = f"file:{path.resolve().as_posix()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def view_columns(conn: sqlite3.Connection, view: str) -> set[str]:
    try:
        rows = conn.execute(f"PRAGMA table_info('{view}')").fetchall()  # noqa: S608 - 名称来自本文件常量表
    except sqlite3.Error:
        return set()
    return {str(row[1]) for row in rows}


def view_exists(conn: sqlite3.Connection, name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type IN ('table','view') AND name = ?",
        (name,),
    ).fetchone()
    return row is not None


def select_list(available: set[str], wanted: tuple[str, ...]) -> tuple[str, list[str]]:
    """只 SELECT 视图里真实存在的列，避免旧库缺列导致整条 SQL 失败。"""
    usable = [c for c in wanted if c in available]
    return ", ".join(usable), usable


def chunked(items: list[Any], size: int) -> list[list[Any]]:
    return [items[i : i + size] for i in range(0, len(items), size)]


def file_sha256(path: Path) -> tuple[str, int, str]:
    try:
        data = path.read_bytes()
    except OSError as exc:
        return "", 0, f"读取本地语料文件失败：{exc}"
    return hashlib.sha256(data).hexdigest(), len(data), ""


# --------------------------------------------------------------------------- #
# 数据截止点：当天最新成功的 pipeline manifest
# --------------------------------------------------------------------------- #


def pick_data_cutoff(
    manifest_dir: Path,
    manual_cutoff: str,
    today: datetime,
) -> tuple[datetime, dict[str, Any] | None, list[str], int]:
    """返回 (截止时刻, provenance, 人读说明, 退出码)。"""
    notes: list[str] = []
    if manual_cutoff:
        parsed = parse_ts(manual_cutoff)
        if parsed is None:
            notes.append(
                f"--cutoff 无法解析：{manual_cutoff!r}，需要 `YYYY-MM-DD HH:MM:SS`（本机时区）。"
            )
            return today, None, notes, EXIT_ENVIRONMENT
        provenance = {
            "mode": "manual",
            "manifest_path": None,
            "manifest_status": None,
            "cutoff_at": fmt_ts(parsed),
            "note": "人工指定截止点，仅用于离线演练；日常分析必须以 pipeline manifest 为准",
        }
        notes.append(f"使用人工指定数据截止点：{fmt_ts(parsed)}（跳过 pipeline manifest 校验）")
        return parsed, provenance, notes, EXIT_OK

    if not manifest_dir.is_dir():
        notes.append(
            f"manifest 目录不存在：{manifest_dir}\n"
            f"       → 先跑每日同步入口 `python run_feishu_local_sync.py` 生成 {PIPELINE_GLOB}。"
        )
        return today, None, notes, EXIT_ENVIRONMENT

    candidates = sorted(manifest_dir.glob(PIPELINE_GLOB))
    if not candidates:
        notes.append(
            f"{manifest_dir} 下没有任何 {PIPELINE_GLOB}，说明本机还没跑通过飞书本地同步管线。\n"
            f'       → 运行 `python run_feishu_local_sync.py`；确认离线样本可用 `--cutoff "YYYY-MM-DD HH:MM:SS"`。'
        )
        return today, None, notes, EXIT_ENVIRONMENT

    day_prefix = today.strftime("%Y%m%d")
    todays = [p for p in candidates if p.name[:8] == day_prefix]
    if not todays:
        latest = candidates[-1].name
        notes.append(
            f"今天（{today.strftime('%Y-%m-%d')}）没有成功的飞书本地同步管线记录。\n"
            f"       现存最新一份是 {latest}；`--cutoff` 只用于离线演练，不要拿昨天的数据冒充今天。"
        )
        return today, None, notes, EXIT_ENVIRONMENT

    successful: list[tuple[Path, dict[str, Any]]] = []
    for path in sorted(todays, key=lambda p: p.name, reverse=True):
        payload, error = read_json_strict(path)
        if error:
            notes.append(f"跳过 {path.name}：{error}")
            continue
        if not isinstance(payload, dict):
            notes.append(f"跳过 {path.name}：顶层不是 JSON 对象")
            continue
        status = str(payload.get("status") or "").strip().lower()
        failures = payload.get("failures") if isinstance(payload.get("failures"), list) else []
        summary = payload.get("summary") if isinstance(payload.get("summary"), dict) else {}
        failed_n = summary.get("failed")
        try:
            failed_n = int(failed_n) if failed_n is not None else len(failures)
        except (TypeError, ValueError):
            failed_n = len(failures)
        if status in {"failed", "error", "fail"} or failed_n > 0:
            notes.append(
                f"跳过 {path.name}：status={status or '未知'}，失败项 {failed_n} 条，不是成功管线"
            )
            continue
        if status not in {"success", "ok", "finished", "succeeded"}:
            notes.append(
                f"跳过 {path.name}：status={status or '缺失'} 未标记为成功，不敢当作数据截止点"
            )
            continue
        successful.append((path, payload))

    if not successful:
        notes.append(
            f"今天有 {len(todays)} 份 {PIPELINE_GLOB}，但没有一份是成功状态；"
            f"请先看最新一份 manifest 的 stages[] 定位失败阶段。"
        )
        return today, None, notes, EXIT_ENVIRONMENT

    path, payload = successful[0]  # 文件名前缀是 YYYYMMDD-HHMMSS，倒序第一即最新
    ended = (
        parse_ts(payload.get("ended_at"))
        or parse_ts(payload.get("finished_at"))
        or parse_ts(payload.get("started_at"))
        or parse_ts(datetime.fromtimestamp(path.stat().st_mtime))
    )
    provenance = {
        "mode": "pipeline-manifest",
        "manifest_path": str(path),
        "manifest_status": str(payload.get("status") or ""),
        "cutoff_at": fmt_ts(ended),
        "cutoff_field": next(
            (k for k in ("ended_at", "finished_at", "started_at") if payload.get(k)), "file_mtime"
        ),
        "manifest_kind": payload.get("kind"),
    }
    notes.append(f"数据截止点取自 {path.name} 的 {provenance['cutoff_field']}：{fmt_ts(ended)}")
    return ended, provenance, notes, EXIT_OK


# --------------------------------------------------------------------------- #
# 视频级内容画像（纯结构事实，按口播稿 SHA 缓存）
# --------------------------------------------------------------------------- #


def build_content_profile(markdown_text: str) -> dict[str, Any]:
    lines = [line.rstrip() for line in markdown_text.splitlines()]
    nonempty = [line.strip() for line in lines if line.strip()]
    headings = [line.lstrip("#").strip() for line in nonempty if line.startswith("#")]
    body = [line for line in nonempty if not line.startswith("#")]

    sentences: list[str] = []
    for line in body:
        for piece in _SENTENCE_SPLIT.split(line):
            piece = piece.strip()
            if piece:
                sentences.append(piece)

    char_count = len("".join(body))
    lengths = sorted(len(s) for s in sentences)
    bigrams: Counter[str] = Counter()
    for line in body:
        for run in _CJK_RUN.findall(line):
            for i in range(len(run) - 1):
                gram = run[i : i + 2]
                if gram not in _BIGRAM_STOPWORDS:
                    bigrams[gram] += 1
    latin: Counter[str] = Counter()
    for line in body:
        for token in _LATIN_TOKEN.findall(line):
            latin[token.lower()] += 1

    return {
        "profile_schema_version": PROFILE_SCHEMA_VERSION,
        "char_count": char_count,
        "line_count": len(nonempty),
        "body_line_count": len(body),
        "heading_count": len(headings),
        "headings": headings[:30],
        "sentence_count": len(sentences),
        "avg_sentence_chars": round(char_count / len(sentences), 2) if sentences else 0.0,
        "median_sentence_chars": lengths[len(lengths) // 2] if lengths else 0,
        "max_sentence_chars": lengths[-1] if lengths else 0,
        "question_sentence_count": sum(1 for s in sentences if s.endswith(("?", "？"))),
        "number_token_count": sum(len(_NUMBER_TOKEN.findall(s)) for s in sentences),
        "latin_token_count": sum(latin.values()),
        "top_cjk_bigrams": [[g, c] for g, c in bigrams.most_common(25)],
        "top_latin_tokens": [[t, c] for t, c in latin.most_common(25)],
        "opening_sentences": sentences[:3],
        "closing_sentences": sentences[-2:],
    }


def load_or_build_profile(
    cache_dir: Path,
    transcript_sha256: str,
    markdown_text: str,
) -> tuple[dict[str, Any], str, bool]:
    """命中缓存则复用；否则计算并落盘。返回 (画像, 缓存路径, 是否命中)。"""
    cache_dir.mkdir(parents=True, exist_ok=True)
    cache_path = cache_dir / f"{transcript_sha256}.json"
    hit = False
    profile: dict[str, Any] | None = None
    if cache_path.is_file():
        stored, error = read_json_strict(cache_path)
        if error:
            print(f"[警告] 画像缓存损坏，将重建：{cache_path} · {error}", file=sys.stderr)
        elif (
            isinstance(stored, dict)
            and stored.get("profile_schema_version") == PROFILE_SCHEMA_VERSION
        ):
            body = stored.get("profile")
            if isinstance(body, dict) and stored.get("transcript_sha256") == transcript_sha256:
                profile = body
                hit = True
    if profile is None:
        profile = build_content_profile(markdown_text)
        write_json(
            cache_path,
            {
                "profile_schema_version": PROFILE_SCHEMA_VERSION,
                "transcript_sha256": transcript_sha256,
                "profiled_at": now_str(),
                "profile": profile,
            },
        )
    canonical = json.dumps(profile, ensure_ascii=False, sort_keys=True)
    return profile, str(cache_path), hit


# --------------------------------------------------------------------------- #
# 镜像读取与样本校验
# --------------------------------------------------------------------------- #


def fetch_window_videos(
    conn: sqlite3.Connection,
    start: datetime,
    end: datetime,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """返回 (窗口内视频, 发布日期无法定位到窗口的视频)。"""
    available = view_columns(conn, "videos_readable")
    cols, usable = select_list(available, VIDEO_COLUMNS)
    if "record_id" not in usable or "published_at" not in usable:
        raise RuntimeError("videos_readable 缺少 record_id / published_at，视图定义与文档不一致")
    sql = (
        "SELECT {} FROM videos_readable WHERE published_at IS NOT NULL AND trim(published_at) <> ''"
    ).format(cols)
    rows = [dict(r) for r in conn.execute(sql).fetchall()]

    in_window: list[dict[str, Any]] = []
    unplaced: list[dict[str, Any]] = []
    for row in rows:
        published = parse_ts(row.get("published_at"))
        if published is None:
            row["_published_parsed"] = None
            unplaced.append(row)
            continue
        row["_published_parsed"] = published
        # SQL 里只做了粗筛（有发布日期即可解析），窗口判定在这里做，避免 `T` 分隔符的字典序陷阱。
        if start <= published <= end:
            in_window.append(row)
    in_window.sort(
        key=lambda r: (r["_published_parsed"], str(r.get("record_id") or "")), reverse=True
    )
    unplaced.sort(key=lambda r: str(r.get("record_id") or ""))
    return in_window, unplaced


def fetch_creator_index(conn: sqlite3.Connection) -> dict[str, dict[str, Any]]:
    if not view_exists(conn, "creators_readable"):
        return {}
    available = view_columns(conn, "creators_readable")
    cols, _ = select_list(available, CREATOR_COLUMNS)
    if not cols or "record_id" not in available:
        return {}
    out: dict[str, dict[str, Any]] = {}
    for row in conn.execute(f"SELECT {cols} FROM creators_readable"):  # noqa: S608
        item = dict(row)
        out[str(item.get("record_id"))] = item
    return out


def fetch_mirror_index(
    conn: sqlite3.Connection, video_ids: list[str]
) -> dict[str, list[dict[str, Any]]]:
    available = view_columns(conn, "video_transcript_mirrors_readable")
    cols, _ = select_list(available, MIRROR_COLUMNS)
    if "video_record_id" not in available or not cols:
        raise RuntimeError(
            "video_transcript_mirrors_readable 缺少 video_record_id，视图定义与文档不一致"
        )
    index: dict[str, list[dict[str, Any]]] = {}
    for batch in chunked(video_ids, 400):
        marks = ", ".join("?" for _ in batch)
        sql = (
            f"SELECT {cols} FROM video_transcript_mirrors_readable "  # noqa: S608 - 列名来自常量白名单
            f"WHERE video_record_id IN ({marks})"
        )
        for row in conn.execute(sql, batch):
            item = dict(row)
            index.setdefault(str(item.get("video_record_id")), []).append(item)
    return index


def fetch_comments(
    conn: sqlite3.Connection, view: str, wanted: tuple[str, ...], video_ids: list[str], limit: int
) -> dict[str, list[dict[str, Any]]]:
    if not view_exists(conn, view):
        return {}
    available = view_columns(conn, view)
    cols, _ = select_list(available, wanted)
    if "video_record_id" not in available or not cols:
        return {}
    order_parts = []
    if "like_count" in available:
        order_parts.append("like_count DESC")
    order_parts.append("comment_id ASC" if "comment_id" in available else "rowid ASC")
    index: dict[str, list[dict[str, Any]]] = {}
    for batch in chunked(video_ids, 400):
        marks = ", ".join("?" for _ in batch)
        sql = (
            f"SELECT {cols} FROM {view} "  # noqa: S608 - view 来自本文件常量
            f"WHERE video_record_id IN ({marks}) "
            f"ORDER BY {', '.join(order_parts)}"
        )
        for row in conn.execute(sql, batch):
            item = dict(row)
            key = str(item.get("video_record_id"))
            bucket = index.setdefault(key, [])
            if len(bucket) < limit:
                bucket.append(item)
    return index


def fetch_snapshots(
    conn: sqlite3.Connection, video_ids: list[str]
) -> dict[str, list[dict[str, Any]]]:
    view = "video_metric_snapshots_readable"
    if not view_exists(conn, view):
        return {}
    available = view_columns(conn, view)
    cols, _ = select_list(available, SNAPSHOT_COLUMNS)
    if "video_record_id" not in available or not cols:
        return {}
    order = "snapshot_at ASC" if "snapshot_at" in available else "rowid ASC"
    index: dict[str, list[dict[str, Any]]] = {}
    for batch in chunked(video_ids, 400):
        marks = ", ".join("?" for _ in batch)
        sql = (
            f"SELECT {cols} FROM {view} "  # noqa: S608
            f"WHERE video_record_id IN ({marks}) ORDER BY {order}"
        )
        for row in conn.execute(sql, batch):
            bucket = index.setdefault(str(dict(row).get("video_record_id")), [])
            if len(bucket) < MAX_EVIDENCE_SNAPSHOTS:
                bucket.append(dict(row))
    return index


def verify_transcript(
    video: dict[str, Any],
    mirrors: list[dict[str, Any]],
) -> tuple[dict[str, Any] | None, list[str]]:
    """校验单条视频的纯净口播稿。返回 (可用语料, 缺陷列表)。"""
    problems: list[str] = []
    record_id = str(video.get("record_id") or "")
    if not mirrors:
        return None, [
            "镜像库无口播稿记录：video_transcript_mirrors_readable 里找不到 video_record_id="
            f"{record_id or '(空 record_id)'} 的任何语料行"
        ]

    qualified = [
        m
        for m in mirrors
        if str(m.get("content_scope") or "") == TRANSCRIPT_CONTENT_SCOPE
        and _as_int(m.get("extractor_version")) == TRANSCRIPT_EXTRACTOR_VERSION
    ]
    if not qualified:
        seen = sorted(
            {
                f"content_scope={m.get('content_scope')!r}/extractor_version={m.get('extractor_version')!r}"
                for m in mirrors
            }
        )
        problems.append(
            "没有 content_scope='readable_transcript' 且 extractor_version=1 的纯净口播稿；"
            f"实际存在的语料行是：{'; '.join(seen)}"
        )
        return None, problems

    healthy = [
        m
        for m in qualified
        if str(m.get("sync_status") or "").lower() in {"ok", "success", "synced"}
    ]
    pool = healthy or qualified
    pool.sort(
        key=lambda m: (str(m.get("checked_at") or ""), str(m.get("mirrored_at") or "")),
        reverse=True,
    )
    mirror = pool[0]

    if not healthy:
        problems.append(
            "口播稿镜像 sync_status 不是 ok（当前 "
            f"{mirror.get('sync_status')!r}），错误信息：{mirror.get('error_message') or '(无)'}"
        )
        return None, problems

    raw_path = str(mirror.get("local_markdown_path") or "").strip()
    if not raw_path:
        problems.append("库中 local_markdown_path 为空，无法定位本地纯净语料文件")
        return None, problems
    candidate = Path(raw_path)
    path = candidate if candidate.is_absolute() else (ROOT / raw_path.replace("\\", "/"))
    if not path.is_file():
        alt = ROOT / "downloads" / raw_path.replace("\\", "/").lstrip("/")
        path = alt if alt.is_file() else path

    expected_sha = str(mirror.get("content_sha256") or "").strip()
    if not expected_sha:
        problems.append(f"库中 content_sha256 为空，无法校验本地文件完整性：{path}")
        return None, problems

    if not path.is_file():
        problems.append(
            f"本地纯净语料文件不存在：{raw_path}（按仓库根解析为 {path}）；"
            "请运行 python sync_feishu_transcript_docs_to_local.py 重新生成"
        )
        return None, problems

    actual_sha, size, read_error = file_sha256(path)
    if read_error:
        problems.append(f"本地语料文件不可读：{path} · {read_error}")
        return None, problems
    if actual_sha.lower() != expected_sha.lower():
        problems.append(
            f"本地语料 SHA-256 与库中记录不一致：{path}\n"
            f"        库中 content_sha256 = {expected_sha}\n"
            f"        实际文件 sha256     = {actual_sha}（文件被手工改过或丢失后重建）"
        )
    expected_bytes = _as_int(mirror.get("content_bytes"))
    if expected_bytes is not None and expected_bytes != size:
        problems.append(
            f"本地语料字节数与库中记录不一致：{path} 实际 {size} 字节，库中 content_bytes={expected_bytes}"
        )
    if problems:
        return None, problems

    corpus = {
        "mirror_record_id": mirror.get("record_id"),
        "video_record_id": mirror.get("video_record_id"),
        "document_url": mirror.get("document_url"),
        "document_id": mirror.get("document_id"),
        "revision_id": mirror.get("revision_id"),
        "local_markdown_path": str(path),
        "path_as_recorded": raw_path,
        "content_scope": mirror.get("content_scope"),
        "extractor_version": _as_int(mirror.get("extractor_version")),
        "content_sha256": expected_sha.lower(),
        "content_bytes": size,
        "source_content_sha256": mirror.get("source_content_sha256"),
        "sync_status": mirror.get("sync_status"),
        "checked_at": mirror.get("checked_at"),
        "mirrored_at": mirror.get("mirrored_at"),
        "sha_verified": True,
    }
    return corpus, []


def _as_int(value: Any) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, int):
        return value
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def describe(video: dict[str, Any]) -> str:
    platform = str(video.get("platform") or "未知平台")
    pid = str(video.get("platform_video_id") or "(无 platform_video_id)")
    title = str(video.get("video_title") or "(无标题)").replace("\n", " ")
    if len(title) > 46:
        title = title[:46] + "…"
    return f"{platform} · {pid} · {title}"


# --------------------------------------------------------------------------- #
# 主流程
# --------------------------------------------------------------------------- #


def prepare(args: argparse.Namespace) -> int:
    window = args.window
    hours = WINDOW_HOURS[window]
    started = datetime.now()
    db_path = Path(args.db).expanduser()
    manifest_dir = Path(args.manifest_dir).expanduser()
    output_root = Path(args.out_dir).expanduser()
    cache_dir = output_root / CACHE_DIR_NAME
    out_path = Path(args.out) if args.out else output_root / f"{window}-input-pack.json"

    print(f"prepare_creator_analysis · window={window} · 窗口 {hours} 小时")

    if not db_path.is_file():
        print(
            f"[错误] 飞书本地镜像库不存在：{db_path}\n"
            "       → 先运行 `python run_feishu_local_sync.py`（它调用 sync_feishu_base_to_local.py 建立 "
            "downloads/feishu-base/feishu-base.sqlite3 与 *_readable 视图）。",
            file=sys.stderr,
        )
        return EXIT_ENVIRONMENT

    cutoff, provenance, notes, code = pick_data_cutoff(manifest_dir, args.cutoff, started)
    for note in notes:
        stream = sys.stderr if code != EXIT_OK else sys.stdout
        print(f"       {note}", stream)
    if code != EXIT_OK:
        return code

    start = cutoff - timedelta(hours=hours)
    print(f"       分析窗口：{fmt_ts(start)} ~ {fmt_ts(cutoff)}（本机时区，终点=数据截止点）")

    try:
        conn = connect_readonly(db_path)
    except sqlite3.Error as exc:
        print(f"[错误] 只读打开镜像库失败：{db_path} · {exc}", file=sys.stderr)
        return EXIT_ENVIRONMENT

    try:
        missing = [v for v in REQUIRED_VIEWS if not view_exists(conn, v)]
        if missing:
            print(
                "[错误] 镜像库缺少必需的只读视图："
                + ", ".join(missing)
                + f"\n       库路径：{db_path}\n"
                "       → 视图每次运行 sync_feishu_base_to_local.py 都会按最新定义重建，请重跑同步；"
                '\n         可用 `sqlite3 -header "<db>" ".schema videos_readable"` 自查。',
                file=sys.stderr,
            )
            return EXIT_ENVIRONMENT

        videos, unplaced = fetch_window_videos(conn, start, cutoff)
        creators = fetch_creator_index(conn)
        video_ids = [str(v.get("record_id")) for v in videos]
        mirrors = fetch_mirror_index(conn, video_ids) if video_ids else {}
        comments = (
            fetch_comments(
                conn, "video_comments_readable", COMMENT_COLUMNS, video_ids, MAX_EVIDENCE_COMMENTS
            )
            if video_ids
            else {}
        )
        snapshots = fetch_snapshots(conn, video_ids) if video_ids else {}
        evidence_missing = [
            name
            for name in ("video_comments_readable", "video_metric_snapshots_readable")
            if not view_exists(conn, name)
        ]

        # 先做全量校验，任何一条不完整都不生成可用的输入包（但会把完整报告写出来给人看）。
        defects: list[dict[str, Any]] = []
        items: list[dict[str, Any]] = []
        cache_hits = 0
        cache_misses = 0
        for video in videos:
            record_id = str(video.get("record_id"))
            corpus, problems = verify_transcript(video, mirrors.get(record_id) or [])
            if problems:
                defects.append(
                    {
                        "platform": video.get("platform"),
                        "platform_video_id": video.get("platform_video_id"),
                        "video_record_id": record_id,
                        "video_title": video.get("video_title"),
                        "published_at": video.get("published_at"),
                        "problems": problems,
                    }
                )
                continue

            text = ""
            try:
                text = Path(corpus["local_markdown_path"]).read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError) as exc:
                defects.append(
                    {
                        "platform": video.get("platform"),
                        "platform_video_id": video.get("platform_video_id"),
                        "video_record_id": record_id,
                        "video_title": video.get("video_title"),
                        "published_at": video.get("published_at"),
                        "problems": [
                            f"本地语料按 UTF-8 解码失败：{corpus['local_markdown_path']} · {exc}"
                        ],
                    }
                )
                continue

            profile, cache_path, hit = load_or_build_profile(
                cache_dir, corpus["content_sha256"], text
            )
            if hit:
                cache_hits += 1
            else:
                cache_misses += 1

            duration = _as_int(video.get("duration_seconds"))
            creator = creators.get(str(video.get("creator_record_id") or ""), {})
            items.append(
                {
                    "video_record_id": record_id,
                    "platform": video.get("platform"),
                    "platform_video_id": video.get("platform_video_id"),
                    "video_title": video.get("video_title"),
                    "video_url": video.get("video_url"),
                    "published_at": video.get("published_at"),
                    "age_at_cutoff_hours": round(
                        (cutoff - parse_ts(video.get("published_at"))).total_seconds() / 3600.0, 2
                    ),
                    "duration_seconds": duration,
                    "chars_per_minute": round(profile["char_count"] / (duration / 60.0), 1)
                    if duration
                    else None,
                    "creator": {
                        "creator_record_id": video.get("creator_record_id"),
                        "creator_name": creator.get("creator_name"),
                        "platform": creator.get("platform"),
                        "cross_platform_identity": creator.get("cross_platform_identity"),
                        "follower_count": creator.get("follower_count"),
                        "follower_count_display": creator.get("follower_count_display"),
                    },
                    "base_analysis_fields": {
                        "content_summary": video.get("content_summary"),
                        "key_points": video.get("key_points"),
                        "user_pain_points": video.get("user_pain_points"),
                        "comment_controversies": video.get("comment_controversies"),
                        "expandable_topics": video.get("expandable_topics"),
                        "representative_comments": video.get("representative_comments"),
                        "high_like_comment_summary": video.get("high_like_comment_summary"),
                        "transcript_status": video.get("transcript_status"),
                        "comment_fetch_status": video.get("comment_fetch_status"),
                        "fetched_comment_count": video.get("fetched_comment_count"),
                    },
                    "corpus": corpus,
                    "content_profile_ref": cache_path,
                    "content_profile": profile,
                    "audience_evidence": comments.get(record_id, []),
                    "metric_snapshots": snapshots.get(record_id, []),
                }
            )

        # 完整性硬闸：一条不完整就整体失败。
        if defects:
            print(
                f"\n[样本不完整] 窗口内 {len(videos)} 条视频里 {len(defects)} 条不合格，"
                "本次不生成输入包（不允许分析任务静默漏掉）：",
                file=sys.stderr,
            )
            for i, defect in enumerate(defects, 1):
                fake = {
                    "platform": defect.get("platform"),
                    "platform_video_id": defect.get("platform_video_id"),
                    "video_title": defect.get("video_title"),
                }
                print(
                    f"  ({i}/{len(defects)}) {describe(fake)}  record_id={defect.get('video_record_id')}"
                    f"  published_at={defect.get('published_at')}",
                    file=sys.stderr,
                )
                for problem in defect["problems"]:
                    print(f"      - {problem}", file=sys.stderr)
            report_path = write_json(
                output_root / f"{window}-incomplete-samples.json",
                {
                    "schema_version": PACK_SCHEMA_VERSION,
                    "status": "incomplete",
                    "window": window,
                    "window_hours": hours,
                    "window_start_at": fmt_ts(start),
                    "data_cutoff": provenance,
                    "generated_at": now_str(),
                    "database": str(db_path),
                    "videos_in_window": len(videos),
                    "defect_count": len(defects),
                    "defects": defects,
                    "unplaced_videos": [
                        {
                            "record_id": u.get("record_id"),
                            "platform": u.get("platform"),
                            "platform_video_id": u.get("platform_video_id"),
                            "video_title": u.get("video_title"),
                            "published_at": u.get("published_at"),
                        }
                        for u in unplaced
                    ],
                },
            )
            print(f"      明细已写出：{report_path}", file=sys.stderr)
            print(
                "      补齐后重跑：python run_feishu_local_sync.py（会重新提取口播稿并校验 SHA）",
                file=sys.stderr,
            )
            return EXIT_INCOMPLETE

        if not items:
            print(
                f"[窗口为空] {fmt_ts(start)} ~ {fmt_ts(cutoff)} 内镜像库里没有任何已发布视频。\n"
                "       这是「没有数据」而不是「数据坏了」：库和视图都正常，只是窗口内无样本。\n"
                f"       可查：SELECT published_at, video_title FROM videos_readable "
                f"ORDER BY published_at DESC LIMIT 20;  库路径 {db_path}",
                file=sys.stderr,
            )
            if unplaced:
                print(
                    f"       另有 {len(unplaced)} 条视频发布日期缺失或无法解析，未参与窗口划分（见下）。",
                    file=sys.stderr,
                )
                for row in unplaced[:20]:
                    print(
                        f"         - {describe(row)}  published_at={row.get('published_at')!r}",
                        file=sys.stderr,
                    )
            return EXIT_EMPTY_WINDOW

        platforms = Counter(str(i.get("platform") or "") for i in items)
        creator_ids = {str((i.get("creator") or {}).get("creator_record_id") or "") for i in items}
        pack: dict[str, Any] = {
            "schema_version": PACK_SCHEMA_VERSION,
            "window": window,
            "window_hours": hours,
            "window_start_at": fmt_ts(start),
            "data_cutoff_at": fmt_ts(cutoff),
            "data_cutoff_provenance": provenance,
            "database": str(db_path),
            "transcript_requirement": {
                "content_scope": TRANSCRIPT_CONTENT_SCOPE,
                "extractor_version": TRANSCRIPT_EXTRACTOR_VERSION,
                "sha_verified_for_all_samples": True,
            },
            "corpus_cache_dir": str(cache_dir),
            "sample_summary": {
                "videos": len(items),
                "creators": len({c for c in creator_ids if c}),
                "platforms": dict(sorted(platforms.items())),
                "total_transcript_chars": sum(i["content_profile"]["char_count"] for i in items),
                "audience_evidence_comments": sum(len(i["audience_evidence"]) for i in items),
                "metric_snapshots": sum(len(i["metric_snapshots"]) for i in items),
            },
            "missing_evidence_views": evidence_missing,
            "unplaced_videos": [
                {
                    "record_id": u.get("record_id"),
                    "platform": u.get("platform"),
                    "platform_video_id": u.get("platform_video_id"),
                    "video_title": u.get("video_title"),
                    "published_at": u.get("published_at"),
                    "reason": "published_at 缺失或无法解析，无法判定是否落在窗口内",
                }
                for u in unplaced
            ],
            "videos": items,
            "writer_contract": {
                "report_json_target": str((output_root / f"{window}-report.json").resolve()),
                "renderer_command": (
                    "python render_creator_analysis_report.py "
                    f'--input "{output_root / f"{window}-report.json"}" '
                    f'--window {window} --output "{output_root / f"{window}-report.html"}"'
                ),
                "rules": [
                    "只读输入包与 content_profile，不得自行扩大样本范围",
                    "不得改写 corpus.content_sha256 指向的语料文件",
                    "报告 JSON 的 schema 见 render_creator_analysis_report.py --help",
                ],
            },
        }
        pack["pack_sha256"] = hashlib.sha256(
            json.dumps(pack, ensure_ascii=False, sort_keys=True).encode("utf-8")
        ).hexdigest()
        pack["hash_scope"] = "除 generated_at / cache_reuse / output_path 之外的全部字段"
        pack["generated_at"] = now_str()
        pack["cache_reuse"] = {
            "hit": cache_hits,
            "built": cache_misses,
            "shared_across_windows": True,
        }
        pack["output_path"] = str(Path(out_path).resolve())

        written = write_json(out_path, pack)
        print(
            f"[完成] 输入包：{written}\n"
            f"       样本 {len(items)} 条视频 / {pack['sample_summary']['creators']} 位博主主体 / "
            f"画像缓存命中 {cache_hits}、新建 {cache_misses}"
        )
        if unplaced:
            print(
                f"       注意：另有 {len(unplaced)} 条视频发布日期无法解析，已记入 unplaced_videos。"
            )
        if evidence_missing:
            print(f"       注意：镜像库缺少视图 {', '.join(evidence_missing)}，对应证据为空。")
        print(
            f"       下一步：AI 写 {output_root / f'{window}-report.json'}，"
            f"再跑 python render_creator_analysis_report.py --window {window} ..."
        )
        return EXIT_OK
    except sqlite3.Error as exc:
        print(f"[错误] 查询镜像库失败（只读）：{exc}", file=sys.stderr)
        return EXIT_ENVIRONMENT
    except RuntimeError as exc:
        print(f"[错误] 镜像结构与文档契约不符：{exc}", file=sys.stderr)
        return EXIT_ENVIRONMENT
    finally:
        conn.close()


def build_parser() -> argparse.ArgumentParser:
    common = (
        "示例：\n"
        "  python prepare_creator_analysis.py prepare --window daily\n"
        "  python prepare_creator_analysis.py prepare --window three-day --out-dir outputs/creator-analysis\n"
        '  python prepare_creator_analysis.py prepare --window weekly --cutoff "2026-08-27 20:00:00"\n'
        "\n退出码：0 成功 / 1 环境或契约缺失 / 2 样本不完整（必须人工补齐）/ 3 窗口内无样本\n"
        "窗口：daily=24h、three-day=72h、weekly=168h，终点都是当天最新成功 pipeline manifest 的时间点。\n"
        "只读保证：SQLite URI mode=ro，不写视图、不写飞书、不写 downloads/manifests/*-feishu-*.json。"
    )
    parser = argparse.ArgumentParser(
        prog="prepare_creator_analysis.py",
        description="内容分析层：生成确定性输入包 + 按口播稿 SHA 缓存的视频级内容画像（只读本地飞书镜像库）。",
        epilog=common,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = parser.add_subparsers(dest="command", required=True, metavar="COMMAND")

    prep = sub.add_parser(
        "prepare",
        help="校验窗口样本并生成输入包",
        description="校验窗口内每条视频的纯净口播稿（readable_transcript/v1 + 本地 SHA 一致），"
        "全部合格才生成确定性输入包；任何不完整都逐条报错并非零退出。",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=common,
    )
    prep.add_argument("--window", required=True, choices=sorted(WINDOW_HOURS), help="分析窗口")
    prep.add_argument(
        "--db",
        default=str(DEFAULT_DB_PATH),
        help=f"飞书本地镜像库路径（默认 {DEFAULT_DB_PATH.relative_to(ROOT)}）",
    )
    prep.add_argument(
        "--manifest-dir",
        default=str(DEFAULT_MANIFEST_DIR),
        help=f"pipeline manifest 目录（默认 {DEFAULT_MANIFEST_DIR.relative_to(ROOT)}）",
    )
    prep.add_argument(
        "--cutoff",
        default="",
        help="人工指定数据截止点 'YYYY-MM-DD HH:MM:SS'，仅用于离线演练；默认必须来自当天成功的 pipeline manifest",
    )
    prep.add_argument(
        "--out-dir",
        default=str(DEFAULT_OUTPUT_ROOT),
        help=f"输出根目录（默认 {DEFAULT_OUTPUT_ROOT.relative_to(ROOT)}），画像缓存在其下的 {CACHE_DIR_NAME}/",
    )
    prep.add_argument(
        "--out", default="", help="输入包落盘路径（默认 <out-dir>/<window>-input-pack.json）"
    )
    prep.set_defaults(func=prepare)
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
