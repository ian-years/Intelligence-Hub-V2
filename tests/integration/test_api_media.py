"""`/api/videos/{id}/media`：本仓库第一个把磁盘字节交给 HTTP 的端点（T6.5 / ADR-0023）。

三条判据写成**关系**而不是样本，因为它们各自挡一类不同的坏：

- **切回去等于整文件**：`bytes=0-k`、`bytes=k-2k` … 一路拼起来必须逐字节等于原文件。
  只断"`bytes=0-3` 给前四个字节"的话，off-by-one 在别的偏移上仍然能活 ——
  而拖进度条恰恰是"任意偏移"。
- **越界与白名单**：先在 `data/cookies/` 里埋一份可识别的假凭证，再把库里的 `media_path`
  改成指向它。断的是"响应里没有那串字节"，不是"状态码是 403" —— 只断状态码的话，
  一个把内容塞进 detail 的实现也算绿。
- **每种失败都有原文**：0 字节 / 文件不在 / 没有媒体，三种红法在界面上长得一样
  （播放器空白），后端必须给得出区别。
"""

from __future__ import annotations

import httpx
import pytest

from intelligence_hub_v2.api.deps import AppState
from intelligence_hub_v2.models.video import VideoDraft

pytestmark = pytest.mark.integration

#: 一份"能验偏移算术"的内容：每个字节都可预测，切片拼回去不等就是真错了。
BODY: bytes = bytes(range(256)) * 40  # 10240 字节

#: 逐字节切的那一条用短文件：一条 HTTP 请求约 12 ms，10 KB 切 1 字节会打一万次请求
#: （实测 133 秒），而 off-by-one 在 256 字节上同样藏不住。
SHORT_BODY: bytes = bytes(range(256))


async def _video(app_state: AppState, *, media_path: str | None, title: str = "t") -> int:
    row = await app_state.storage.videos.insert(
        VideoDraft(
            platform="bilibili",
            platform_video_id=f"BV{title}",
            title=title,
            media_path=media_path,
        )
    )
    return row.id


async def _media_file(app_state: AppState, name: str = "media.mp4", body: bytes = BODY) -> str:
    """在媒体树下放一份真文件，返回**相对 data/** 那一列要写的内容。"""
    path = app_state.files.media_root / "bilibili" / "某UP" / "BV1x-media" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(body)
    return app_state.files.rel(path)


# --------------------------------------------------------------------------- #
# 正常出图
# --------------------------------------------------------------------------- #


async def test_a_media_file_comes_out_whole_and_says_so(
    client: httpx.AsyncClient, app_state: AppState
) -> None:
    """没有 Range 就是整段：字节全等 + `Accept-Ranges` 在场 + 长度自洽。

    `Content-Length` 与实际字节数必须相等 —— 播放器信的是头，不是体。
    """
    rel = await _media_file(app_state)
    video_id = await _video(app_state, media_path=rel)

    resp = await client.get(f"/api/videos/{video_id}/media")
    assert resp.status_code == 200
    assert resp.content == BODY
    assert resp.headers["accept-ranges"] == "bytes"
    assert resp.headers["content-length"] == str(len(BODY))
    assert resp.headers["content-type"] == "video/mp4"
    assert "content-range" not in resp.headers, "整段响应带 Content-Range 是协议错"


@pytest.mark.parametrize("piece_size", [1, 7, 64, 255])
async def test_slicing_the_file_and_concatenating_reproduces_it_exactly(
    client: httpx.AsyncClient, app_state: AppState, piece_size: int
) -> None:
    """任意起点、任意长度切完拼回去 == 原文件。

    参数化那四个尺寸是有讲究的：1 挡"每段都 off-by-one"，7 是不整除的余数（最后一段
    必须短），64/255 是真实分块尺度。只测 `0-3` 的话，`start+length` 与 `end+1`
    搞混照样绿。
    """
    rel = await _media_file(app_state, body=SHORT_BODY)
    video_id = await _video(app_state, media_path=rel)

    pieces: list[bytes] = []
    start = 0
    while start < len(SHORT_BODY):
        end = min(start + piece_size, len(SHORT_BODY)) - 1
        resp = await client.get(
            f"/api/videos/{video_id}/media", headers={"Range": f"bytes={start}-{end}"}
        )
        assert resp.status_code == 206, f"bytes={start}-{end}"
        assert resp.headers["content-range"] == f"bytes {start}-{end}/{len(SHORT_BODY)}"
        assert resp.headers["content-length"] == str(end - start + 1)
        pieces.append(resp.content)
        start = end + 1

    assert b"".join(pieces) == SHORT_BODY


