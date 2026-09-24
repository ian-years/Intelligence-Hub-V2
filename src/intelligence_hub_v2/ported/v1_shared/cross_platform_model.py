# ruff: noqa
# TODO(v2-adapt): 这一份与**已适配的 core/identity.py（T4.3）是重复的两份**，适配时合并过去，别留
# 第二套 normalize_*。它还剩下的价值只有飞书侧那半截，而那正是 identity.py 明确不收的东西：三张多维
# 表格的字段规格（CONTENT_WORK_FIELDS / VIDEO_MODEL_FIELDS / SNAPSHOT_MODEL_FIELDS）、单元格工具
# cell_text / link_ids / format_datetime、以及 '跨平台主体标识' / '检查点' / 'T+3' 那批中文字段名
# 常量 —— feishu_core 写回镜像库用的就是这批。合并方向：判定与归一以 core/identity.py 为准，字段名
# 与 spec 若还要用就随 T5.1 挪到飞书侧，本文件整体删除。
from __future__ import annotations

import hashlib
import math
import re
import unicodedata
from dataclasses import asdict, dataclass
from datetime import datetime, timedelta
from difflib import SequenceMatcher
from typing import Any

from intelligence_hub_v2.ported.v1_shared import platform_schema as ps


CONTENT_TABLE_CONFIG_KEY = "content_works"
CONTENT_TABLE_NAME = "内容作品"

CONTENT_WORK_LINK_FIELD = "关联内容作品"
CONTENT_WORK_REVERSE_LINK_FIELD = "平台发布记录"
DOWNLOAD_STRATEGY_FIELD = "下载策略"
CREATOR_COLLECTION_STRATEGY_FIELD = "视频采集策略"
CREATOR_IDENTITY_FIELD = "跨平台主体标识"
MATCH_STATUS_FIELD = "同源匹配状态"
MATCH_NOTE_FIELD = "同源匹配说明"

CHECKPOINT_FIELD = "检查点"
SNAPSHOT_KEY_FIELD = "快照唯一键"
TARGET_SNAPSHOT_TIME_FIELD = "目标快照时间"
PUBLISH_AGE_HOURS_FIELD = "发布后小时数"
CAPTURE_LAG_MINUTES_FIELD = "采集延迟分钟"
DATA_SOURCE_FIELD = "数据来源"

CHECKPOINT_INITIAL = "初始"
CHECKPOINT_T3 = "T+3"
CHECKPOINT_T7 = "T+7"
CHECKPOINT_LIVE = "即时"
CHECKPOINT_DAYS = {CHECKPOINT_T3: 3, CHECKPOINT_T7: 7}
DEFAULT_CHECKPOINT_MAX_LAG_HOURS = 26

DOWNLOAD_STRATEGY_DEFAULT = "沿用原流程"
DOWNLOAD_STRATEGY_PRIMARY = "下载主素材"
DOWNLOAD_STRATEGY_METRICS_ONLY = "仅采集数据"

CREATOR_STRATEGY_DEFAULT = "沿用原流程"
CREATOR_STRATEGY_DOWNLOAD = "下载媒体"
CREATOR_STRATEGY_METRICS_ONLY = "仅采集数据"


CONTENT_WORK_FIELDS = [
    {
        "type": "text",
        "name": "作品标题",
        "description": "同源内容的标准标题；一条内容作品可关联多个平台发布。",
    },
    {"type": "text", "name": "作品标识"},
    {
        "type": "select",
        "name": "匹配状态",
        "multiple": False,
        "options": [
            {"name": "仅单平台", "hue": "Gray"},
            {"name": "自动匹配", "hue": "Green"},
            {"name": "待人工确认", "hue": "Orange"},
            {"name": "人工确认", "hue": "Blue"},
        ],
    },
    {
        "type": "select",
        "name": "主下载平台",
        "multiple": False,
        "options": [
            {"name": ps.PLATFORM_BILI, "hue": "Blue"},
            {"name": ps.PLATFORM_DOUYIN, "hue": "Orange"},
            {"name": ps.PLATFORM_XIAOHONGSHU, "hue": "Carmine"},
            {"name": ps.PLATFORM_YOUTUBE, "hue": "Red"},
        ],
    },
    {"type": "text", "name": "主素材视频路径"},
    {"type": "text", "name": "匹配依据"},
    {"type": "datetime", "name": "首次发布时间", "style": {"format": "yyyy-MM-dd HH:mm"}},
    {"type": "datetime", "name": "最近关联时间", "style": {"format": "yyyy-MM-dd HH:mm"}},
    {"type": "created_at", "name": "创建时间", "style": {"format": "yyyy-MM-dd HH:mm"}},
]

