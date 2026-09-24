"""/api/export：CSV 与 JSON 两条出口（计划 T6.4）。

这一文件里最值钱的三条，全是"文件里看不出来的错"：

1. **公式注入**：标题是外部输入，`=cmd|...` 原样导出后在**别人的 Excel** 里执行。
   页面上一条 XSS 关掉标签就没了，一份发出去的 CSV 追不回来。
2. **翻页翻到底**：列表端点的 `size<=200` 是列表页要的，导出要的是全表。
   少翻一页得到的是一份**看起来完整**的 CSV —— 行数对不上这件事在文件里看不出来。
3. **BOM**：没有它，中文 Windows 的 Excel 按 GBK 猜编码，整篇乱码，
   而用户报上来的症状是"数据丢了"。
"""

from __future__ import annotations

import csv
import io
import json

import httpx
import pytest

from intelligence_hub_v2.api.deps import AppState
from intelligence_hub_v2.api.v1.export import _COLUMNS
from intelligence_hub_v2.models.creator import Creator, CreatorDraft
from intelligence_hub_v2.models.video import Video, VideoDraft

pytestmark = pytest.mark.integration

_BOM = b"\xef\xbb\xbf"


async def _seed(state: AppState, *, count: int = 2, titles: tuple[str, ...] | None = None) -> None:
    creator = await state.storage.creators.insert(
        CreatorDraft(
            platform="douyin",
            platform_id="sec1",
            name="姜胡说",
            profile_url="douyin://sec1",
            follower_count=12345,
        )
    )
    for index in range(count):
        await state.storage.videos.insert(
            VideoDraft(
                platform="douyin",
                platform_video_id=f"aweme-{index}",
                creator_id=creator.id,
                title=(titles[index] if titles else f"作品 {index}"),
            )
        )


def _csv_rows(payload: bytes) -> list[list[str]]:
    """按**读的人的方式**解一遍：剥 BOM、交给 `csv` 模块（它认引号与转义）。

    不用 `splitlines()`：标题里带换行时那种切法会把一行切成两行，
    然后这条看护自己给出假红。
    """
    text = payload.decode("utf-8-sig")
    return list(csv.reader(io.StringIO(text)))


async def test_csv_export_has_bom_header_and_every_row(
    client: httpx.AsyncClient, app_state: AppState
) -> None:
    await _seed(app_state, count=3)
    resp = await client.get("/api/export", params={"entity": "videos"})
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("text/csv")
    raw = resp.content
    assert raw.startswith(_BOM), "缺 BOM：中文 Windows 的 Excel 会把 UTF-8 当 GBK 猜，整篇乱码"
    rows = _csv_rows(raw)
    assert rows[0][0] == "id" and "title" in rows[0]
    assert len(rows) == 4, "表头 + 3 行"
    # CRLF（RFC 4180）：Excel 对裸 LF 的多行单元格显示不稳定
    assert b"\r\n" in raw


async def test_json_export_uses_the_column_list_and_keeps_chinese_readable(
    client: httpx.AsyncClient, app_state: AppState
) -> None:
    await _seed(app_state, count=1, titles=("怎么选题，90% 的人都搞反了",))
    resp = await client.get("/api/export", params={"entity": "videos", "format": "json"})
    assert resp.status_code == 200
    assert resp.headers["content-type"].startswith("application/json")
    body = resp.content.decode("utf-8")
    assert "怎么选题" in body, "ensure_ascii=False 是判据：导出不是给机器看的"
    rows = json.loads(body)
    assert rows[0]["title"] == "怎么选题，90% 的人都搞反了"
    assert "metadata_json" not in rows[0], "整段 V1 raw_data 不该出现在导出里"


@pytest.mark.parametrize(
    ("title", "expected"),
    [
        ("=cmd|' /C calc'!A0", "'=cmd|' /C calc'!A0"),
        ("@SUM(A1:A9)", "'@SUM(A1:A9)"),
        ("+1+1", "'+1+1"),
        ("-1+1", "'-1+1"),
        ('   =HYPERLINK("http://evil")', '\'   =HYPERLINK("http://evil")'),
        ("正常标题", "正常标题"),
    ],
)
async def test_a_hostile_title_is_neutralised_in_csv(
    client: httpx.AsyncClient, app_state: AppState, title: str, expected: str
) -> None:
    await _seed(app_state, count=1, titles=(title,))
    raw = (await client.get("/api/export", params={"entity": "videos"})).content
    row = _csv_rows(raw)[1]
    assert row[3] == expected, "公式起手没被中和（或把正常数据改坏了）"


