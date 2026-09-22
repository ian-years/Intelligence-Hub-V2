"""`tests/unit/tasks/` 共用的假件与 fixture。

这一层的被测对象是 handler 与 runner/scheduler，它们只通过 `TaskContext` 接触世界。
所以假件都做成"能塞进 `TaskContext` 的最小实现"：`FakeRegistry` 顶替
`PlatformRegistry`、`FakeAdapter` 顶替 `PlatformAdapter`、`FakeBus` 顶替 `EventBus`。

`storage` / `files` 用**真的**：SQLite 内存库与 tmp 目录，不为难自己造假 Repository，
也不让"入库/查重/清单路径"这些真实行为被 mock 糊过去（V1 §1.3 的老毛病就是 mock 掉了
唯一会被验的那一步）。
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx

from intelligence_hub_v2.errors import PlatformError
from intelligence_hub_v2.infra.cookies import CookieManager
from intelligence_hub_v2.models.creator import CreatorProfile, CreatorRef
from intelligence_hub_v2.models.media import SingleFileArtifact
from intelligence_hub_v2.models.task import TaskKind
from intelligence_hub_v2.models.transcript import Transcript
from intelligence_hub_v2.models.video import VideoMeta
from intelligence_hub_v2.platforms.base import (
    AdapterDeps,
    Capabilities,
    HealthReport,
    PlatformConfig,
    RateLimitConfig,
)
from intelligence_hub_v2.storage.db import SqliteStorage
from intelligence_hub_v2.storage.files import FileStorage
from intelligence_hub_v2.tasks.definition import CancelToken, TaskContext, TaskDefinition
from intelligence_hub_v2.tasks.params import PreflightParams


class FakeLogger:
    """structlog BoundLogger 的最小替身：把调用记下来，别真的往 stderr 打。"""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str, dict[str, Any]]] = []

    def bind(self, **_: Any) -> FakeLogger:
        return self

    def _rec(self, level: str, event: str, kw: dict[str, Any]) -> None:
        self.calls.append((level, event, kw))

    def debug(self, event: str, **kw: Any) -> None:
        self._rec("debug", event, kw)

    def info(self, event: str, **kw: Any) -> None:
        self._rec("info", event, kw)

    def warning(self, event: str, **kw: Any) -> None:
        self._rec("warning", event, kw)

    def error(self, event: str, **kw: Any) -> None:
        self._rec("error", event, kw)

    def exception(self, event: str, **kw: Any) -> None:
        self._rec("exception", event, kw)


class FakeBus:
    """收集事件的 EventBus 替身（`publish` 只记账，不落库不广播）。"""

    def __init__(self) -> None:
        self.events: list[Any] = []

    async def publish(self, event: Any) -> None:
        self.events.append(event)

    def subscribe(self, types: Any = None, task_id: str | None = None) -> AsyncIterator[Any]:
        async def _empty() -> AsyncIterator[Any]:
            return
            yield  # pragma: no cover - 让它成为 async generator

        return _empty()

    async def replay(self, task_id: str, since: datetime | None = None) -> list[Any]:
        return []

    async def shutdown(self) -> None:
        return None


def make_video_meta(
    platform: str, video_id: str, *, title: str = "t", creator: CreatorRef | None = None
) -> VideoMeta:
    ref = creator or CreatorRef.model_validate(
        {
            "platform": platform,
            "platform_id": "c1",
            "profile_url": f"https://{platform}.com/user/c1",
        }
    )
    return VideoMeta.model_validate(
        {
            "platform": platform,
            "platform_video_id": video_id,
            "creator_ref": ref,
            "title": title,
            "webpage_url": f"https://{platform}.com/video/{video_id}",
        }
    )


class FakeAdapter:
    """可编程的平台适配器替身。只实现 handler 会用到的方法。"""

    def __init__(
        self,
        platform: str,
        *,
        videos: list[VideoMeta] | None = None,
        list_error: Exception | None = None,
        artifact_factory: Callable[[VideoMeta, Path], SingleFileArtifact] | None = None,
        download_error: Exception | None = None,
        profile: CreatorProfile | None = None,
        ref: CreatorRef | None = None,
        parse_error: Exception | None = None,
        health: HealthReport | None = None,
        subtitles: Transcript | None = None,
        subtitle_error: Exception | None = None,
        capabilities: Capabilities | None = None,
    ) -> None:
        self.platform = platform
        self.name = platform
        self._videos = videos or []
        self._list_error = list_error
        self._artifact_factory = artifact_factory
        self._download_error = download_error
        self._profile = profile
        self._ref = ref
        self._parse_error = parse_error
        self._health = health
        self._subtitles = subtitles
        self._subtitle_error = subtitle_error
        self._capabilities = capabilities
        self.download_calls: list[str] = []
        self.list_calls: list[dict[str, Any]] = []
        self.subtitle_calls: list[str] = []

    @property
    def capabilities(self) -> Capabilities:
        return self._capabilities or Capabilities(
            needs_browser=False,
            needs_cookies=False,
            cookie_variants=("none",),
            supports_subtitles=self._subtitles is not None or self._subtitle_error is not None,
            supports_dash_split=False,
            list_strategy="yt_dlp_flat",
            media_strategy="yt_dlp",
        )

    async def healthcheck(self) -> HealthReport:
        if self._health is not None:
            return self._health
        return HealthReport(platform=self.platform, status="ok", checked_at=datetime.now(UTC))

    async def parse_creator_url(self, url: str) -> CreatorRef:
        if self._parse_error is not None:
            raise self._parse_error
        assert self._ref is not None
        return self._ref

    async def fetch_creator_profile(self, ref: CreatorRef) -> CreatorProfile:
        assert self._profile is not None
        return self._profile

    async def list_creator_videos(
        self, ref: CreatorRef, *, since: datetime | None = None, limit: int = 30
    ) -> AsyncIterator[VideoMeta]:
        self.list_calls.append({"ref": ref, "since": since, "limit": limit})
        if self._list_error is not None:
            raise self._list_error
        for video in self._videos:
            yield video

    async def download_media(
        self, video: VideoMeta, dest: Path, *, on_progress: Any = None
    ) -> SingleFileArtifact:
        self.download_calls.append(video.platform_video_id)
        if self._download_error is not None:
            raise self._download_error
        assert self._artifact_factory is not None
        return self._artifact_factory(video, dest)

    async def fetch_subtitles(self, video: VideoMeta) -> Transcript | None:
        self.subtitle_calls.append(video.platform_video_id)
        if self._subtitle_error is not None:
            raise self._subtitle_error
        return self._subtitles


class FakeConfig:
    """`PlatformConfig` 的最小替身：handler 只读 enabled / videos_per_creator / rate_limit。"""

    def __init__(
        self,
        *,
        enabled: bool = True,
        videos_per_creator: int = 30,
        per_creator_seconds: float = 0.0,
        per_minute: int = 60,
    ) -> None:
        self.enabled = enabled
        self.videos_per_creator = videos_per_creator
        self.rate_limit = RateLimitConfig(
            per_minute=per_minute, per_creator_seconds=per_creator_seconds
        )
        self.display_name = "fake"

    def model_dump(self, **_kw: Any) -> dict[str, Any]:
        """给 scheduler 的配置快照用（快照只做审计，字段不必全）。"""
        return {"enabled": self.enabled, "display_name": self.display_name}


class FakeRegistry:
    """`PlatformRegistry` 替身：按名字拿假适配器 + 假配置。"""

    def __init__(self, adapters: dict[str, FakeAdapter], configs: dict[str, Any]) -> None:
        self._adapters = adapters
        self._configs = configs

    def implemented_platforms(self) -> list[str]:
        return sorted(self._adapters)

    def enabled_platforms(self) -> list[str]:
        return [
            name
            for name, cfg in self._configs.items()
            if getattr(cfg, "enabled", False) and name in self._adapters
        ]

    def get(self, name: str) -> FakeAdapter:
        if name not in self._adapters:
            raise PlatformError(name, "task", f"no adapter {name}")
        return self._adapters[name]

    def config_for(self, name: str) -> Any:
        return self._configs[name]

    def capabilities(self, name: str) -> Capabilities:
        return self._adapters[name].capabilities

    def inconsistencies(self) -> list[str]:
        return []

    def invalidate(self, name: str | None = None) -> None:
        return None


def make_deps(config: PlatformConfig | FakeConfig) -> Any:
    """造一个 `AdapterDeps` 替身（handler 只用 `deps.http` / `deps.logger` / `deps.bridge`）。

    `http` 用 `MockTransport` 而不是默认真传输 —— 这是 `docs/lessons.md` 坑 19 的直接后果：
    Windows 上每建一个默认真传输的 `httpx.AsyncClient` 要 ~2.1 秒（去碰系统证书存储）。
    handler 测试根本不发消息，给个假传输既省时间又不会误打网络。
    """
    transport = httpx.MockTransport(lambda request: httpx.Response(200, json={}))
    return AdapterDeps(
        config=config,  # type: ignore[arg-type]
        storage=None,  # type: ignore[arg-type]
        events=FakeBus(),  # type: ignore[arg-type]
        http=httpx.AsyncClient(transport=transport),
        logger=FakeLogger(),  # type: ignore[arg-type]
        cookies=CookieManager(FileStorage(Path("data"))),
        bridge=None,
    )


def make_ctx(
    *,
    storage: SqliteStorage,
    files: FileStorage,
    registry: FakeRegistry,
    bus: FakeBus,
    task_id: str = "task-1",
    config: PlatformConfig | FakeConfig | None = None,
) -> TaskContext:
    cfg = config or FakeConfig()
    return TaskContext(
        task_id=task_id,
        deps=make_deps(cfg),
        adapters=registry,  # type: ignore[arg-type]
        events=bus,  # type: ignore[arg-type]
        storage=storage,
        files=files,
        cancel_token=CancelToken(),
        workdir=files.tmp_dir(task_id, create=True),
        logger=FakeLogger(),  # type: ignore[arg-type]
        config_snapshot={},
        task_name="test",
    )


class FakeDepsFactory:
    """`DepsFactory` 替身：给哪个平台都返回同一份 deps（runner 只用 `.default()` / 调用）。"""

    def __init__(self, deps: Any) -> None:
        self._deps = deps

    def __call__(self, platform: str) -> Any:
        return self._deps

    def default(self) -> Any:
        return self._deps


def make_definition(
    runner: Any,
    *,
    name: str = "echo",
    platforms: tuple[str, ...] = (),
    timeout_seconds: int | None = None,
    params_schema: Any = None,
) -> Any:
    """造一个测试用的 `TaskDefinition`（runner 由调用方给，其余字段填无害默认）。"""
    return TaskDefinition(
        name=name,
        display_name=name,
        kind=TaskKind.PREFLIGHT,
        params_schema=params_schema or PreflightParams,
        platforms=platforms,
        requires=(),
        timeout_seconds=timeout_seconds,
        cancellable=True,
        runner=runner,
    )