VIDEO_MODEL_FIELDS = {
    DOWNLOAD_STRATEGY_FIELD: {
        "type": "select",
        "name": DOWNLOAD_STRATEGY_FIELD,
        "multiple": False,
        "options": [
            {"name": DOWNLOAD_STRATEGY_DEFAULT, "hue": "Gray"},
            {"name": DOWNLOAD_STRATEGY_PRIMARY, "hue": "Green"},
            {"name": DOWNLOAD_STRATEGY_METRICS_ONLY, "hue": "Orange"},
        ],
    },
    MATCH_STATUS_FIELD: {
        "type": "select",
        "name": MATCH_STATUS_FIELD,
        "multiple": False,
        "options": [
            {"name": "仅单平台", "hue": "Gray"},
            {"name": "自动匹配", "hue": "Green"},
            {"name": "待人工确认", "hue": "Orange"},
            {"name": "人工确认", "hue": "Blue"},
        ],
    },
    MATCH_NOTE_FIELD: {"type": "text", "name": MATCH_NOTE_FIELD},
}

CREATOR_MODEL_FIELDS = {
    CREATOR_IDENTITY_FIELD: {
        "type": "text",
        "name": CREATOR_IDENTITY_FIELD,
        "description": "同一博主的不同平台账号填写相同标识；留空时不自动假定账号属于同一主体。",
    },
    CREATOR_COLLECTION_STRATEGY_FIELD: {
        "type": "select",
        "name": CREATOR_COLLECTION_STRATEGY_FIELD,
        "multiple": False,
        "options": [
            {"name": CREATOR_STRATEGY_DEFAULT, "hue": "Gray"},
            {"name": CREATOR_STRATEGY_DOWNLOAD, "hue": "Green"},
            {"name": CREATOR_STRATEGY_METRICS_ONLY, "hue": "Orange"},
        ],
        "description": "留空或沿用原流程时不改变现有下载行为。",
    },
}

SNAPSHOT_MODEL_FIELDS = {
    CHECKPOINT_FIELD: {
        "type": "select",
        "name": CHECKPOINT_FIELD,
        "multiple": False,
        "options": [
            {"name": CHECKPOINT_INITIAL, "hue": "Gray"},
            {"name": CHECKPOINT_T3, "hue": "Orange"},
            {"name": CHECKPOINT_T7, "hue": "Green"},
            {"name": CHECKPOINT_LIVE, "hue": "Blue"},
        ],
    },
    SNAPSHOT_KEY_FIELD: {"type": "text", "name": SNAPSHOT_KEY_FIELD},
    TARGET_SNAPSHOT_TIME_FIELD: {
        "type": "datetime",
        "name": TARGET_SNAPSHOT_TIME_FIELD,
        "style": {"format": "yyyy-MM-dd HH:mm"},
    },
    PUBLISH_AGE_HOURS_FIELD: {
        "type": "number",
        "name": PUBLISH_AGE_HOURS_FIELD,
        "style": {
            "type": "plain",
            "precision": 2,
            "percentage": False,
            "thousands_separator": False,
        },
    },
    CAPTURE_LAG_MINUTES_FIELD: {
        "type": "number",
        "name": CAPTURE_LAG_MINUTES_FIELD,
        "style": {
            "type": "plain",
            "precision": 0,
            "percentage": False,
            "thousands_separator": False,
        },
    },
    "平台": ps.PLATFORM_FIELD_SPEC_SINGLE,
    DATA_SOURCE_FIELD: {"type": "text", "name": DATA_SOURCE_FIELD},
    "视频标题": {"type": "text", "name": "视频标题"},
}


def cell_text(value: Any) -> str:
    return ps.cell_text(value)


def link_ids(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item.get("id")) for item in value if isinstance(item, dict) and item.get("id")]