async def test_open_ended_and_suffix_ranges_both_land_on_the_right_bytes(
    client: httpx.AsyncClient, app_state: AppState
) -> None:
    """`bytes=N-`（拖进度条）与 `bytes=-N`（尾部）两种写法各自算对。"""
    rel = await _media_file(app_state)
    video_id = await _video(app_state, media_path=rel)

    tail = await client.get(f"/api/videos/{video_id}/media", headers={"Range": "bytes=1000-"})
    assert tail.status_code == 206
    assert tail.content == BODY[1000:]
    assert tail.headers["content-range"] == f"bytes 1000-{len(BODY) - 1}/{len(BODY)}"

    suffix = await client.get(f"/api/videos/{video_id}/media", headers={"Range": "bytes=-256"})
    assert suffix.content == BODY[-256:]
    assert suffix.headers["content-range"] == f"bytes {len(BODY) - 256}-{len(BODY) - 1}/{len(BODY)}"


async def test_a_range_that_cannot_be_served_says_416_with_the_total(
    client: httpx.AsyncClient, app_state: AppState
) -> None:
    """起点已经越过文件尾 → 416，且 `Content-Range: bytes */total`（播放器靠它校正）。"""
    rel = await _media_file(app_state)
    video_id = await _video(app_state, media_path=rel)

    resp = await client.get(
        f"/api/videos/{video_id}/media", headers={"Range": f"bytes={len(BODY) + 5}-"}
    )
    assert resp.status_code == 416
    assert resp.headers["content-range"] == f"bytes */{len(BODY)}"


async def test_a_range_we_do_not_model_falls_back_to_the_whole_file(
    client: httpx.AsyncClient, app_state: AppState
) -> None:
    """多区间 / 看不懂的 Range → **整段 200**，不是 400、不是 500。

    按 HTTP 语义，无法解析的 Range 必须被忽略而不是拒绝。为多区间背一套语义不值
    （`<video>` 不发），但把它做成 4xx 会让"进度条偶尔整段重下"变成"播放器直接报错"。
    """
    rel = await _media_file(app_state)
    video_id = await _video(app_state, media_path=rel)

    for raw in ("bytes=0-9,20-29", "items=0-9", "bytes=abc", "bytes="):
        resp = await client.get(f"/api/videos/{video_id}/media", headers={"Range": raw})
        assert resp.status_code == 200, raw
        assert resp.content == BODY, raw


# --------------------------------------------------------------------------- #
# 边界：不该出图的那些
# --------------------------------------------------------------------------- #


async def test_a_media_path_escaping_the_media_tree_is_refused_without_leaking_bytes(
    client: httpx.AsyncClient, app_state: AppState
) -> None:
    """库里那一列被人改成指向 `cookies/` 时，端点必须**不给内容**。

    `data/` 同一个根下就是 cookie 与 CDP profile（真凭证）。断的是"响应字节里没有那串
    凭证"，不是"状态码是 403" —— 后者对一个把内容写进 detail 的实现也成立。
    """
    secret = b"session-token=DEADBEEFNOTFORHTTP"
    cookie_file = app_state.files.cookies_dir / "example.com.txt"
    cookie_file.parent.mkdir(parents=True, exist_ok=True)
    cookie_file.write_bytes(secret)

    rel = app_state.files.rel(cookie_file)
    assert not rel.startswith("media/"), f"fixture 没搭对：{rel} 本来就在媒体树下"
    video_id = await _video(app_state, media_path=rel)

    resp = await client.get(f"/api/videos/{video_id}/media")
    assert resp.status_code == 403
    assert secret not in resp.content
    assert b"DEADBEEFNOTFORHTTP" not in resp.text.encode()


