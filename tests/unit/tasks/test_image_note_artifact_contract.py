"""ADR-0019：图文笔记的产物表达方式，以及"`media_aux_paths_json` 里不一定是音频"这条边界。

为什么这些用例值得单独一份：它们钉的不是某个函数的返回值，而是一句**跨模块的约定** ——
`videos.media_aux_paths_json` 那一列从今天起装的是"同一条作品的其他产物文件"
（DASH 的音频轨 **或** 图文笔记的第 2~N 张原图）。列名里没有"audio"，
而读它的那一侧（`postprocess._audio_source`）曾经**只看非空**就当成音频轨喂给 ffmpeg。

所以每条断言都是"两个模块对同一份数据的解释必须一致"，不是"某个输入给出某个输出"。
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from intelligence_hub_v2.models.media import (
    SingleFileArtifact,
    audio_path_of,
    total_size_bytes,
)
from intelligence_hub_v2.models.video import Video
from intelligence_hub_v2.tasks import postprocess as post_mod
from intelligence_hub_v2.tasks.collect import _artifact_fields

DATA_DIR = Path("data")


class _Files:
    """只够 `_artifact_fields` / `_audio_source` 用到的那半个 `FileStorage`。

    不引真 `FileStorage`：这两个函数要的只是"绝对 → 相对 `data/`"这一步换算，
    而真 `FileStorage` 会顺手把 `data/` 的整棵目录约定牵进用例（那是另一份契约的事）。
    """

    def rel(self, path: Path) -> str:
        return path.relative_to(DATA_DIR).as_posix()

    def abs(self, rel: str) -> Path:
        return DATA_DIR / rel


@pytest.fixture
def ctx() -> Any:
    return type("_Ctx", (), {"files": _Files()})()


def _artifact(**overrides: object) -> SingleFileArtifact:
    base: dict[str, object] = {
        "path": DATA_DIR / "media/xiaohongshu/note/01.jpg",
        "size_bytes": 300_000,
        "media_source": "page_play_url",
        "has_audio": False,
        "has_video": False,
    }
    base.update(overrides)
    return SingleFileArtifact.model_validate(base)


def _row(**overrides: object) -> Video:
    base: dict[str, object] = {
        "id": 1,
        "platform": "xiaohongshu",
        "platform_video_id": "note1",
        "title": "t",
        "created_at": datetime.now(UTC),
        "updated_at": datetime.now(UTC),
    }
    base.update(overrides)
    return Video.model_validate(base)


# --------------------------------------------------------------------------- #
# 生产侧：extra_paths 落进 aux 列
# --------------------------------------------------------------------------- #


def test_extra_paths_land_in_the_aux_column_and_the_main_file_stays_the_first_one(
    ctx: Any,
) -> None:
    """图文笔记入库：主文件 = 第一张，其余按页面顺序进 aux。"""
    artifact = _artifact(
        extra_paths=(
            DATA_DIR / "media/xiaohongshu/note/02.jpg",
            DATA_DIR / "media/xiaohongshu/note/03.jpg",
        )
    )
    main, aux, _source, _size, _dur, _rung, _err = _artifact_fields(artifact, ctx)
    assert main == "media/xiaohongshu/note/01.jpg"
    # 顺序也是契约的一部分：V1 的 `images/01.jpg…` 就是按页面顺序编号的。
    assert aux == ["media/xiaohongshu/note/02.jpg", "media/xiaohongshu/note/03.jpg"]


def test_a_plain_video_artifact_still_writes_an_empty_aux_list(ctx: Any) -> None:
    """反向：没有 extra_paths 时不许凭空多出 `[""]` 之类的东西。

    这条是给上一条兜底的 —— 只验"有图时进了 aux"的用例，实现改成"永远把主文件
    也塞进 aux"照样绿，而那会把每一条普通作品的 aux 都污染成"看起来像分片"。
    """
    _main, aux, *_rest = _artifact_fields(_artifact(), ctx)
    assert aux == []


def test_size_bytes_covers_the_extra_paths_not_only_the_main_file() -> None:
    """`size_bytes` 的口径是"主文件 + 其余"，`total_size_bytes()` 因此不再二次相加。

    写成关系而不是样本：清单里那条作品的体积**必须等于**它在磁盘上真正占的字节，
    少算一半的后果是"盘满了但清单说这轮只写了 200 KB"，那是查不下去的账。
    """
    artifact = _artifact(
        size_bytes=300_000 + 250_000 + 250_000,
        extra_paths=(
            DATA_DIR / "media/xiaohongshu/note/02.jpg",
            DATA_DIR / "media/xiaohongshu/note/03.jpg",
        ),
    )
    assert total_size_bytes(artifact) == artifact.size_bytes == 800_000


def test_an_image_note_has_no_transcribable_audio() -> None:
    """`has_audio=False` 时 `audio_path_of()` 抛而不是交出那张 jpg。

    这是 ADR-0019 整个方案赖以成立的那道闸：契约允许 `media_path` 指向一个不可播放的
    文件，所以"音频在哪"这个问题必须有一个**会拒绝回答**的入口。
    """
    with pytest.raises(ValueError, match="没有音频轨"):
        audio_path_of(_artifact())


# --------------------------------------------------------------------------- #
# 消费侧：postprocess 不许把 aux 当成"一定是音频"
# --------------------------------------------------------------------------- #


def test_audio_source_never_comes_from_the_aux_list_of_an_image_note(ctx: Any) -> None:
    """库行长得像"有音频 aux"的图文笔记，转写必须判它没有声音。

    判据是采集时落进 `metadata_json` 的 `has_audio`，不是"aux 非空"。
    变异检查：把 `_audio_source` 退回旧的"非空就当音频轨"，这条必红。
    """
    row = _row(
        media_path="media/xiaohongshu/note/01.jpg",
        media_aux_paths_json=json.dumps(
            ["media/xiaohongshu/note/02.jpg", "media/xiaohongshu/note/03.jpg"]
        ),
        metadata_json=json.dumps({"has_audio": False}),
    )
    assert post_mod._audio_source(ctx, video=row) is None


def test_a_dash_pair_still_takes_its_audio_from_the_aux_entry(ctx: Any) -> None:
    """同一条判据的另一半没被改坏：DASH 未合并的音频轨照旧来自 aux[0]。

    只验"图文不回退"的用例，实现改成"永远返回 None"也能绿 —— 那等于把所有
    B站 分片作品的口播稿都悄悄关掉，而看板上一片绿。
    """
    row = _row(
        platform="bilibili",
        media_path="media/bilibili/up/BV.f137.mp4",
        media_aux_paths_json=json.dumps(["media/bilibili/up/BV.f140.m4a"]),
        metadata_json=json.dumps({"has_audio": True}),
    )
    assert post_mod._audio_source(ctx, video=row) == DATA_DIR / "media/bilibili/up/BV.f140.m4a"


def test_a_row_whose_metadata_says_nothing_falls_back_to_the_main_file(ctx: Any) -> None:
    """没有 `has_audio` 键（早于这一版的行）= **不知道**，交回主文件让 ffprobe 去问。

    这里刻意不猜 False：猜错的后果是白丢一篇稿子，而"丢稿子"在看板上完全不可见
    （AGENTS.md §1.3 那一族）。
    """
    row = _row(media_path="media/douyin/user/7001/media.mp4", metadata_json="{}")
    assert post_mod._audio_source(ctx, video=row) == DATA_DIR / "media/douyin/user/7001/media.mp4"


def test_metadata_that_is_not_a_json_object_counts_as_saying_nothing(ctx: Any) -> None:
    """`metadata_json` 是一列自由文本：坏 JSON 与 JSON 数组都不许让整条转写红掉。

    这一列今天有两个写入方（collect 与迁移脚本），两边格式约定不同就会撞上一次。
    """
    for payload in ("not json at all", '["a", "b"]', "null", ""):
        row = _row(media_path="media/douyin/u/v/media.mp4", metadata_json=payload)
        assert post_mod._audio_source(ctx, video=row) == DATA_DIR / "media/douyin/u/v/media.mp4"
