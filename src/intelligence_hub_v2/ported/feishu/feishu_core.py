# ruff: noqa
# TODO(v2-adapt): V1 的飞书 Base 访问层，未适配。它把"仓库根"当运行目录：ROOT=Path(__file__).parent
# 推导出 feishu-base-config.json（真实 token 文件）与 downloads/、downloads/manifests/，并且
# run_command(cwd=ROOT) 把 lark-cli 子进程和 .tmp-lark 载荷全落在那个根下 —— 搬进来后 ROOT 指向
# 本包目录，真跑就会往 src/ 里写。它还吃 V1 的 config dict 与镜像库中文字段名。
# 适配要做的那件事：路径/凭证改由 core/config.py + storage/files 供给，把 lark-cli 调用面收进
# infra/subprocess 形态的封装，失败原文进清单而不是 print。
"""Shared Feishu Base API helpers and common project utilities.

Extracted from ``download_bili_following_latest.py`` to decouple generic
Feishu operations from Bilibili-specific crawling logic.  All scripts
that previously did ``import download_bili_following_latest as bili`` and
only used general utilities should now ``import feishu_core as fc``.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from intelligence_hub_v2.ported.v1_shared import cross_platform_model as cross_model
from intelligence_hub_v2.ported.feishu import lark_cli_runtime
from intelligence_hub_v2.ported.v1_shared import platform_schema as ps

# ---------------------------------------------------------------------------
# Project-level path constants
# ---------------------------------------------------------------------------

ROOT: Path = Path(__file__).resolve().parent
CONFIG_PATH: Path = ROOT / "feishu-base-config.json"
DOWNLOAD_ROOT: Path = ROOT / "downloads"
MANIFEST_ROOT: Path = DOWNLOAD_ROOT / "manifests"
ARCHIVE_PATH: Path = DOWNLOAD_ROOT / "download-archive.txt"
BILIBILI_DOWNLOAD_SCRIPT: Path = (
    ROOT / ".agents" / "skills" / "bilibili-download" / "scripts" / "download_bilibili.py"
)

# ---------------------------------------------------------------------------
# Tiny helpers
# ---------------------------------------------------------------------------


def now_str() -> str:
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def ts_slug() -> str:
    return datetime.now().strftime("%Y%m%d-%H%M%S")


def add_phase_seconds(manifest: dict, phase: str, seconds: float) -> None:
    timings = manifest.setdefault("timings", {})
    phases = timings.setdefault("phase_seconds", {})
    phases[phase] = round(float(phases.get(phase) or 0) + float(seconds), 3)


def set_total_seconds(manifest: dict, started_perf: float) -> None:
    timings = manifest.setdefault("timings", {})
    timings["total_seconds"] = round(time.perf_counter() - started_perf, 3)


def date_part(value: Any) -> str | None:
    return str(value or "").split(" ", 1)[0] if value else None


def parse_iso_date(value: str, *, option_name: str) -> "datetime.date":
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError as exc:
        raise ValueError(f"{option_name} must be YYYY-MM-DD, got {value!r}") from exc


def publish_date_in_window(
    value: Any, start_date: "datetime.date", end_date: "datetime.date"
) -> bool:
    part = date_part(value)
    if not part:
        return False
    try:
        published = parse_iso_date(part, option_name="published date")
    except ValueError:
        return False
    return start_date <= published <= end_date


def date_window_label(start_date: "datetime.date", end_date: "datetime.date") -> str:
    if start_date == end_date:
        return str(end_date)
    return f"{start_date}..{end_date}"


# ---------------------------------------------------------------------------
# JSON / manifest I/O
# ---------------------------------------------------------------------------


def load_json(path: str | Path) -> Any:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def write_manifest(path: Path, manifest: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(manifest, f, ensure_ascii=False, indent=2)
    tmp.replace(path)


def safe_json_from_stdout(stdout: str) -> dict:
    decoder = json.JSONDecoder()
    start = stdout.find("{")
    if start < 0:
        raise RuntimeError(f"command did not return JSON: {stdout[:500]}")
    obj, _ = decoder.raw_decode(stdout[start:])
    return obj


# ---------------------------------------------------------------------------
# Command execution
# ---------------------------------------------------------------------------


def command_env() -> dict[str, str]:
    return lark_cli_runtime.command_env()


def normalize_command(args: list[str]) -> list[str]:
    if not args:
        return args
    if lark_cli_runtime.is_lark_cli_command(args):
        return lark_cli_runtime.normalize_lark_cli_command(args, env=command_env())
    executable = shutil.which(args[0]) or args[0]
    suffix = Path(executable).suffix.lower()
    if suffix in {".cmd", ".bat"}:
        return ["cmd", "/c", executable, *args[1:]]
    return [executable, *args[1:]]


def run_command(
    args: list[str], *, timeout: int | None = None, check: bool = True
) -> subprocess.CompletedProcess:
    if lark_cli_runtime.is_lark_cli_command(args):
        return lark_cli_runtime.run_lark_cli_command(
            args,
            cwd=ROOT,
            env=command_env(),
            timeout=timeout,
            check=check,
        )
    result = subprocess.run(
        normalize_command(args),
        cwd=ROOT,
        env=command_env(),
        text=True,
        encoding="utf-8",
        errors="replace",
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=timeout,
    )
    if check and result.returncode != 0:
        raise RuntimeError(
            "command failed\n"
            f"args: {args}\n"
            f"exit: {result.returncode}\n"
            f"stdout:\n{result.stdout[-2000:]}\n"
            f"stderr:\n{result.stderr[-2000:]}"
        )
    return result


# ---------------------------------------------------------------------------
# Core lark-cli / Feishu Base operations
# ---------------------------------------------------------------------------


def run_lark(config: dict[str, Any], base_args: list[str], *, timeout: int = 60) -> dict[str, Any]:
    args = ["lark-cli", "--profile", config["profile"], "base", *base_args, "--format", "json"]
    result = run_command(args, timeout=timeout)
    data = safe_json_from_stdout(result.stdout)
    if not data.get("ok"):
        raise RuntimeError(
            f"lark-cli returned not ok: {json.dumps(data, ensure_ascii=False)[:2000]}"
        )
    return data


def load_config() -> dict[str, Any]:
    with CONFIG_PATH.open("r", encoding="utf-8") as f:
        return json.load(f)


def field_names(config: dict[str, Any], table_id: str) -> dict[str, Any]:
    data = run_lark(
        config,
        [
            "+field-list",
            "--as",
            "user",
            "--base-token",
            config["base_token"],
            "--table-id",
            table_id,
        ],
    )
    return {field["name"]: field for field in data["data"]["fields"]}


def list_records(config: dict[str, Any], table_id: str, fields: list[str]) -> list[dict[str, Any]]:
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
            "200",
            "--offset",
            str(offset),
        ]
        for field in fields:
            args.extend(["--field-id", field])
        data = run_lark(config, args)
        payload = data["data"]
        names = payload["fields"]
        for record_id, values in zip(payload["record_id_list"], payload["data"]):
            row = dict(zip(names, values))
            row["_record_id"] = record_id
            rows.append(row)
        if not payload.get("has_more"):
            break
        offset += 200
    return rows


# ---------------------------------------------------------------------------
# Batch write operations
# ---------------------------------------------------------------------------


def batch_create_video_records(config: dict[str, Any], rows: list[list[Any]]) -> list[str]:
    if not rows:
        return []
    table_id = config["tables"]["videos"]["table_id"]
    fields = [
        "视频标题",
        "平台",
        "平台视频ID",
        "AID",
        "视频链接",
        "关联博主",
        "发布时间",
        "时长秒",
        "视频文件路径",
        "元数据文件路径",
        "视频文案路径",
        "封面文件路径",
        "评论文件路径",
        "评论抓取状态",
        "已抓评论数",
        "视频下载状态",
        "音频状态",
        "转写状态",
        "最近采集时间",
    ]
    created_ids: list[str] = []
    tmp_dir = ROOT / ".tmp-lark"
    tmp_dir.mkdir(exist_ok=True)
    for start in range(0, len(rows), 200):
        payload = {"fields": fields, "rows": rows[start : start + 200]}
        with tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", suffix=".json", dir=tmp_dir, delete=False
        ) as f:
            json.dump(payload, f, ensure_ascii=False)
            payload_path = Path(f.name)
        try:
            data = run_lark(
                config,
                [
                    "+record-batch-create",
                    "--as",
                    "user",
                    "--base-token",
                    config["base_token"],
                    "--table-id",
                    table_id,
                    "--json",
                    f"@{payload_path.relative_to(ROOT)}",
                ],
                timeout=120,
            )
        finally:
            payload_path.unlink(missing_ok=True)
        created_ids.extend(data["data"].get("record_id_list", []))
    return created_ids


def batch_create_metric_snapshots(
    config: dict[str, Any],
    items: list[dict[str, Any]],
    *,
    platform: str = ps.PLATFORM_BILI,
    default_data_source: str = "bilibili-download-metadata",
) -> list[str]:
    if not items:
        return []
    table_id = config["tables"]["video_metric_snapshots"]["table_id"]
    available = field_names(config, table_id)
    base_fields = [
        "关联视频",
        "快照时间",
        "播放量",
        "点赞量",
        "投币数",
        "收藏数",
        "分享数",
        "评论数",
        "弹幕数",
        "粉丝数快照",
        "备注",
    ]
    optional_fields = [
        cross_model.CHECKPOINT_FIELD,
        cross_model.SNAPSHOT_KEY_FIELD,
        cross_model.TARGET_SNAPSHOT_TIME_FIELD,
        cross_model.PUBLISH_AGE_HOURS_FIELD,
        cross_model.CAPTURE_LAG_MINUTES_FIELD,
        "平台",
        cross_model.DATA_SOURCE_FIELD,
        "视频标题",
        cross_model.CONTENT_WORK_LINK_FIELD,
    ]
    fields = base_fields + [field for field in optional_fields if field in available]
    rows: list[list[Any]] = []
    for item in items:
        metrics = item.get("metrics") or {}
        if not metrics:
            continue
        checkpoint = item.get("checkpoint") or cross_model.CHECKPOINT_INITIAL
        values = {
            "关联视频": [{"id": item["video_record_id"]}],
            "快照时间": item.get("snapshot_time") or now_str(),
            "播放量": metrics.get("播放量"),
            "点赞量": metrics.get("点赞量"),
            "投币数": metrics.get("投币数"),
            "收藏数": metrics.get("收藏数"),
            "分享数": metrics.get("分享数"),
            "评论数": metrics.get("评论数"),
            "弹幕数": metrics.get("弹幕数"),
            "粉丝数快照": metrics.get("粉丝数快照"),
            "备注": (
                f"平台视频ID={item.get('platform_video_id') or item.get('bvid')}; "
                f"{metrics.get('备注') or ''}; local={item.get('metrics_path')}"
            ),
            cross_model.CHECKPOINT_FIELD: checkpoint,
            cross_model.SNAPSHOT_KEY_FIELD: item.get("snapshot_key")
            or cross_model.snapshot_unique_key(item["video_record_id"], checkpoint),
            cross_model.TARGET_SNAPSHOT_TIME_FIELD: item.get("target_snapshot_time"),
            cross_model.PUBLISH_AGE_HOURS_FIELD: item.get("publish_age_hours"),
            cross_model.CAPTURE_LAG_MINUTES_FIELD: item.get("capture_lag_minutes"),
            "平台": platform,
            cross_model.DATA_SOURCE_FIELD: item.get("data_source") or default_data_source,
            "视频标题": item.get("video_title"),
            cross_model.CONTENT_WORK_LINK_FIELD: item.get("content_work_links") or [],
        }
        rows.append([values.get(field) for field in fields])
    if not rows:
        return []
    created_ids: list[str] = []
    tmp_dir = ROOT / ".tmp-lark"
    tmp_dir.mkdir(exist_ok=True)
    for start in range(0, len(rows), 200):
        payload = {"fields": fields, "rows": rows[start : start + 200]}
        with tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", suffix=".json", dir=tmp_dir, delete=False
        ) as f:
            json.dump(payload, f, ensure_ascii=False)
            payload_path = Path(f.name)
        try:
            data = run_lark(
                config,
                [
                    "+record-batch-create",
                    "--as",
                    "user",
                    "--base-token",
                    config["base_token"],
                    "--table-id",
                    table_id,
                    "--json",
                    f"@{payload_path.relative_to(ROOT)}",
                ],
                timeout=120,
            )
        finally:
            payload_path.unlink(missing_ok=True)
        created_ids.extend(data["data"].get("record_id_list", []))
    return created_ids


def update_video_record(
    config: dict[str, Any], record_id: str, patch: dict[str, Any]
) -> dict[str, Any]:
    tmp_dir = ROOT / ".tmp-lark"
    tmp_dir.mkdir(exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", suffix=".json", dir=tmp_dir, delete=False
    ) as f:
        json.dump(patch, f, ensure_ascii=False)
        payload_path = Path(f.name)
    try:
        return run_lark(
            config,
            [
                "+record-upsert",
                "--as",
                "user",
                "--base-token",
                config["base_token"],
                "--table-id",
                config["tables"]["videos"]["table_id"],
                "--record-id",
                record_id,
                "--json",
                f"@{payload_path.relative_to(ROOT)}",
            ],
            timeout=60,
        )
    finally:
        payload_path.unlink(missing_ok=True)


def create_task_log(
    config: dict[str, Any],
    started_at: str,
    ended_at: str,
    success_count: int,
    failure_count: int,
    manifest_path: str | Path,
    summary: str,
    *,
    task_name: str = "下载关注博主最新三条视频",
    task_type: str = "视频列表采集",
    target_scope: str = "飞书博主表：持续跟踪博主，每个最新3条",
) -> None:
    if not config:
        # 纯本地模式没有飞书表可写；早先是 config["tables"] 直接抛 'NoneType' object is not
        # subscriptable，重试包装层会照着这个不明所以的信息再空转三轮。
        raise ValueError("create_task_log 需要飞书配置：本地模式请只写清单文件")
    table_id = config["tables"]["crawl_task_logs"]["table_id"]
    if failure_count == 0:
        status = "成功"
    elif success_count == 0:
        status = "失败"
    else:
        status = "部分失败"
    payload = {
        "fields": [
            "任务名称",
            "开始时间",
            "结束时间",
            "任务类型",
            "状态",
            "目标范围",
            "成功数量",
            "失败数量",
            "错误摘要",
            "日志文件路径",
        ],
        "rows": [
            [
                task_name,
                started_at,
                ended_at,
                task_type,
                status,
                target_scope,
                success_count,
                failure_count,
                summary[:1000] if summary else "",
                str(manifest_path),
            ]
        ],
    }
    tmp_dir = ROOT / ".tmp-lark"
    tmp_dir.mkdir(exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", suffix=".json", dir=tmp_dir, delete=False
    ) as f:
        json.dump(payload, f, ensure_ascii=False)
        payload_path = Path(f.name)
    try:
        run_lark(
            config,
            [
                "+record-batch-create",
                "--as",
                "user",
                "--base-token",
                config["base_token"],
                "--table-id",
                table_id,
                "--json",
                f"@{payload_path.relative_to(ROOT)}",
            ],
            timeout=60,
        )
    finally:
        payload_path.unlink(missing_ok=True)
