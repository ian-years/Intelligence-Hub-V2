"""`platforms/youtube/urls.py` 的纯解析用例（离线，不发任何请求）。

信条照旧：**断言写成关系**。这里的关系有三条：

1. "同一个频道的三种写法必须收成同一个 `UC…`" —— 不然库里会出现三条博主行。
2. "认不出必须返回空串，不许猜一个" —— 猜出来的身份会入库，比失败更难清理。
3. `enumeration_url()` 的输出必须真的**能再被本模块认出来**（自反），
   且不重复追加 `/videos`（那是"枚举恒空而 yt-dlp 报 404"的形状）。
"""

from __future__ import annotations

import pytest

from intelligence_hub_v2.errors import PlatformError
from intelligence_hub_v2.platforms.youtube import urls

CHANNEL_ID = "UCKC8SZ0cdXPnnkKkQQLJmsQ"

# 同一个频道的真实可粘贴写法。V1 的 `channel_videos_url` 要收的就是这一族。
SAME_CHANNEL_FORMS = (
    f"https://www.youtube.com/channel/{CHANNEL_ID}",
    f"https://www.youtube.com/channel/{CHANNEL_ID}/featured",
    f"https://m.youtube.com/channel/{CHANNEL_ID}?si=AbCdEf",
    f"https://www.youtube-nocookie.com/channel/{CHANNEL_ID}/videos",
    CHANNEL_ID,
    f"  {CHANNEL_ID}  ",
    f"这条频道 https://www.youtube.com/channel/{CHANNEL_ID} 值得关注",
)


@pytest.mark.parametrize("form", SAME_CHANNEL_FORMS)
def test_every_spelling_of_one_channel_yields_the_same_identity(form: str) -> None:
    assert urls.extract_channel_id(form) == CHANNEL_ID


def test_the_identity_is_not_a_url_and_survives_a_missing_trailing_slash() -> None:
    """防空转的前置：上面那 7 条如果全都只靠"整段等于自己"过，
    那 `search` 那一步根本没被走过 —— 这里挑两条**必须**走 search 的。"""
    assert urls.extract_channel_id(f"/channel/{CHANNEL_ID}/videos") == CHANNEL_ID
    assert urls.extract_channel_id(f"youtube.com/watch?v=dQw4w9WgXcQ&channel={CHANNEL_ID}") == (
        CHANNEL_ID
    )


@pytest.mark.parametrize(
    "text",
    [
        "",
        "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
        "@somehandle",
        "UC短",  # 不足 `{6,}`，认不出
        "https://example.com/channel/abc",
    ],
)
def test_no_channel_identity_means_empty_string_not_a_guess(text: str) -> None:
    assert urls.extract_channel_id(text) == ""


def test_channel_id_regex_is_deliberately_loose_and_that_is_pinned() -> None:
    """`{6,}` 而不是定长 22（V1 同一条）。

    这条用例钉的是**取舍本身**：改成 `{22}` 会让短一点的合法串认不出来，
    而那是一次真实行为变更，必须有人在这里改注释才知道要去看 ADR-0016 那条
    "判紧的后果"。
    """
    short = "UC" + "a" * 6
    assert urls.extract_channel_id(f"/channel/{short}") == short
    assert urls.extract_channel_id("UC" + "a" * 5) == ""


def test_is_channel_id_is_the_strict_sister_of_extract() -> None:
    """`is_channel_id` 是整串判定（`fullmatch`），`extract` 是扫描。
    两者混用会让"这条 platform_id 合法吗"与"这段文本里有没有身份"变成一件事。"""
    assert urls.is_channel_id(CHANNEL_ID) is True
    assert urls.is_channel_id(f"https://www.youtube.com/channel/{CHANNEL_ID}") is False


HANDLE_FORMS = {
    "https://www.youtube.com/@RickAstleyYT": "RickAstleyYT",
    "@RickAstleyYT": "RickAstleyYT",
    "https://www.youtube.com/@rick.astley_-videos/videos": "rick.astley_-videos",
    "https://www.youtube.com/@RickAstleyYT?si=x": "RickAstleyYT",
}


