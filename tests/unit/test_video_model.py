"""`models/video.Video` 上那一格 `has_video` —— 它决定界面挂不挂 `<video>`。

为什么单独一个文件测它：这一位不是"多一个字段好看"，是 ADR-0019 那条判据的**出口**。
产物上早就有 `has_video`（图文笔记 = False），但从来没有传到需要它的那一端，
于是 2026-09-25 收到第一条真图文笔记时，界面挂出一个永不加载的黑播放器。

三条断言各挡一种坏法：
- **回落按容器表判后缀** → 挡"本改动之前入库的行"被一律当真（黑屏回来了）。
- **metadata 里那位优先** → 挡"适配器说了不算，我又猜一次后缀"。
- **两处判据不许漂** → 与 `/api/videos/{id}/media` 的白名单逐字对齐。
  这是这条链上唯一真正的危险：模型这一侧与端点那一侧各列一份容器名单，
  加一档时改一处，就会出现"`has_video=True` 但 `/media` 403"（黑屏）
  或反过来"`False` 但端点其实出得了图"（能播的不播）。
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

import pytest

from intelligence_hub_v2.api.v1.media import MEDIA_CONTENT_TYPES
from intelligence_hub_v2.models.video import _PLAYABLE_CONTAINERS, Video

_NOW = datetime(2026, 9, 25, 12, 0, tzinfo=UTC)


def _video(**kw: object) -> Video:
    base: dict[str, object] = {
        "id": 1,
        "platform": "xiaohongshu",
        "platform_video_id": "6a910d0400000000210337a9",
        "title": "t",
        "created_at": _NOW,
        "updated_at": _NOW,
    }
    base.update(kw)
    return Video.model_validate(base)


def test_no_media_path_is_not_playable() -> None:
    """ "库里没有落地文件"当然不播 —— 而且这一位**不该由界面去猜** `media_path` 真假。"""
    assert _video().has_video is False


@pytest.mark.parametrize(
    ("path", "expected"),
    [
        ("media/x/a/images/01.jpg", False),
        ("media/x/a/images/02.png", False),
        ("media/x/a/media.mp4", True),
        ("media/x/a/media.webm", True),
        ("media/x/a/media.MP4", True),  # 大小写不敏感：与端点同一口径
        ("media/x/a/speech-clean.txt", False),
    ],
)
def test_the_fallback_judges_by_container_not_by_optimism(path: str, expected: bool) -> None:
    """metadata 里没这一位（本改动之前入库的行）→ 按后缀对容器表判。

    回落那一支不是"默认 True 省事"：那等于把图文笔记继续推回黑屏。
    """
    assert _video(media_path=path).has_video is expected


def test_the_artifact_field_wins_over_the_suffix() -> None:
    """适配器交了 `has_video` 就以它为准，后缀不许翻案。

    刻意造一个"后缀像 mp4 但字段说 False"的组合：真出现这种行，说明产物与入库
    两处对同一条作品的判断不一致 —— 那时**听产物的**（它是下载那一步的事实），
    而不是让读的一侧按文件名重新发明一次。
    """
    video = _video(
        media_path="media/x/a/media.mp4",
        metadata_json=json.dumps({"has_video": False}),
    )
    assert video.has_video is False


def test_malformed_metadata_json_degrades_to_the_suffix_rule() -> None:
    """脏 metadata 不该让 `GET /api/videos` 整页 500。

    这一列是外部输入与历史代码共同写出来的，坏 JSON 是可能发生的；
    发生时的正确行为是退到后缀那条判据，而不是抛。
    """
    assert _video(media_path="m.mp4", metadata_json="{ 不是 JSON").has_video is True
    assert _video(media_path="m.jpg", metadata_json="[1,2]").has_video is False
    assert _video(media_path="m.jpg", metadata_json="").has_video is False


def test_has_video_is_exposed_on_the_api_shape() -> None:
    """这一位必须出现在响应里 —— 前端拿不到它就等于没有这一位。

    这条是那个 bug 的直接看护：ADR-0019 写了"播放器先问这一位"，
    但字段只活在产物上，`Video` 模型不暴露，前端**无从问起**。
    """
    schema = Video.model_json_schema()
    assert "has_video" in schema["properties"], sorted(schema["properties"])[:6]
    assert "has_video" in _video(media_path="m.mp4").model_dump()


def test_has_video_fallback_suffixes_match_the_media_endpoint_whitelist() -> None:
    """模型那侧的容器表必须与 `/media` 端点的白名单**逐字相等**。

    两处各列一份就是"同一个判据两个真源"，而它俩必须永远同答案：
    模型说可播而端点 403 = 黑屏；模型说不播而端点出得了图 = 能播的没播。
    """
    # 就是要盯这份私有常量：它不该被业务代码用，只该被这条对齐看护用
    assert frozenset(MEDIA_CONTENT_TYPES) == _PLAYABLE_CONTAINERS, (
        f"模型侧多了：{set(_PLAYABLE_CONTAINERS) - set(MEDIA_CONTENT_TYPES)}；"
        f"端点侧多了：{set(MEDIA_CONTENT_TYPES) - _PLAYABLE_CONTAINERS}"
    )
