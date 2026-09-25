"""小红书 `urls.py` 的用例：身份、链接、发布时间。**全部离线**（这一层不许碰网络）。

看护的形状与抖音/B站 那两份 `urls.py` 一致：先按"真实来路"逐条钉，再钉两条
**关系**而不是一次性样本 ——

- `build_profile_url(uid)` 里必须含得上 `uid`（V1 §7.1 那条"同一个人被收录两次"的根因）；
- `parse_publish_time` 交出的东西**要么是 None，要么是 aware datetime**（V2 的
  `videos.published_at` 是 `UTCDateTime`，naive 值会在算时长时炸，而且炸点离原因很远）。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

import pytest

from intelligence_hub_v2.platforms.xiaohongshu.urls import (
    build_note_url,
    build_profile_url,
    extract_note_id,
    extract_user_id,
    note_id_from_timestamp,
    note_token_of,
    parse_publish_time,
    published_at_from_note_id,
)

# 一条真形状的笔记 ID：前 8 位是十六进制的发布时间戳。
NOTE_ID = "64a3b2c10000000012034567"
USER_ID = "5fb5c2f00000000001005b23"


# --------------------------------------------------------------------------- #
# note_id
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "url",
    [
        f"https://www.xiaohongshu.com/explore/{NOTE_ID}",
        f"https://www.xiaohongshu.com/item/{NOTE_ID}",
        f"https://www.xiaohongshu.com/discovery/item/{NOTE_ID}",
        f"https://www.xiaohongshu.com/search_result/{NOTE_ID}",
        f"https://www.xiaohongshu.com/explore/{NOTE_ID}?xsec_token=ABc%3D&xsec_source=pc_search",
    ],
)
def test_note_id_from_the_real_landing_shapes(url: str) -> None:
    assert extract_note_id(url) == NOTE_ID


def test_a_share_short_link_is_not_resolved_here() -> None:
    """`xhslink.com/a/xxxx` 那类短码**不含身份信息**，要拿它得先跟一次 302。

    那是适配器 `parse_creator_url()` / 详情那一步的事（那里能注入 `deps.http`，
    测试才能用 `MockTransport` 验"真的跟了一次跳转"）。这一层判空，
    而不是顺手猜一个 ID 出来 —— 短码背后是什么，站外谁都不知道。
    """
    assert extract_note_id("https://xhslink.com/a/3AbCdEf") == ""
    assert extract_user_id("https://xhslink.com/a/3AbCdEf") == ""


def test_a_bare_note_id_survives_and_a_share_blurb_too() -> None:
    """用户从表格里复制的常常是裸 ID；分享文案则在 URL 前面带一串中文。"""
    assert extract_note_id(NOTE_ID) == NOTE_ID
    noisy = f"7.24 复制打开小红书，看看【某人的作品】https://www.xiaohongshu.com/explore/{NOTE_ID}"
    assert extract_note_id(noisy) == NOTE_ID


def test_a_profile_url_is_never_read_as_a_note_id() -> None:
    """主页链接里那段数字是**人**的身份。

    覆盖路径段那条兜底：`/user/profile/<uid>/` 里 uid 也是 `[0-9a-zA-Z]{8,}` 的形状，
    认错就把博主当作品入库（V1 那条正则专门吃掉了这一段，照搬）。
    """
    assert extract_note_id(build_profile_url(USER_ID)) == ""
    assert extract_note_id(f"https://www.xiaohongshu.com/user/profile/{USER_ID}/col") == ""


@pytest.mark.parametrize("value", ["", None, "   ", "不是链接", "64a3", "xiaohongshu.com/explore/"])
def test_unrecognisable_values_are_empty_not_guessed(value: Any) -> None:
    assert extract_note_id(value) == ""


# --------------------------------------------------------------------------- #
# user_id
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "url",
    [
        f"https://www.xiaohongshu.com/user/profile/{USER_ID}",
        f"https://www.xiaohongshu.com/user/profile/{USER_ID}?xsec_token=ABc%3D",
        f"https://m.xiaohongshu.com/user/profile/{USER_ID}",
    ],
)
def test_user_id_from_a_profile_url(url: str) -> None:
    assert extract_user_id(url) == USER_ID


def test_a_bare_user_id_is_recognised_and_a_note_url_is_not() -> None:
    assert extract_user_id(USER_ID) == USER_ID
    # 作品链接里没有博主身份 —— 要拿它得调一次详情接口，那不在纯解析层
    assert extract_user_id(f"https://www.xiaohongshu.com/explore/{NOTE_ID}") == ""


# --------------------------------------------------------------------------- #
# 链接拼装
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("uid", [USER_ID, "5fb5", "abc123"])
def test_the_canonical_profile_url_always_carries_the_uid(uid: str) -> None:
    """**关系而不是样本**：库里存的链接含不上 `platform_id`，下一轮就对不上号（V1 §7.1）。"""
    assert uid in build_profile_url(uid)


def test_empty_uid_gives_empty_url_rather_than_a_dangling_prefix() -> None:
    assert build_profile_url("") == ""


def test_the_xsec_token_is_percent_encoded_into_the_url() -> None:
    """token 里有 `=` `/` `+`，不转义会把 query 拼断（V1 用 `quote(safe="")`）。"""
    url = build_note_url(NOTE_ID, "AbC=d/e+F==")
    assert f"/explore/{NOTE_ID}?" in url
    assert "xsec_token=AbC%3Dd%2Fe%2BF%3D%3D" in url
    assert "xsec_source=pc_user" in url


def test_no_token_means_no_query_string_at_all() -> None:
    """缺 token 时站内会跳风控页 —— 但那是"取不到"，不是"拼错"，
    所以这里**不**发明一个假 token，只交回干净的 explore 链接。"""
    assert build_note_url(NOTE_ID) == f"https://www.xiaohongshu.com/explore/{NOTE_ID}"


# --------------------------------------------------------------------------- #
# 发布时间
# --------------------------------------------------------------------------- #


def test_the_note_id_prefix_is_an_epoch_second() -> None:
    """非循环的判据：交回的时间换算回 epoch 秒正好是那 8 位十六进制。"""
    stamp = note_id_from_timestamp(NOTE_ID)
    assert stamp == 0x64A3B2C1
    assert published_at_from_note_id(NOTE_ID) is not None
    assert published_at_from_note_id(NOTE_ID).timestamp() == stamp  # type: ignore[union-attr]


@pytest.mark.parametrize("note_id", ["zzzzzzzz0000000000000000", "", "00000000abc", None])
def test_a_note_id_that_is_not_a_timestamp_yields_none(note_id: Any) -> None:
    assert published_at_from_note_id(note_id) is None


def test_a_stamp_in_the_next_century_is_not_a_publish_time() -> None:
    """没有这道闸，一条 ID 恰好以 `ffffffff` 开头的笔记会安静地排到 Feed 最前面。"""
    assert note_id_from_timestamp("ffffffff0000000000000000") is None


def test_the_low_end_of_the_window_is_deliberately_not_bounded() -> None:
    """`00000001…` 这种 ID 会被读成 1970-01-01 那一秒。**不发明下界**。

    小红书 2013 年才有内容，看着像可以判掉；但"下界取哪一年"是我们的猜测，
    而猜错的后果是一条真笔记的发布时间被判成"没有"。V1 也只用 `stamp <= 0` 这一条。
    """
    assert note_id_from_timestamp("00000001abc0000000000000") == 1


@pytest.mark.parametrize(
    ("value", "expected_epoch"),
    [
        (1719000000, 1719000000),  # 秒（int）
        ("1719000000", 1719000000),  # 秒（纯数字串）
        (1719000000000, 1719000000),  # 毫秒 → 必须收敛到同一个时刻
        ("1719000000000", 1719000000),
    ],
)
def test_epoch_shapes_all_land_on_the_same_instant(value: Any, expected_epoch: int) -> None:
    parsed = parse_publish_time(value)
    assert parsed is not None and parsed.timestamp() == float(expected_epoch)


@pytest.mark.parametrize(
    "text",
    [
        "2026-01-02 03:04:05",
        "2026-01-02 03:04",
        "2026/01/02",
        "2026-01-02T03:04:05",
        "发布于 2026-01-02 03:04:05",  # 混在句子里：search 而不是 fullmatch
    ],
)
def test_the_human_date_shapes_land_on_local_wall_clock(text: str) -> None:
    parsed = parse_publish_time(text)
    assert parsed is not None
    assert (parsed.year, parsed.month, parsed.day) == (2026, 1, 2)
    assert parsed.strftime("%H:%M") == "03:04" if "03:04" in text else parsed.hour == 0


@pytest.mark.parametrize("value", [None, "", "   ", True, False, "昨天", "刚刚", 0, -1])
def test_unreadable_times_are_none_not_a_fabricated_moment(value: Any) -> None:
    """None 而不是 0 / 今天：存 0 会让"没解析出来"与"1970 年发布"在看板上长得一样。"""
    assert parse_publish_time(value) is None


@pytest.mark.parametrize(
    "value",
    [
        NOTE_ID,
        1719000000,
        "1719000000000",
        "2026-01-02",
        "2026-01-02T03:04:05",
        "发布于 2026-01-02 03:04:05",
        datetime(2026, 1, 2, 3, 4, 5),  # noqa: DTZ001 - 就是要喂一个 naive 进去
    ],
)
def test_every_understood_value_is_aware(value: Any) -> None:
    """V2 这一层的契约：**要么 None，要么带时区**。

    naive datetime 进 `UTCDateTime` 列不会当场报错，报错点在很久之后那句
    `datetime.now(UTC) - row.published_at`（"can't subtract offset-naive and offset-aware"）。
    """
    parsed = parse_publish_time(value)
    assert parsed is None or (parsed.tzinfo is not None and parsed.utcoffset() is not None)


# --------------------------------------------------------------------------- #
# `xsec_token` 的反方向（T3.3 真机炸出来的那一手）
# --------------------------------------------------------------------------- #

#: 真链接里的 token 长这样（尾部带 `=`，中间可能带 `-` `_`）。
_REAL_TOKEN = "ABOgq79sds9_-oBYa6uGqmDJ6KcKC_sWjr37ssyquu0iw="


def test_the_token_survives_a_build_then_extract_round_trip() -> None:
    """`build_note_url` 拼出去、`note_token_of` 取回来，必须**一字不差**。

    写成往返而不是"等于某个样本串"：这两只手一个是编码、一个是解码，
    任何一边改了（比如把 `safe=""` 换成 `safe="/"`）都会先把往返打断，
    而不需要我来记住"编码之后应该长什么样"。
    token 尾部的 `=` 是这一条的重点：它是 query 值的一部分，
    解码不回来 / 编码编两次，站内都认不出 —— 而认不出的症状是跳风控页，
    看起来像"这条笔记被删了"（2026-09-25 真机踩过的是它的另一面：整条链路丢 token）。
    """
    url = build_note_url(NOTE_ID, _REAL_TOKEN, source="pc_feed")
    token, source = note_token_of(url)
    assert token == _REAL_TOKEN, f"往返之后 token 变了：{token!r}"
    assert source == "pc_feed"


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        (f"https://www.xiaohongshu.com/explore/{NOTE_ID}", ""),
        (f"https://www.xiaohongshu.com/explore/{NOTE_ID}?xsec_token=", ""),
        (f"https://www.xiaohongshu.com/explore/{NOTE_ID}?from=x", ""),
        ("", ""),
    ],
)
def test_absent_token_is_an_empty_string_not_a_crash(url: str, expected: str) -> None:
    """ "链接里没有 token"要给出空串，让调用方去决定怎么说。

    这一手不抛、也不编一个假 token：调用方（`single_link`）要能分清
    "用户粘的是裸链接"（该提示他粘完整链接）与"我解析失败"。
    """
    assert note_token_of(url) == (expected, "")