@pytest.mark.parametrize(("text", "expected"), HANDLE_FORMS.items())
def test_handles_are_recognized_without_the_at_sign(text: str, expected: str) -> None:
    assert urls.extract_handle(text) == expected


def test_handle_extraction_does_not_invent_one_from_a_channel_url() -> None:
    """`/channel/UC…` 里**没有**手柄。认出来就是把两种词汇混成一格。"""
    assert urls.extract_handle(f"https://www.youtube.com/channel/{CHANNEL_ID}") == ""


VIDEO_URLS = {
    "https://www.youtube.com/watch?v=dQw4w9WgXcQ": "dQw4w9WgXcQ",
    "https://www.youtube.com/watch?v=dQw4w9WgXcQ&list=PLabc": "dQw4w9WgXcQ",
    "https://youtu.be/dQw4w9WgXcQ?t=42": "dQw4w9WgXcQ",
    "https://www.youtube.com/shorts/dQw4w9WgXcQ": "dQw4w9WgXcQ",
    "https://www.youtube.com/live/dQw4w9WgXcQ": "dQw4w9WgXcQ",
    "https://www.youtube.com/embed/dQw4w9WgXcQ": "dQw4w9WgXcQ",
}


@pytest.mark.parametrize(("url", "expected"), VIDEO_URLS.items())
def test_video_ids_are_extracted_from_every_landing_page_shape(url: str, expected: str) -> None:
    assert urls.extract_video_id(url) == expected


@pytest.mark.parametrize(
    "text",
    [
        "",
        "dQw4w9WgXcQ",  # 裸 ID **故意不认**（见 docstring：11 位短码太容易撞）
        "https://www.youtube.com/watch?v=tooShort",
        "https://www.youtube.com/watch?v=",
        "https://www.youtube.com/@handle/videos",
    ],
)
def test_an_ambiguous_or_truncated_video_reference_is_not_guessed(text: str) -> None:
    assert urls.extract_video_id(text) == ""


def test_video_id_shape_is_exactly_eleven_and_the_negative_case_proves_it() -> None:
    assert urls.is_video_id("dQw4w9WgXcQ") is True
    assert urls.is_video_id("dQw4w9WgXc") is False
    assert urls.is_video_id("dQw4w9WgXcQ1") is False
    assert urls.is_video_id("dQw4w9WgX Q") is False


def test_urls_round_trip_through_their_own_extractors() -> None:
    """**自反关系**：本模块拼出来的地址必须能被本模块认回同一个身份。
    不这样就会"存进去的地址下一轮解析不出来"，而那是两个模块之间的暗坑。"""
    channel_page = urls.channel_url(CHANNEL_ID)
    assert urls.extract_channel_id(channel_page) == CHANNEL_ID
    video_page = urls.canonical_video_url("dQw4w9WgXcQ")
    assert urls.extract_video_id(video_page) == "dQw4w9WgXcQ"
    assert urls.extract_channel_id(video_page) == ""


def test_enumeration_url_points_at_the_videos_tab_for_every_identity_form() -> None:
    """三条来路各一：UC / `@手柄` / 老式 `/c/<name>`。全部以 `/videos` 结尾。"""
    assert (
        urls.enumeration_url(CHANNEL_ID) == f"https://www.youtube.com/channel/{CHANNEL_ID}/videos"
    )
    assert urls.enumeration_url("@RickAstleyYT") == "https://www.youtube.com/@RickAstleyYT/videos"
    assert urls.enumeration_url("c/TEDxTalks") == "https://www.youtube.com/c/TEDxTalks/videos"


