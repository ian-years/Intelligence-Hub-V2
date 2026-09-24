# ruff: noqa
# TODO(v2-adapt): V1 的第二博主源（独立 JSON 文件 downloads/launcher-state/creators.json + 模块级
# threading.RLock），未适配 —— V2 的博主唯一真源是主库 creators 表，"两个源"本身就是 §7.7 那条陷阱。
# 搬进来才咬人的形状：root 与 path 都不传时它退回 Path(__file__).parent/_DEFAULT_FILENAME，V1 里那
# 是仓库根，本份里却变成 src/.../ported/v1_shared/downloads/…（AGENTS.md §1 的"一切产物限制在 data/
# 下"在这里是反的）。适配要做的那件事：读走 storage/repositories/creators.py，需要 V1 那份 JSON 就
# 从 tools/migrate_from_v1.py 一次性导入，本文件随 §7.6 一起删。它不在飞书的 import 链上，是 V1 地面。
"""Small atomic JSON store for locally managed creator accounts."""

from __future__ import annotations

import json
import os
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable


class LocalCreatorStoreError(Exception):
    """Raised when the local creator store cannot be read or written."""


_REQUIRED_FIELDS = (
    "id",
    "name",
    "platform",
    "homepage_url",
    "platform_id",
    "cross_platform_identity",
    "collection_strategy",
    "is_tracking",
    "created_at",
    "updated_at",
    "source",
)
_DEFAULT_FILENAME = Path("downloads") / "launcher-state" / "creators.json"
_LOCK = threading.RLock()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class LocalCreatorStore:
    """Persist independent platform accounts in one JSON file.

    ``root`` is the project root; callers may provide ``path`` when an explicit
    store location is needed (for example, in tests).
    """

    def __init__(
        self,
        root: str | os.PathLike[str] | None = None,
        *,
        path: str | os.PathLike[str] | None = None,
    ) -> None:
        if path is not None:
            self.path = Path(path)
        else:
            self.path = (
                Path(root) / _DEFAULT_FILENAME
                if root is not None
                else Path(__file__).resolve().parent / _DEFAULT_FILENAME
            )

    def read(self) -> list[dict[str, Any]]:
        """Read all creators, accepting either a JSON array or ``{creators: []}``."""
        with _LOCK:
            if not self.path.exists():
                return []
            try:
                with self.path.open("r", encoding="utf-8") as handle:
                    payload = json.load(handle)
            except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise LocalCreatorStoreError(f"无法读取博主数据文件 {self.path}: {exc}") from exc
            if isinstance(payload, list):
                creators = payload
            elif isinstance(payload, dict) and isinstance(payload.get("creators"), list):
                creators = payload["creators"]
            else:
                raise LocalCreatorStoreError(
                    f"博主数据文件格式无效，应为数组或 {{creators: []}}: {self.path}"
                )
            if not all(isinstance(item, dict) for item in creators):
                raise LocalCreatorStoreError(f"博主数据文件包含非对象记录: {self.path}")
            return [dict(item) for item in creators]

    def list_creators(
        self, *, platform: str | None = None, is_tracking: bool | None = None
    ) -> list[dict[str, Any]]:
        """Return creators optionally filtered by platform and tracking state."""
        creators = self.read()
        if platform is not None:
            creators = [item for item in creators if item.get("platform") == platform]
        if is_tracking is not None:
            creators = [item for item in creators if item.get("is_tracking") is is_tracking]
        return creators

    # Convenient aliases for callers that prefer collection terminology.
    get_all = read
    list = list_creators

    def upsert(self, creator: dict[str, Any]) -> dict[str, Any]:
        """Insert or update one account, keyed by ``platform`` + ``platform_id``."""
        if not isinstance(creator, dict):
            raise LocalCreatorStoreError("博主记录必须是对象")
        platform = str(creator.get("platform") or "").strip()
        platform_id = str(creator.get("platform_id") or "").strip()
        if not platform or not platform_id:
            raise LocalCreatorStoreError("博主记录必须包含非空 platform 和 platform_id")
        with _LOCK:
            creators = self.read()
            index = next(
                (
                    i
                    for i, item in enumerate(creators)
                    if item.get("platform") == platform
                    and str(item.get("platform_id") or "") == platform_id
                ),
                None,
            )
            now = _now()
            if index is None:
                record = dict(creator)
                record.setdefault("id", str(uuid.uuid4()))
                record.setdefault("created_at", now)
            else:
                record = dict(creators[index])
                record.update(creator)
                record.setdefault("created_at", creators[index].get("created_at") or now)
            record.update(
                {
                    "platform": platform,
                    "platform_id": platform_id,
                    "updated_at": now,
                    "source": "local",
                }
            )
            for field, default in (
                ("name", ""),
                ("homepage_url", ""),
                ("cross_platform_identity", ""),
                ("collection_strategy", ""),
            ):
                record.setdefault(field, default)
            record.setdefault("is_tracking", True)
            if index is None:
                creators.append(record)
            else:
                creators[index] = record
            self._write(creators)
            return dict(record)

    def upsert_many(self, creators: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
        return [self.upsert(creator) for creator in creators]

    def delete(self, platform: str, platform_id: str) -> bool:
        """Remove one account keyed by ``platform`` + ``platform_id``; True when a row went away.

        只动对标库这一份 JSON：已下载的作品、口播稿与磁盘文件都不在它的管辖范围内。
        """
        platform = str(platform or "").strip()
        platform_id = str(platform_id or "").strip()
        if not platform or not platform_id:
            raise LocalCreatorStoreError("删除博主必须给出非空 platform 和 platform_id")
        with _LOCK:
            creators = self.read()
            kept = [
                item
                for item in creators
                if not (
                    item.get("platform") == platform
                    and str(item.get("platform_id") or "") == platform_id
                )
            ]
            if len(kept) == len(creators):
                return False
            self._write(kept)
            return True

    def _write(self, creators: list[dict[str, Any]]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.path.with_name(
            f".{self.path.name}.{os.getpid()}.{threading.get_ident()}.tmp"
        )
        try:
            with temporary.open("w", encoding="utf-8", newline="\n") as handle:
                json.dump(creators, handle, ensure_ascii=False, indent=2)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, self.path)
        except OSError as exc:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass
            raise LocalCreatorStoreError(f"无法写入博主数据文件 {self.path}: {exc}") from exc


__all__ = ["LocalCreatorStore", "LocalCreatorStoreError"]