def parse_datetime(value: Any) -> datetime | None:
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value.replace(tzinfo=None)
    if isinstance(value, (int, float)):
        timestamp = float(value)
        if timestamp > 10_000_000_000:
            timestamp /= 1000
        return datetime.fromtimestamp(timestamp)
    text = cell_text(value).strip()
    if not text:
        return None
    text = text.replace("T", " ").replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
            try:
                parsed = datetime.strptime(text, fmt)
                break
            except ValueError:
                continue
        else:
            raise ValueError(f"Unsupported datetime value: {value!r}")
    if parsed.tzinfo:
        parsed = parsed.astimezone().replace(tzinfo=None)
    return parsed


def format_datetime(value: datetime | None) -> str | None:
    return value.strftime("%Y-%m-%d %H:%M:%S") if value else None


def normalize_title(value: Any) -> str:
    text = unicodedata.normalize("NFKC", cell_text(value)).lower()
    text = re.sub(
        r"\s*-\s*(抖音|哔哩哔哩|bilibili|小红书|youtube)\s*$",
        "",
        text,
        flags=re.I,
    )
    text = re.sub(r"#[^#\s]+", "", text)
    return "".join(char for char in text if char.isalnum())


def normalize_creator_name(value: Any) -> str:
    text = unicodedata.normalize("NFKC", cell_text(value)).lower()
    text = re.sub(r"(?:官方|聊赚钱|的抖音|的小红书|b站)$", "", text)
    return "".join(char for char in text if char.isalnum())


def normalize_content_text(value: Any) -> str:
    text = unicodedata.normalize("NFKC", cell_text(value)).lower()
    text = re.sub(r"https?://\S+", "", text)
    text = re.sub(r"#[^#\s]+", "", text)
    normalized = "".join(char for char in text if char.isalnum())
    return normalized[:6000]


def compact_number_to_int(value: Any) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool):
        raise ValueError(f"Boolean is not a metric count: {value!r}")
    if isinstance(value, (int, float)):
        if not math.isfinite(float(value)) or float(value) < 0:
            raise ValueError(f"Invalid metric count: {value!r}")
        return int(round(float(value)))
    text = unicodedata.normalize("NFKC", str(value)).strip().replace(",", "")
    if not text or text in {"-", "--"}:
        return None
    match = re.fullmatch(r"([0-9]+(?:\.[0-9]+)?)\s*([万wWkK]?)\+?", text)
    if not match:
        raise ValueError(f"Unsupported compact metric count: {value!r}")
    number = float(match.group(1))
    suffix = match.group(2).lower()
    multiplier = 10_000 if suffix in {"万", "w"} else 1_000 if suffix == "k" else 1
    return int(round(number * multiplier))


def checkpoint_target(published_at: datetime, checkpoint: str) -> datetime:
    if checkpoint not in CHECKPOINT_DAYS:
        raise ValueError(f"Unsupported scheduled checkpoint: {checkpoint}")
    return published_at + timedelta(days=CHECKPOINT_DAYS[checkpoint])


def snapshot_unique_key(
    video_record_id: str, checkpoint: str, captured_at: datetime | None = None
) -> str:
    if checkpoint == CHECKPOINT_LIVE:
        if not captured_at:
            raise ValueError("captured_at is required for live snapshot keys")
        return f"metric:{video_record_id}:{checkpoint}:{captured_at.strftime('%Y%m%d%H')}"
    return f"metric:{video_record_id}:{checkpoint}"


def work_key_from_video(video_record_id: str) -> str:
    digest = hashlib.sha1(str(video_record_id).encode("utf-8")).hexdigest()[:16]
    return f"work_{digest}"