def test_enumeration_url_alands_on_the_videos_tab_however_the_page_is_written() -> None:
    """两条判据合起来才是这一格真正要的东西：

    1. **不许追加出第二个 `/videos`** —— `/videos/videos` 是 404，
       而症状是"这位博主一条作品都没有"（`entry` 抽不出、退出码照样 0）。
    2. **标签页由我们定，不由用户粘的那一条定** —— 同一位频道被两个人分别用
       `/shorts` 与 `/videos` 链接加进来，必须走同一个枚举地址，
       否则"为什么这两位收出来的作品不一样"没有答案。
    """
    assert urls.enumeration_url(urls.channel_videos_url(CHANNEL_ID)) == urls.channel_videos_url(
        CHANNEL_ID
    )
    for tab in ("videos", "shorts", "live", "playlists"):
        assert urls.enumeration_url(f"https://www.youtube.com/channel/{CHANNEL_ID}/{tab}") == (
            urls.channel_videos_url(CHANNEL_ID)
        )
        assert urls.enumeration_url(f"https://www.youtube.com/@RickAstleyYT/{tab}") == (
            "https://www.youtube.com/@RickAstleyYT/videos"
        )
    # 认不出身份的完整 URL 走第三条分支：补一次 /videos，且已经在 tab 上就原样返回
    assert urls.enumeration_url("https://www.youtube.com/partners/x") == (
        "https://www.youtube.com/partners/x/videos"
    )
    assert urls.enumeration_url("https://www.youtube.com/partners/x/videos") == (
        "https://www.youtube.com/partners/x/videos"
    )


def test_enumeration_url_upgrades_a_bare_channel_page_once() -> None:
    got = urls.enumeration_url(f"https://www.youtube.com/channel/{CHANNEL_ID}")
    assert got == urls.channel_videos_url(CHANNEL_ID)
    # 再喂一次仍然稳定（幂等）
    assert urls.enumeration_url(got) == got


def test_enumeration_url_prefers_the_id_over_the_homepage() -> None:
    """库里 `platform_id` 与 `homepage_url` 同时存在时以身份为准：
    主页可能是被改过的手柄页，而 UC 不会变。"""
    got = urls.enumeration_url(CHANNEL_ID, homepage_url="https://www.youtube.com/@whatever")
    assert got == urls.channel_videos_url(CHANNEL_ID)


def test_a_video_link_enumerates_that_video_not_a_made_up_channel_page() -> None:
    """用户从"这个频道发过这样一条"进来时粘的就是作品链接。

    去补 `/videos` 会得到 `youtube.com/watch/videos` 这种不存在的地址，
    症状是"身份解析失败"而报错里全是 yt-dlp 的 404 原文。
    """
    for text in (
        "https://www.youtube.com/watch?v=dQw4w9WgXcQ",
        "https://youtu.be/dQw4w9WgXcQ",
        "https://www.youtube.com/shorts/dQw4w9WgXcQ",
    ):
        assert urls.enumeration_url(text) == "https://www.youtube.com/watch?v=dQw4w9WgXcQ"


def test_enumeration_url_refuses_an_identity_that_is_nothing_at_all() -> None:
    with pytest.raises(PlatformError, match="拼不出可枚举的地址"):
        urls.enumeration_url("")


def test_enumeration_url_refuses_a_non_url_instead_of_handing_it_to_ytdlp() -> None:
    """把一句中文原样交给 yt-dlp，报出来的是"无效 URL"，读的人不知道是哪一步。"""
    with pytest.raises(PlatformError):
        urls.enumeration_url("我的频道")


def test_require_model_url_and_as_http_url_split_on_none() -> None:
    """可空字段用 `as_http_url`（None），必填字段用 `require_model_url`（抛）。
    两者必须**只在一处**分岔：`as_http_url` 永不抛。"""
    assert urls.as_http_url("//i.ytimg.com/vi/x/hq.jpg") is not None  # 协议相对会补 https
    assert urls.as_http_url("不是地址") is None
    with pytest.raises(PlatformError, match="应该是一个 http"):
        urls.require_model_url("不是地址", context="频道主页")


def test_url_helpers_never_touch_the_network() -> None:
    """**防空转**：整模块没有任何 http/socket/subprocess 名字。
    这一格的价值在于"哪天有人在这里塞一次请求，本模块就不再是可离线测的纯函数层"
    （B站 那份 `urls.py` 的 docstring 写的是同一条纪律）。"""
    smuggled = [
        name
        for name in ("httpx", "requests", "subprocess", "socket", "AsyncClient")
        if name in vars(urls)
    ]
    assert not smuggled, f"纯解析层里出现了会发请求的名字：{smuggled}"