async def test_a_negative_number_is_not_quoted_away_as_text(
    client: httpx.AsyncClient, app_state: AppState
) -> None:
    """负数是**数字**，加 `'` 前缀会把它变成文本、Excel 一列求和就废了。

    判据是"只中和字符串"，这条用例防止下一个人在 `_csv_cell` 里把判据放宽到所有值。
    取值故意用 `-5`：它既是合法负数，又落在起手字符表里（`-1+1` 那类公式的开头），
    所以"放宽判据"与"判据太紧"两种改法都会在这里红。
    """
    await _seed(app_state, count=1, titles=("正常标题",))
    rows = (await client.get("/api/videos", params={"size": 50})).json()["items"]
    await app_state.storage.videos.update_fields(rows[0]["id"], view_count=-5)

    parsed = _csv_rows((await client.get("/api/export")).content)
    header, row = parsed[0], parsed[1]
    assert row[header.index("view_count")] == "-5", "数字被当字符串中和了"
    assert row[header.index("title")] == "正常标题", "正常文本不该被改坏"


async def test_export_pages_past_the_list_endpoint_size_cap(
    client: httpx.AsyncClient, app_state: AppState
) -> None:
    """**这一条是"少翻页"唯一的哨兵**：250 行 > 列表端点的 200 行上限。

    只看前两页的实现也能让上面所有用例绿 —— 缺失的行在文件里完全看不出来。
    """
    await _seed(app_state, count=250)
    raw = (await client.get("/api/export", params={"entity": "videos"})).content
    assert len(_csv_rows(raw)) == 251, "导出被列表端的 size 上限截断了"

    rows = json.loads((await client.get("/api/export", params={"format": "json"})).content)
    assert len(rows) == 250, "JSON 那条走的是另一个出口，也要翻到底"


async def test_hidden_rows_follow_the_same_default_as_the_list(
    client: httpx.AsyncClient, app_state: AppState
) -> None:
    await _seed(app_state, count=2)
    listed = (await client.get("/api/videos", params={"size": 50})).json()["items"]
    await client.patch(f"/api/videos/{listed[0]['id']}/hide", json={"reason": "重复"})

    default = _csv_rows((await client.get("/api/export")).content)
    assert len(default) == 2, "默认与 /api/videos 一样只给未隐藏的（表头 + 剩下那一条）"

    everything = _csv_rows((await client.get("/api/export", params={"hidden": "all"})).content)
    assert len(everything) == 3
    only_hidden = _csv_rows((await client.get("/api/export", params={"hidden": "hidden"})).content)
    assert len(only_hidden) == 2


@pytest.mark.parametrize("params", [{"entity": "cookies"}, {"format": "xlsx"}, {"hidden": "yes"}])
async def test_unknown_enums_are_a_422_not_a_fallback(
    client: httpx.AsyncClient, app_state: AppState, params: dict[str, str]
) -> None:
    """猜一个默认值 = 给用户一份他以为要的不是他拿到的东西。"""
    resp = await client.get("/api/export", params=params)
    assert resp.status_code == 422


async def test_the_filename_carries_no_request_input(
    client: httpx.AsyncClient, app_state: AppState
) -> None:
    """文件名要进 `Content-Disposition` 响应头：一个带 CRLF 的参数就能做 header injection。

    所以 platform 这类外部输入**一个字都不进文件名**，这里用带引号与空格的取值去试。
    """
    await _seed(app_state, count=1)
    resp = await client.get("/api/export", params={"platform": 'x"; filename="pwned'})
    assert resp.status_code == 200
    disposition = resp.headers["content-disposition"]
    assert disposition.startswith('attachment; filename="intelligence-hub-videos-')
    assert disposition.endswith('.csv"')
    assert "\r" not in disposition and "\n" not in disposition
    assert "pwned" not in disposition


async def test_creators_export_is_a_different_column_set(
    client: httpx.AsyncClient, app_state: AppState
) -> None:
    await _seed(app_state, count=1)
    rows = json.loads(
        (await client.get("/api/export", params={"entity": "creators", "format": "json"})).content
    )
    assert rows[0]["name"] == "姜胡说" and rows[0]["follower_count"] == 12345
    assert rows[0]["is_tracking"] == "true", "布尔统一成小写 true/false（CSV 里 True 会被读坏）"
    assert "metadata_json" not in rows[0]


def test_the_two_column_lists_are_declared_not_inferred() -> None:
    """列清单是手写的（`_COLUMNS`）：新增模型字段不该自动进导出。

    这条断言把"为什么手写"钉住 —— 一旦有人改成 `model_dump().keys()`，
    `metadata_json`（几十 KB 一行）与以后任何内部字段都会静默出现在别人的表格里。
    """

    assert set(_COLUMNS) == {"videos", "creators"}
    # 每一列都真存在于模型上：写错一个名字不会报错，只会导出一列空值（最难查的那种）
    assert set(_COLUMNS["videos"]) <= set(Video.model_fields), "videos 列清单里有模型没写的字段"
    assert set(_COLUMNS["creators"]) <= set(Creator.model_fields), "creators 同上"
    # 而反过来不收的那几列是**决定**，不是遗漏
    assert "metadata_json" not in _COLUMNS["videos"] and "metadata_json" not in _COLUMNS["creators"]
    assert "avatar_url" not in _COLUMNS["creators"], "签名 URL 是会过期的外部地址，别导出去当身份"
