"""`tasks/backfill.py`：爆款回溯。这一整组用例的存在理由就是 V1 §7.22 那一次事故。

事故形状（`Intelligence-Hub/AGENTS.md §7.22`）：按钮写着某一位博主，argv 里却只有
`--videos-per-creator 5`，采集器按跟踪开关筛全库 → 库里两位 B站 博主开关都关着时拿到 0 条
→ `RuntimeError` 甩成 traceback。所以这里最要紧的一条不是"能挑出爆款"，而是
**点名这条路一次都不碰"取名单"**。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from tests.unit.tasks.conftest import (
    FakeAdapter,
    FakeBus,
    FakeConfig,
    FakeRegistry,
    make_ctx,
    make_video_meta,
)

from intelligence_hub_v2.errors import ListError, TaskCancelled
from intelligence_hub_v2.models.creator import CreatorDraft, CreatorRef
from intelligence_hub_v2.models.media import SingleFileArtifact
from intelligence_hub_v2.tasks.backfill import run_backfill
from intelligence_hub_v2.tasks.params import BackfillParams

PLATFORM = "bilibili"
PID = "mid_42"
URL = f"https://space.bilibili.com/{PID}"


def _ref() -> CreatorRef:
    return CreatorRef.model_validate({"platform": PLATFORM, "platform_id": PID, "profile_url": URL})


def _artifact(video: Any, dest: Path) -> SingleFileArtifact:
    path = dest / "media.mp4"
    path.write_bytes(b"x" * 32)
    return SingleFileArtifact(
        path=path, size_bytes=32, media_source="yt_dlp", cookie_rung="匿名", has_audio=True
    )


def _hit(video_id: str, likes: int | None) -> Any:
    return make_video_meta(PLATFORM, video_id, counts={"like_count": likes})


async def _seed(storage, *, tracking: bool) -> int:
    creator = await storage.creators.insert(
        CreatorDraft(
            platform=PLATFORM,
            platform_id=PID,
            name="老王",
            profile_url=URL,
            is_tracking=tracking,
        )
    )
    return creator.id


def _ctx(adapter: FakeAdapter, storage: Any, files: Any, config: FakeConfig | None = None):
    reg = FakeRegistry({PLATFORM: adapter}, {PLATFORM: config or FakeConfig()})
    return make_ctx(storage=storage, files=files, registry=reg, bus=FakeBus())


# --------------------------------------------------------------------------- §7.22 本体


async def test_a_backfill_of_an_untracked_creator_still_finds_her(storage, files) -> None:
    """**这条就是 §7.22 的看护**：开关关着的博主，点名回溯照样成。

    前置（防空转）：先当场证明"走名单那条路会拿到 0 条"——也就是 V1 事故的那个条件
    在这份 fixture 里**真的成立**。少了这一段，本用例在"哪天有人把 `find()` 换回
    `list_tracked()`"之前都可能一直绿着却什么也没挡。
    """
    await _seed(storage, tracking=False)

    # 前置（防空转）：先当场证明"走名单那条路会拿到 0 条"——也就是 V1 事故的那个条件
    # 在这份 fixture 里**真的成立**。少了这一段，本用例在"哪天有人把 `find()` 换回
    # `list_tracked()`"之前都可能一直绿着却什么也没挡。
    assert await storage.creators.list_tracked(platform=PLATFORM) == []

    adapter = FakeAdapter(
        PLATFORM,
        ref=_ref(),
        videos=[_hit("v1", 10)],
        artifact_factory=_artifact,  # type: ignore[arg-type]
    )
    ctx = _ctx(adapter, storage, files)

    result = await run_backfill(ctx, BackfillParams(creator_url=URL, platform=PLATFORM, top=1))

    assert result.status == "success", result.failures
    assert result.summary["downloaded"] == 1
    assert result.summary["creator_was_tracked"] == 0  # 说实话：这位本来没在跟踪
    assert adapter.download_calls == ["v1"]
    row = await storage.videos.find_by_platform_id(PLATFORM, "v1")
    assert row is not None and row.creator_id is not None


async def test_naming_a_creator_never_consults_the_list(
    storage, files, monkeypatch: pytest.MonkeyPatch
) -> None:
    """点名任务一次都不该"取名单"——`list_tracked` / `list_all` 一旦被调就是回归。

    比上一条更硬：直接把两条取名单的路炸掉。哪天实现漂回去（比如"找不到就退化成扫全库"），
    这条先红，而不是悄悄多抓一遍平台接口。
    """
    await _seed(storage, tracking=False)

    def boom(*_a: Any, **_kw: Any) -> Any:
        raise AssertionError("点名任务不许取名单（V1 §7.22）")

    monkeypatch.setattr(storage.creators, "list_tracked", boom)
    monkeypatch.setattr(storage.creators, "list_all", boom)

    adapter = FakeAdapter(
        PLATFORM,
        ref=_ref(),
        videos=[_hit("v1", 5)],
        artifact_factory=_artifact,  # type: ignore[arg-type]
    )
    result = await run_backfill(
        _ctx(adapter, storage, files), BackfillParams(creator_url=URL, platform=PLATFORM, top=1)
    )
    assert result.status == "success"
    assert await storage.creators.find(PLATFORM, PID) is not None


async def test_a_creator_not_in_the_library_never_borrows_someone_elses_list(
    storage, files
) -> None:
    """**这条是变异检查挖出来的**（`.scratch/mutation/run_backfill_guard.py` 的 M2）：
    库里没有点名的那位、但**另有别人**时，不许拿别人的身份去扫。

    为什么单独要一条：`test_a_creator_not_in_the_library_fails_without_touching_the_platform`
    那份库里一个博主都没有，于是"退化成 `list_all()[0]`"这种写法在它眼里与如实失败**完全同形**；
    而"绝不取名单"那条把 `list_all` 炸掉了，可它的前提是点名的人存在、根本走不到那一支。
    两个半边各挡一种退化，合起来挡不住第三种 —— 这就是"逐条绿不等于整体有效"。
    """
    await _seed(storage, tracking=True)  # 库里有一位（tracking 也开着），但不是被点名的那位
    other = await storage.creators.find(PLATFORM, PID)
    assert other is not None  # 前置：这份 fixture 里确实"有别人可拿"

    adapter = FakeAdapter(
        PLATFORM,
        ref=_ref(),  # 适配器会把这条链接认成 PID —— 让它认一个库里没有的身份
        videos=[_hit("v1", 9)],
        artifact_factory=_artifact,
    )

    async def unknown(_url: str) -> CreatorRef:
        return CreatorRef.model_validate(
            {
                "platform": PLATFORM,
                "platform_id": "nobody_here",
                "profile_url": "https://space.bilibili.com/nobody_here",
            }
        )

    adapter.parse_creator_url = unknown  # type: ignore[method-assign]
    ctx = _ctx(adapter, storage, files)

    result = await run_backfill(
        ctx,
        BackfillParams(creator_url="https://space.bilibili.com/nobody_here", platform=PLATFORM),
    )

    assert result.status == "failed"
    assert result.failures[0].error_kind == "CreatorNotFound"
    # 关键三条：不许扫、不许下、库里那条别人的作品一行都不许多出来
    assert adapter.list_calls == [], "退化成扫别人了"
    assert adapter.download_calls == []
    assert await storage.videos.count() == 0


async def test_a_creator_not_in_the_library_fails_without_touching_the_platform(
    storage, files
) -> None:
    """库里没有这位 → 如实红，**且一次枚举都不发**（不退化成扫全库的另一半）。"""
    adapter = FakeAdapter(PLATFORM, ref=_ref(), videos=[_hit("v1", 9)], artifact_factory=_artifact)  # type: ignore[arg-type]
    ctx = _ctx(adapter, storage, files)

    result = await run_backfill(ctx, BackfillParams(creator_url=URL, platform=PLATFORM))

    assert result.status == "failed"
    assert adapter.list_calls == []  # 没身份就没扫：这一条挡住"顺手全库拉一遍"
    assert adapter.download_calls == []
    (failure,) = result.failures
    assert failure.error_kind == "CreatorNotFound"
    assert "add_creator" in failure.error  # 文案得给出下一步，不能只说"没有"
    assert await storage.videos.count() == 0


# ----------------------------------------------------------------------- 扫描窗口 ≠ 交付数


async def test_the_scan_window_is_not_the_deliverable(storage, files) -> None:
    """`scan` 留空时窗口是配置的 4 倍（夹下限 20），而交付只有 `top` 条。

    这两个数必须**在清单里分开看得见**：`scanned=6, top=2` 说的是"看了 6 条挑 2 条"，
    与"只看最新 2 条"是两件事（后者压根不算回溯）。
    """
    await _seed(storage, tracking=True)
    videos = [_hit(f"v{i}", likes) for i, likes in enumerate([5, 300, 1, 90, 20, 3], start=1)]
    adapter = FakeAdapter(PLATFORM, ref=_ref(), videos=videos, artifact_factory=_artifact)  # type: ignore[arg-type]
    ctx = _ctx(adapter, storage, files, config=FakeConfig(videos_per_creator=8))

    result = await run_backfill(ctx, BackfillParams(creator_url=URL, platform=PLATFORM, top=2))

    assert adapter.list_calls[0]["limit"] == 32  # max(20, 8*4)
    assert adapter.download_calls == ["v2", "v4"]  # 300 与 90，不是枚举顺序的前两条
    assert result.summary["scanned"] == 6
    assert result.summary["top"] == 2
    assert result.summary["downloaded"] == 2
    assert "v2(like=300)" in str(result.summary["picked"])


async def test_an_explicit_scan_is_honoured(storage, files) -> None:
    await _seed(storage, tracking=True)
    adapter = FakeAdapter(PLATFORM, ref=_ref(), videos=[_hit("v1", 1)], artifact_factory=_artifact)  # type: ignore[arg-type]
    ctx = _ctx(adapter, storage, files)

    await run_backfill(ctx, BackfillParams(creator_url=URL, platform=PLATFORM, top=1, scan=7))
    assert adapter.list_calls[0]["limit"] == 7


# --------------------------------------------------------------------- "不知道"不许冒充爆款


async def test_a_missing_like_count_never_impersonates_a_hit(storage, files) -> None:
    """读数缺失排最后：一条"平台没给点赞"的作品不能因为缺数就被当成爆款收进来。"""
    await _seed(storage, tracking=True)
    videos = [_hit("unknown_a", None), _hit("low", 2), _hit("unknown_b", None), _hit("high", 7)]
    adapter = FakeAdapter(PLATFORM, ref=_ref(), videos=videos, artifact_factory=_artifact)  # type: ignore[arg-type]

    result = await run_backfill(
        _ctx(adapter, storage, files), BackfillParams(creator_url=URL, platform=PLATFORM, top=2)
    )

    assert adapter.download_calls == ["high", "low"]
    assert result.summary["unknown_metrics"] == 2


async def test_a_real_zero_like_outranks_a_missing_one(storage, files) -> None:
    """**变异检查 M3 逼出来的一条**：缺读数不是"0 赞"，两者不许同形。

    为什么上一条挡不住 M3（把 `None` 当 0 参与排序）：正的点赞数怎么都比 0 大，
    于是"缺数排最后"在 `[None, 2, None, 7]` 那种输入下**碰巧也成立**。
    要分辨只有一个形状：让缺数那条在枚举顺序里排在一条**真 0 赞**之前 ——
    正确处理是 `one, zero` 入选、`unknown` 垫底；`or 0` 那种写法会让 `unknown` 与 `zero`
    打平并按枚举顺序把 `unknown` 抬进去。少了这一格，"平台没给数"就能冒充"没人点赞"。
    """
    await _seed(storage, tracking=True)
    videos = [_hit("unknown", None), _hit("zero", 0), _hit("one", 1)]
    adapter = FakeAdapter(PLATFORM, ref=_ref(), videos=videos, artifact_factory=_artifact)

    result = await run_backfill(
        _ctx(adapter, storage, files), BackfillParams(creator_url=URL, platform=PLATFORM, top=2)
    )

    assert adapter.download_calls == ["one", "zero"]
    assert result.summary["unknown_metrics"] == 1


async def test_when_nothing_has_likes_the_manifest_says_it_did_not_rank(storage, files) -> None:
    """全都没读数时按枚举顺序交（稳定排序），但 `unknown_metrics == scanned` 把
    "这一次其实没按热度挑"说出口 —— 少了这一栏，清单与一次正常的爆款挑选完全同形。"""
    await _seed(storage, tracking=True)
    videos = [_hit(f"v{i}", None) for i in range(1, 4)]
    adapter = FakeAdapter(PLATFORM, ref=_ref(), videos=videos, artifact_factory=_artifact)  # type: ignore[arg-type]

    result = await run_backfill(
        _ctx(adapter, storage, files), BackfillParams(creator_url=URL, platform=PLATFORM, top=3)
    )

    assert adapter.download_calls == ["v1", "v2", "v3"]
    assert result.summary["unknown_metrics"] == result.summary["scanned"] == 3


# ------------------------------------------------------------------------- 失败与取消的形状


async def test_a_list_failure_keeps_the_original_text_and_does_not_raise(storage, files) -> None:
    """V1 那次是 `RuntimeError` 一路甩成 traceback。这里要求：红成一条 `list` 失败 + 原文。"""
    await _seed(storage, tracking=True)
    adapter = FakeAdapter(
        PLATFORM,
        ref=_ref(),
        list_error=ListError(PLATFORM, "list", "Request is blocked by server (412)"),
    )
    result = await run_backfill(
        _ctx(adapter, storage, files), BackfillParams(creator_url=URL, platform=PLATFORM)
    )

    assert result.status == "failed"  # 一条都没成 + 有失败 → failed（collect 同一套判据）
    (failure,) = result.failures
    assert failure.stage == "list"
    assert "Request is blocked by server (412)" in failure.error  # 原文，不是"枚举失败"那种转述
    assert result.summary["scanned"] == 0


async def test_cancel_during_the_scan_is_a_cancel_not_a_list_failure(storage, files) -> None:
    """取消不许被"这位博主枚举失败"那圈 catch 吞掉（collect 里同一条纪律，抄漏就是回归）。"""
    await _seed(storage, tracking=True)
    adapter = FakeAdapter(PLATFORM, ref=_ref(), list_error=TaskCancelled("user cancelled"))
    ctx = _ctx(adapter, storage, files)

    with pytest.raises(TaskCancelled):
        await run_backfill(ctx, BackfillParams(creator_url=URL, platform=PLATFORM))


async def test_a_single_download_failure_leaves_the_others_running(storage, files) -> None:
    """一条下不动记进 failures 并继续；**部分成功是 partial，不是 failed**。"""
    await _seed(storage, tracking=True)

    def picky(video: Any, dest: Path) -> SingleFileArtifact:
        if video.platform_video_id == "bad":
            raise RuntimeError("yt-dlp exit 1")
        return _artifact(video, dest)

    videos = [_hit("good1", 50), _hit("bad", 40), _hit("good2", 30)]
    adapter = FakeAdapter(PLATFORM, ref=_ref(), videos=videos, artifact_factory=picky)
    ctx = _ctx(adapter, storage, files)

    result = await run_backfill(ctx, BackfillParams(creator_url=URL, platform=PLATFORM, top=3))

    assert result.status == "partial"
    assert result.summary["downloaded"] == 2
    assert result.summary["failed"] == 1
    assert [f.stage for f in result.failures] == ["download"]
    assert "yt-dlp exit 1" in result.failures[0].error