@dataclass(frozen=True)
class MatchEvidence:
    eligible: bool
    auto_match: bool
    score: float
    title_score: float
    creator_score: float
    creator_identity_match: bool
    content_score: float | None
    duration_score: float | None
    publish_delta_hours: float | None
    reason: str

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def publication_match(left: dict[str, Any], right: dict[str, Any]) -> MatchEvidence:
    left_platform = ps.video_platform(left)
    right_platform = ps.video_platform(right)
    if not left_platform or not right_platform or left_platform == right_platform:
        return MatchEvidence(
            False, False, 0.0, 0.0, 0.0, False, None, None, None, "same_or_missing_platform"
        )

    left_title = normalize_title(left.get("视频标题"))
    right_title = normalize_title(right.get("视频标题"))
    if not left_title or not right_title:
        return MatchEvidence(False, False, 0.0, 0.0, 0.0, False, None, None, None, "missing_title")
    title_score = SequenceMatcher(None, left_title, right_title).ratio()

    left_creator_ids = set(link_ids(left.get("关联博主")))
    right_creator_ids = set(link_ids(right.get("关联博主")))
    left_identity_keys = {
        cell_text(item) for item in left.get("_creator_identity_keys", []) if cell_text(item)
    }
    right_identity_keys = {
        cell_text(item) for item in right.get("_creator_identity_keys", []) if cell_text(item)
    }
    creator_identity_match = bool(
        left_creator_ids & right_creator_ids or left_identity_keys & right_identity_keys
    )
    creator_score = 1.0 if creator_identity_match else 0.0
    if creator_score == 0:
        left_names = [normalize_creator_name(item) for item in left.get("_creator_names", [])]
        right_names = [normalize_creator_name(item) for item in right.get("_creator_names", [])]
        creator_score = max(
            (
                SequenceMatcher(None, a, b).ratio()
                for a in left_names
                for b in right_names
                if a and b
            ),
            default=0.0,
        )

    left_time = parse_datetime(left.get("发布时间"))
    right_time = parse_datetime(right.get("发布时间"))
    delta_hours = (
        abs((left_time - right_time).total_seconds()) / 3600 if left_time and right_time else None
    )
    if delta_hours is not None and delta_hours > 7 * 24:
        return MatchEvidence(
            False,
            False,
            0.0,
            title_score,
            creator_score,
            creator_identity_match,
            None,
            None,
            delta_hours,
            "publish_gap_over_7_days",
        )

    left_content = normalize_content_text(left.get("_content_text"))
    right_content = normalize_content_text(right.get("_content_text"))
    content_score = None
    if len(left_content) >= 40 and len(right_content) >= 40:
        content_score = SequenceMatcher(None, left_content, right_content).ratio()

    left_duration = left.get("时长秒")
    right_duration = right.get("时长秒")
    duration_score = None
    if isinstance(left_duration, (int, float)) and isinstance(right_duration, (int, float)):
        longer = max(float(left_duration), float(right_duration))
        if longer > 0:
            duration_score = 1 - abs(float(left_duration) - float(right_duration)) / longer

    time_score = 0.5 if delta_hours is None else max(0.0, 1 - delta_hours / (7 * 24))
    score_parts = [(title_score, 0.6), (creator_score, 0.25), (time_score, 0.15)]
    if content_score is not None and duration_score is not None:
        score_parts = [
            (title_score, 0.35),
            (content_score, 0.25),
            (creator_score, 0.2),
            (time_score, 0.1),
            (duration_score, 0.1),
        ]
    elif content_score is not None:
        score_parts = [
            (title_score, 0.45),
            (content_score, 0.25),
            (creator_score, 0.2),
            (time_score, 0.1),
        ]
    elif duration_score is not None:
        score_parts = [
            (title_score, 0.5),
            (creator_score, 0.2),
            (time_score, 0.1),
            (duration_score, 0.2),
        ]
    score = sum(value * weight for value, weight in score_parts)

    exact_distinctive_title = left_title == right_title and len(left_title) >= 8
    same_creator_route = creator_score >= 0.82 and title_score >= 0.84
    duration_route = creator_score >= 0.72 and title_score >= 0.74 and (duration_score or 0) >= 0.9
    strong_title_route = title_score >= 0.96 and min(len(left_title), len(right_title)) >= 12
    content_route = ((content_score or 0) >= 0.9 and title_score >= 0.4) or (
        (content_score or 0) >= 0.72 and title_score >= 0.55
    )
    timely = delta_hours is None or delta_hours <= 7 * 24
    auto_match = (
        timely
        and creator_identity_match
        and (
            exact_distinctive_title
            or same_creator_route
            or duration_route
            or strong_title_route
            or content_route
        )
    )
    reason = (
        f"title={title_score:.3f};creator={creator_score:.3f};"
        f"creator_identity_match={creator_identity_match};"
        f"content={content_score if content_score is not None else 'n/a'};"
        f"duration={duration_score if duration_score is not None else 'n/a'};"
        f"publish_delta_hours={delta_hours if delta_hours is not None else 'n/a'}"
    )
    return MatchEvidence(
        True,
        auto_match,
        round(score, 4),
        title_score,
        creator_score,
        creator_identity_match,
        content_score,
        duration_score,
        delta_hours,
        reason,
    )
