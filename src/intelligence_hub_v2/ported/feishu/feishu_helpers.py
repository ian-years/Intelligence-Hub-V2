# ruff: noqa
# TODO(v2-adapt): V1 的飞书批量写入助手，未适配。它与 lark-cli 有一条隐式契约：载荷先写进
# ROOT/.tmp-lark，再以 `--json @<相对 ROOT 的路径>` 交给子进程，所以它必须与 fc.run_command 的
# cwd=ROOT 同根；搬进来后那个根是本包目录而不是 V1 仓库根，写回（create/upsert/delete）在 V2
# 里也没有任何调用方。
# 适配要做的那件事：载荷落到 data/ 下的受控目录并传绝对路径（或直接废掉写回，等镜像库并进主库
# 时改成 Repository 写入），config 换成 V2 的 Pydantic 配置模型而不是 V1 dict。
from __future__ import annotations

import json
import tempfile
from pathlib import Path
from typing import Any

from intelligence_hub_v2.ported.feishu import feishu_core as fc


ROOT = Path(__file__).resolve().parent


def run_json_payload(
    config: dict[str, Any],
    table_id: str,
    command: str,
    payload: Any,
    *,
    record_id: str | None = None,
    timeout: int = 120,
) -> dict[str, Any]:
    tmp_dir = ROOT / ".tmp-lark"
    tmp_dir.mkdir(exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", suffix=".json", dir=tmp_dir, delete=False
    ) as handle:
        json.dump(payload, handle, ensure_ascii=False)
        payload_path = Path(handle.name)
    try:
        args = [
            command,
            "--as",
            "user",
            "--base-token",
            config["base_token"],
            "--table-id",
            table_id,
        ]
        if record_id:
            args.extend(["--record-id", record_id])
        args.extend(["--json", f"@{payload_path.relative_to(ROOT)}"])
        return fc.run_lark(config, args, timeout=timeout)
    finally:
        payload_path.unlink(missing_ok=True)


def create_records(
    config: dict[str, Any], table_id: str, fields: list[str], rows: list[list[Any]]
) -> list[str]:
    if not rows:
        return []
    record_ids: list[str] = []
    for start in range(0, len(rows), 200):
        payload = {"fields": fields, "rows": rows[start : start + 200]}
        data = run_json_payload(config, table_id, "+record-batch-create", payload)
        created = (data.get("data") or {}).get("record_id_list") or []
        if len(created) != len(payload["rows"]):
            raise RuntimeError(
                f"Feishu created {len(created)} records for {len(payload['rows'])} requested rows in {table_id}"
            )
        record_ids.extend(created)
    return record_ids


def update_record(
    config: dict[str, Any], table_id: str, record_id: str, patch: dict[str, Any]
) -> None:
    if not patch:
        return
    run_json_payload(config, table_id, "+record-upsert", patch, record_id=record_id, timeout=60)


def update_records_same_patch(
    config: dict[str, Any],
    table_id: str,
    record_ids: list[str],
    patch: dict[str, Any],
) -> None:
    if not record_ids or not patch:
        return
    # The current Feishu user identity rejects OpenAPIBatchUpdateRecords (800004135).
    # Record upsert is the verified writable path and keeps partial runs idempotent.
    for record_id in record_ids:
        update_record(config, table_id, record_id, patch)


def delete_records(config: dict[str, Any], table_id: str, record_ids: list[str]) -> None:
    if not record_ids:
        return
    for start in range(0, len(record_ids), 200):
        args = [
            "+record-delete",
            "--as",
            "user",
            "--base-token",
            config["base_token"],
            "--table-id",
            table_id,
            "--yes",
        ]
        for record_id in record_ids[start : start + 200]:
            args.extend(["--record-id", record_id])
        fc.run_lark(config, args, timeout=120)


def write_json_atomic(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_suffix(path.suffix + ".tmp")
    temp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    temp.replace(path)