async def test_a_non_container_under_the_media_tree_is_not_served(
    client: httpx.AsyncClient, app_state: AppState
) -> None:
    """媒体树**不只有媒体**：稿子、metadata.json、封面、ASR 用的 wav 都在同一条作品目录下。

    "在 media/ 底下"因此不足以说明"这是能播的字节"。这一条钉的是白名单那一半。
    """
    note = (
        app_state.files.media_root / "bilibili" / "某UP" / "BV1x-media" / "transcript" / "srt.txt"
    )
    note.parent.mkdir(parents=True, exist_ok=True)
    note.write_text("第一句。", encoding="utf-8")
    video_id = await _video(app_state, media_path=app_state.files.rel(note))

    resp = await client.get(f"/api/videos/{video_id}/media")
    assert resp.status_code == 403
    assert "第一句" not in resp.text


@pytest.mark.parametrize(
    ("suffix", "expected"),
    [
        (".mp4", 200),
        (".webm", 200),
        (".mkv", 200),
        (".txt", 403),
        (".json", 403),
        (".sqlite3", 403),
    ],
)
async def test_only_the_listed_containers_come_out(
    client: httpx.AsyncClient, app_state: AppState, suffix: str, expected: int
) -> None:
    """白名单是一份**可数的**清单：加一种容器要有意识，少一种会红在这里。"""
    rel = await _media_file(app_state, name=f"media{suffix}")
    video_id = await _video(app_state, media_path=rel)
    resp = await client.get(f"/api/videos/{video_id}/media")
    assert resp.status_code == expected


# --------------------------------------------------------------------------- #
# 每种失败各有各的原文
# --------------------------------------------------------------------------- #


async def test_missing_row_and_missing_media_are_two_different_answers(
    client: httpx.AsyncClient, app_state: AppState
) -> None:
    """作品不存在 / 存在但没有落地文件，都是 404，但原文必须能分开。

    前端那一行"这条没有可播的媒体"只有在后端说清楚是哪一种时才不是猜的。
    """
    absent = await client.get("/api/videos/4242/media")
    assert absent.status_code == 404
    assert "4242" in absent.json()["detail"]

    video_id = await _video(app_state, media_path=None)
    no_media = await client.get(f"/api/videos/{video_id}/media")
    assert no_media.status_code == 404
    assert "没有落地媒体" in no_media.json()["detail"]
    assert no_media.json()["detail"] != absent.json()["detail"]


async def test_a_row_pointing_at_a_missing_file_says_the_file_is_not_there(
    client: httpx.AsyncClient, app_state: AppState
) -> None:
    """库里有行、盘上没文件（被移动过）→ 404，原文要指到那个路径。

    与"0 字节"一起构成"下载那一步失败过"的两种形状，都不许被读成"能播但没内容"。
    """
    video_id = await _video(app_state, media_path="media/bilibili/某UP/BV1x-media/absent.mp4")
    resp = await client.get(f"/api/videos/{video_id}/media")
    assert resp.status_code == 404
    assert "absent.mp4" in resp.json()["detail"]


async def test_a_zero_byte_media_file_is_refused_as_a_failed_download(
    client: httpx.AsyncClient, app_state: AppState
) -> None:
    """0 字节是失败产物，不是"一段空视频"。给 200 会让播放器显示一个永久 0:00 的进度条。"""
    path = app_state.files.media_root / "bilibili" / "某UP" / "BV1x-media" / "media.mp4"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"")
    video_id = await _video(app_state, media_path=app_state.files.rel(path))

    resp = await client.get(f"/api/videos/{video_id}/media")
    assert resp.status_code == 409
    assert "0 字节" in resp.json()["detail"]
