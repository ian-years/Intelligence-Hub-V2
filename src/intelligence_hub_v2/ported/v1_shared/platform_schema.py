# ruff: noqa
# TODO(v2-adapt): V1 的"飞书多维表格行 → 本地记录"词表 + 解包函数，未适配。它把平台显示名写死成
# 中文（'B站'/'抖音'/'小红书'/'YouTube'）并自带一份 PLATFORMS 列表，而 V2 的平台身份已经有四处真相
# （config/platforms.yaml 的 key、PLATFORM_CONFIG_SCHEMAS、platforms/registry.py、前端列表），
# core/identity.py 因此明确拒绝写第五份（§7.10/§7.11 那一族）。infer_douyin_sec_uid / infer_bili_mid
# 正是 §7.1 看护的形状，但在 V2 属于平台适配器内部而不是词表。适配要做的那件事：飞书列名映射留在
# T5.1 的适配层（如果镜像库还要），平台常量交回 registry，本文件删。
import json
import re
from pathlib import Path


PLATFORM_BILI = "B站"
PLATFORM_DOUYIN = "抖音"
PLATFORM_XIAOHONGSHU = "小红书"
PLATFORM_YOUTUBE = "YouTube"
PLATFORMS = [PLATFORM_BILI, PLATFORM_DOUYIN, PLATFORM_XIAOHONGSHU, PLATFORM_YOUTUBE]

PLATFORM_FIELD = "平台"
CREATOR_PLATFORM_ID_FIELD = "平台用户ID"
VIDEO_PLATFORM_ID_FIELD = "平台视频ID"
TRACK_FIELD = "是否持续跟踪"
HOME_FIELD = "主页链接"

PLATFORM_FIELD_SPEC_MULTI = {
    "type": "select",
    "name": PLATFORM_FIELD,
    "multiple": True,
    "options": [
        {"name": PLATFORM_BILI, "hue": "Blue", "lightness": "Lighter"},
        {"name": PLATFORM_DOUYIN, "hue": "Orange", "lightness": "Lighter"},
        {"name": PLATFORM_XIAOHONGSHU, "hue": "Carmine", "lightness": "Lighter"},
        {"name": PLATFORM_YOUTUBE, "hue": "Red", "lightness": "Lighter"},
    ],
}

PLATFORM_FIELD_SPEC_SINGLE = {
    **PLATFORM_FIELD_SPEC_MULTI,
    "multiple": False,
}

CREATOR_PLATFORM_ID_FIELD_SPEC = {"type": "text", "name": CREATOR_PLATFORM_ID_FIELD}
VIDEO_PLATFORM_ID_FIELD_SPEC = {"type": "text", "name": VIDEO_PLATFORM_ID_FIELD}


def cell_text(value):
    if value is None:
        return ""
    if isinstance(value, dict):
        for key in ("text", "name", "value", "link"):
            if value.get(key) is not None:
                return str(value.get(key) or "").strip()
        return ""
    if isinstance(value, list):
        parts = [cell_text(item) for item in value]
        return " ".join(part for part in parts if part).strip()
    return str(value or "").strip()


def platform_values(value):
    if value is None:
        return []
    if isinstance(value, list):
        values = []
        for item in value:
            text = cell_text(item)
            if text:
                values.append(text)
        return values
    text = cell_text(value)
    if not text:
        return []
    if text in PLATFORMS:
        return [text]
    return [platform for platform in PLATFORMS if platform in text]


def has_platform(value, platform):
    return platform in platform_values(value)


def first_platform(value):
    values = platform_values(value)
    return values[0] if values else ""


def platform_cell(field_map, platform):
    field = (field_map or {}).get(PLATFORM_FIELD) or {}
    return [platform] if field.get("multiple") is True else platform


def checkbox_enabled(value):
    if value is True:
        return True
    if value is False or value is None:
        return False
    return str(value).strip().lower() in {"true", "1", "yes", "y", "是"}


def infer_bili_mid(value):
    match = re.search(r"space\.bilibili\.com/(\d+)", str(value or ""), flags=re.I)
    return match.group(1) if match else ""


def infer_douyin_sec_uid(value):
    text = str(value or "")
    if "douyin.com" not in text.lower():
        return ""
    match = re.search(r"douyin\.com/user/([^/?#\s)\]]+)", text, flags=re.I)
    return match.group(1) if match else ""


def infer_xhs_user_id(value):
    match = re.search(r"xiaohongshu\.com/user/profile/([^/?#\s)\]]+)", str(value or ""), flags=re.I)
    return match.group(1) if match else ""


def infer_youtube_channel_id(value):
    match = re.search(r"youtube\.com/channel/(UC[0-9A-Za-z_-]+)", str(value or ""), flags=re.I)
    return match.group(1) if match else ""


def infer_creator_id(row, platform):
    if platform == PLATFORM_BILI:
        return infer_bili_mid(row.get(HOME_FIELD))
    if platform == PLATFORM_DOUYIN:
        return infer_douyin_sec_uid(row.get(HOME_FIELD))
    if platform == PLATFORM_XIAOHONGSHU:
        return infer_xhs_user_id(row.get(HOME_FIELD))
    if platform == PLATFORM_YOUTUBE:
        return infer_youtube_channel_id(row.get(HOME_FIELD))
    return ""


def creator_platform_id(row, platform=None):
    value = cell_text(row.get(CREATOR_PLATFORM_ID_FIELD))
    if value:
        return value
    if platform:
        return infer_creator_id(row, platform)
    return ""


def creator_home_url(row, platform=None):
    return cell_text(row.get(HOME_FIELD))


def creator_tracking_enabled(row, platform=None):
    return checkbox_enabled(row.get(TRACK_FIELD))


def load_local_creator_rows(path, include_untracked=False):
    """Load local creator records and expose the legacy field names to crawlers.

    ``include_untracked=True`` 是给"显式点名一位博主"那条路用的（界面上的「🔥 抓取爆款」）：
    用户点下去就是要这一位，跟踪开关不该拦——那开关是给定时/整库采集筛人用的。
    默认 False，所有既有调用方的口径不变。
    """
    source = Path(path)
    if not source.is_file():
        raise RuntimeError(f"Local creator file not found: {source}")
    try:
        payload = json.loads(source.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Unable to read local creator file {source}: {exc}") from exc
    rows = payload.get("creators") if isinstance(payload, dict) else payload
    if not isinstance(rows, list):
        raise RuntimeError("Local creator file must be a list or an object with a creators list")
    tracked = []
    for index, raw in enumerate(rows, 1):
        if not isinstance(raw, dict):
            raise RuntimeError(f"Local creator item #{index} must be an object")
        row = dict(raw)
        row.setdefault(PLATFORM_FIELD, raw.get("platform", ""))
        row.setdefault(CREATOR_PLATFORM_ID_FIELD, raw.get("platform_id", ""))
        row.setdefault(HOME_FIELD, raw.get("homepage_url", ""))
        row.setdefault(TRACK_FIELD, raw.get("is_tracking", True))
        row.setdefault("博主名称", raw.get("name", ""))
        row.setdefault("_record_id", raw.get("id", ""))
        if not include_untracked and not checkbox_enabled(row.get(TRACK_FIELD, False)):
            continue
        tracked.append(row)
    if not tracked:
        raise RuntimeError(
            "Local creator file contains no creators"
            if include_untracked
            else "Local creator file contains no tracked creators"
        )
    return tracked


def video_platform(row):
    platform = first_platform(row.get(PLATFORM_FIELD))
    if platform:
        return platform
    return ""


def video_platform_id(row):
    return cell_text(row.get(VIDEO_PLATFORM_ID_FIELD))
