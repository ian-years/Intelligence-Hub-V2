"""`/api/videos/{id}/shots`：工坊「截图包」的真数据源。

整批用例吃一个假 seam：monkeypatch `infra/ffmpeg.run_subprocess`，由它真的往磁盘写
一小段 JPEG 字节。为什么这一层要假件而不是直接调真 ffmpeg：这一层的判据是**文件语义**
（谁覆盖谁、落在哪、失败怎么说、越界给不给得出图），而那些与"某一秒的画面长什么样"无关；
把它们混在一条用例里，坏了分不清是哪一层。
"截出来确实是那一秒"由 `test_shots_real_ffmpeg.py`（`-m real_network`）用真 9.0.1 验，
两条各管一头。

断言写成关系，因为坏法各有不同：

- **每一帧都在自己的 shots 目录下，且文件名对时间点是单射** —— 否则点一次按钮攒一套垃圾，
  `cached` 就永远是假的。
- **帧数 == min(请求的时间点数, 上限)**，且**超限那条一个字节都不许写** ——
  半套产物比没有产物难查（界面上看着像成功了一半）。
- **ffmpeg 不在场时不是 200 + 空列表**：那会把"这台机器截不了图"渲染成"这条作品没有分镜"，
  两者在界面上完全同形（AGENTS.md §1.3）。
- **越界出图**：先在 `data/cookies/` 埋一份可识别的假凭证，再从 shots 端点尝试走过去。
  断的是"响应里没有那串字节"，不是"状态码是 403"。
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

import httpx
import pytest

from intelligence_hub_v2.api.deps import AppState
from intelligence_hub_v2.api.v1 import shots as shots_module
from intelligence_hub_v2.infra import ffmpeg as ffmpeg_module
from intelligence_hub_v2.infra.subprocess import SubprocessResult
from intelligence_hub_v2.models.video import VideoDraft

pytestmark = pytest.mark.integration

#: 一小段真 JPEG 头（SOI + APP0），用来让"文件确实有内容"这件事是真的。
JPEG_BYTES = bytes.fromhex("ffd8ffe000104a46494600010100000100010000") + b"\x11" * 24 + b"ffd9"
MP4_BYTES = b"\x00\x00\x00\x18ftypmp42" + b"\x00" * 32
COOKIE_TEXT = "DOUYIN_SESSION_SECRET_DO_NOT_SERVE=abc123"
#: 别人作品的一张"合规扩展名"的图：只有"不许走出 shots 目录"这一条判据挡得住它。
SECRET_JPEG = JPEG_BYTES[:-3] + b"\x99\x99\xff\xd9"


class FakeFfmpeg:
    """按 argv 产出帧字节的假 `run_subprocess`。"""

    def __init__(self, *, broken: set[str] | None = None, missing_binary: bool = False) -> None:
        self.calls: list[list[str]] = []
        self.broken = broken or set()
        self.missing_binary = missing_binary

    async def __call__(self, argv: Any, **_kwargs: Any) -> SubprocessResult:
        args = [str(part) for part in argv]
        self.calls.append(args)
        if self.missing_binary:
            raise LookupError(f"找不到可执行文件 'ffmpeg'（命令：{' '.join(args)[:80]}）")
        if "-version" in args:
            return self._result(args, stdout="ffmpeg version FAKE-6.1 built for test")
        out = Path(args[-1])
        if any(token in out.name for token in self.broken):
            return self._result(
                args, returncode=1, stderr="Invalid data found when processing input"
            )
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(JPEG_BYTES)
        return self._result(args)

    @staticmethod
    def _result(
        args: list[str], *, returncode: int = 0, stdout: str = "", stderr: str = ""
    ) -> SubprocessResult:
        return SubprocessResult(
            argv=tuple(args),
            returncode=returncode,
            stdout=stdout,
            stderr=stderr,
            duration_seconds=0.05,
        )

    @property
    def frame_calls(self) -> int:
        return sum(1 for args in self.calls if "-version" not in args)


@pytest.fixture
def fake(monkeypatch: pytest.MonkeyPatch) -> FakeFfmpeg:
    seam = FakeFfmpeg()
    monkeypatch.setattr(ffmpeg_module, "run_subprocess", seam)
    return seam


async def _video(
    app_state: AppState,
    *,
    duration: float | None = 100.0,
    media: bool = True,
    title: str = "怎么把资料存成第二大脑",
    platform_video_id: str = "BV1x",
) -> tuple[int, Path]:
    """建一条作品行 + （可选）真的把成片落到媒体目录里，返回 `(id, 该作品的媒体目录)`。"""
    media_dir = app_state.files.media_dir("bilibili", "某UP", platform_video_id, title)
    stored: str | None = None
    if media:
        media_dir.mkdir(parents=True, exist_ok=True)
        (media_dir / "media.mp4").write_bytes(MP4_BYTES)
        stored = app_state.files.rel(media_dir / "media.mp4")
    row = await app_state.storage.videos.insert(
        VideoDraft(
            platform="bilibili",
            platform_video_id=platform_video_id,
            title=title,
            media_path=stored,
            duration_seconds=duration,
        )
    )
    return row.id, media_dir


def _shot_files(media_dir: Path) -> list[Path]:
    shots = media_dir / "shots"
    return sorted(shots.glob("*")) if shots.is_dir() else []


# ---------------------------------------------------------------------------
# 真截：产物落在哪、叫什么、能不能认出是同一帧
# ---------------------------------------------------------------------------


async def test_every_frame_lands_in_its_own_shots_dir_with_one_file_per_time_point(
    client: httpx.AsyncClient, app_state: AppState, fake: FakeFfmpeg
) -> None:
    """每一帧都在 `<媒体目录>/shots/` 底下，文件名对 `at_seconds` **单射**。"""
    video_id, media_dir = await _video(app_state)
    resp = await client.post(f"/api/videos/{video_id}/shots", json={"at_seconds": [0, 8, 65]})
    assert resp.status_code == 200
    body = resp.json()
    assert [item["at_seconds"] for item in body["shots"]] == [0.0, 8.0, 65.0]
    assert [item["path"] for item in body["shots"]] == [
        f"media/bilibili/某UP/{media_dir.name}/shots/shot-{second}.jpg"
        for second in ("0", "8", "65")
    ]
    names = [Path(item["path"]).name for item in body["shots"]]
    assert len(set(names)) == len(names)
    shots_dir = (media_dir / "shots").resolve()
    for item in body["shots"]:
        path = app_state.files.abs(item["path"])
        assert path.is_relative_to(shots_dir)
        assert path.stat().st_size == len(JPEG_BYTES) == item["size_bytes"]
    assert fake.frame_calls == 3


async def test_the_image_endpoint_serves_exactly_the_bytes_on_disk(
    client: httpx.AsyncClient, app_state: AppState, fake: FakeFfmpeg
) -> None:
    """`url` 与磁盘**同源**：前端拿到的地址必须交出那份文件本身，逐字节。

    只断 200 的话，一个"回一张内置占位图"的实现也算绿。
    """
    video_id, media_dir = await _video(app_state)
    body = (await client.post(f"/api/videos/{video_id}/shots", json={"at_seconds": [8]})).json()
    url = body["shots"][0]["url"]
    resp = await client.get(url)
    assert resp.status_code == 200
    assert resp.content == JPEG_BYTES
    assert resp.headers["content-type"] == "image/jpeg"
    assert (media_dir / "shots" / "shot-8.jpg").read_bytes() == JPEG_BYTES


async def test_a_second_request_reports_cached_and_spawns_nothing_new(
    client: httpx.AsyncClient, app_state: AppState, fake: FakeFfmpeg
) -> None:
    """同一批时间点第二次请求：`cached` 必须为真，且**一次 ffmpeg 都不起**。

    这条同时钉"重复截是覆盖而不是攒垃圾"：磁盘上仍然只有三份文件。
    """
    video_id, media_dir = await _video(app_state)
    first = (
        await client.post(f"/api/videos/{video_id}/shots", json={"at_seconds": [0, 8, 65]})
    ).json()
    assert first["cached"] is False
    after_first = fake.frame_calls

    second = await client.post(f"/api/videos/{video_id}/shots", json={"at_seconds": [0, 8, 65]})
    assert second.status_code == 200
    assert second.json()["cached"] is True
    assert fake.frame_calls == after_first
    assert len(_shot_files(media_dir)) == 3


async def test_a_partially_new_batch_is_not_reported_as_cached(
    client: httpx.AsyncClient, app_state: AppState, fake: FakeFfmpeg
) -> None:
    """三帧里有一帧是新截的，`cached` 就得是假 —— 否则界面会把"刚截好一张"说成"没动过"。"""
    video_id, _ = await _video(app_state)
    await client.post(f"/api/videos/{video_id}/shots", json={"at_seconds": [0, 8]})
    body = (
        await client.post(f"/api/videos/{video_id}/shots", json={"at_seconds": [0, 8, 65]})
    ).json()
    assert body["cached"] is False
    assert [item["produced"] for item in body["shots"]] == [False, False, True]


async def test_the_same_second_written_two_ways_still_uses_one_file(
    client: httpx.AsyncClient, app_state: AppState, fake: FakeFfmpeg
) -> None:
    """`8.5` 与 `8.500` 是同一帧：请求内部先去重，磁盘上只有一份。

    不归一的话浮点尾数每次都能生成一个新文件名，`cached` 就永远是假的。
    """
    video_id, media_dir = await _video(app_state)
    body = (
        await client.post(
            f"/api/videos/{video_id}/shots", json={"at_seconds": [8.5, 8.500, 60 / 7]}
        )
    ).json()
    assert body["requested_at"] == [8.5, 8.571]
    assert len(_shot_files(media_dir)) == 2


async def test_omitting_the_times_splits_the_duration_and_never_ends_on_the_last_frame(
    client: httpx.AsyncClient, app_state: AppState, fake: FakeFfmpeg
) -> None:
    """省略 `at_seconds` → 按时长均分：递增、都在片内、且**不超过上限**。

    最后一帧压在片尾那一瞬时 ffmpeg 会退出 0 且不产出，那不是失败而是空手而归，
    会伪装成"截图包坏了一半"。
    """
    duration = 100.0
    video_id, _ = await _video(app_state, duration=duration)
    body = (await client.post(f"/api/videos/{video_id}/shots", json={})).json()
    times = body["requested_at"]
    assert times == sorted(times)
    assert len(times) == len(set(times))
    assert times[0] >= 0
    assert times[-1] < duration
    assert 1 <= len(times) <= 12
    assert len(body["shots"]) == len(times)


async def test_the_frame_count_is_the_minimum_of_requests_and_the_cap(
    client: httpx.AsyncClient, app_state: AppState, fake: FakeFfmpeg
) -> None:
    """帧数 == min(请求的时间点数, 上限)。"""
    video_id, _ = await _video(app_state)
    wanted = [float(second) for second in range(6)]
    body = (await client.post(f"/api/videos/{video_id}/shots", json={"at_seconds": wanted})).json()
    assert len(body["shots"]) == min(len(wanted), 12)


# ---------------------------------------------------------------------------
# 拒绝：各有原文，不许并成一句"失败"
# ---------------------------------------------------------------------------


async def test_asking_for_too_many_frames_writes_not_a_single_byte(
    client: httpx.AsyncClient, app_state: AppState, fake: FakeFfmpeg
) -> None:
    """超限是 422，并且**一个文件都没写**：半套产物比没有产物难查。"""
    video_id, media_dir = await _video(app_state)
    times = [float(second) for second in range(13)]
    resp = await client.post(f"/api/videos/{video_id}/shots", json={"at_seconds": times})
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert "12" in detail and str(len(times)) in detail
    assert _shot_files(media_dir) == []
    assert fake.frame_calls == 0


async def test_a_negative_time_point_is_rejected_with_the_value_that_broke_it(
    client: httpx.AsyncClient, app_state: AppState, fake: FakeFfmpeg
) -> None:
    video_id, media_dir = await _video(app_state)
    resp = await client.post(f"/api/videos/{video_id}/shots", json={"at_seconds": [-1]})
    assert resp.status_code == 422
    assert "-1" in resp.json()["detail"]
    assert _shot_files(media_dir) == []


async def test_no_times_and_no_duration_says_what_is_missing(
    client: httpx.AsyncClient, app_state: AppState, fake: FakeFfmpeg
) -> None:
    """均分需要时长。没有时长元数据时不能猜一个，也不能默默返回空包。"""
    video_id, media_dir = await _video(app_state, duration=None)
    resp = await client.post(f"/api/videos/{video_id}/shots", json={})
    assert resp.status_code == 422
    assert "duration_seconds" in resp.json()["detail"]
    assert _shot_files(media_dir) == []
    assert fake.frame_calls == 0


async def test_a_video_without_media_a_missing_file_and_a_zero_byte_file_each_speak(
    client: httpx.AsyncClient, app_state: AppState
) -> None:
    """三种"截不了"各有原文。并成一句"失败"就等于让前端去猜。"""
    no_media, _ = await _video(app_state, media=False, platform_video_id="BVnomedia")
    gone, gone_dir = await _video(app_state, platform_video_id="BVgone")
    gone_dir.joinpath("media.mp4").unlink()
    empty, empty_dir = await _video(app_state, platform_video_id="BVempty")
    empty_dir.joinpath("media.mp4").write_bytes(b"")

    responses = [
        await client.post(f"/api/videos/{video_id}/shots", json={"at_seconds": [0]})
        for video_id in (no_media, gone, empty)
    ]
    details = [str(item.json()["detail"]) for item in responses]
    assert [item.status_code for item in responses] == [404, 404, 409]
    assert len({*details}) == 3, f"三种失败说了同一句话：{details}"
    assert "媒体" in details[0]
    assert "文件不在" in details[1] or "不在" in details[1]
    assert "0 字节" in details[2]

    missing = await client.post("/api/videos/999999/shots", json={"at_seconds": [0]})
    assert missing.status_code == 404
    assert "999999" in str(missing.json()["detail"])


async def test_a_missing_ffmpeg_is_a_red_actionable_answer_not_an_empty_package(
    client: httpx.AsyncClient, app_state: AppState, monkeypatch: pytest.MonkeyPatch
) -> None:
    """ffmpeg 不在场：状态非 2xx、原文里有"ffmpeg"与一条能照着做的下一步、磁盘上没产物。

    前置防空转：这台机器 `where ffmpeg` 为空（AGENTS.md §5），所以这一条走的是真分支 ——
    另外断 seam 确实被调用过，避免"根本没打到 ffmpeg 那一层"也算绿。
    """
    seam = FakeFfmpeg(missing_binary=True)
    monkeypatch.setattr(ffmpeg_module, "run_subprocess", seam)
    video_id, media_dir = await _video(app_state)

    resp = await client.post(f"/api/videos/{video_id}/shots", json={"at_seconds": [0, 8]})
    assert resp.status_code >= 400
    assert not (200 <= resp.status_code < 300)
    detail = str(resp.json()["detail"])
    assert "ffmpeg" in detail
    assert "paths.ffmpeg" in detail
    assert seam.calls, "这一条没打到 ffmpeg 那一层，等于什么都没测"
    assert _shot_files(media_dir) == []


async def test_seconds_that_ffmpeg_cannot_decode_are_reported_one_by_one(
    client: httpx.AsyncClient,
    app_state: AppState,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """坏的是那一秒，不是这一批：200 + 成功的帧 + 逐条原因，而不是 500。"""
    seam = FakeFfmpeg(broken={"shot-8.jpg"})
    monkeypatch.setattr(ffmpeg_module, "run_subprocess", seam)
    video_id, _ = await _video(app_state)

    resp = await client.post(f"/api/videos/{video_id}/shots", json={"at_seconds": [0, 8, 65]})
    assert resp.status_code == 200
    body = resp.json()
    assert [item["at_seconds"] for item in body["shots"]] == [0.0, 65.0]
    assert [item["at_seconds"] for item in body["failures"]] == [8.0]
    assert "Invalid data" in body["failures"][0]["reason"]


async def test_a_batch_where_nothing_came_out_is_a_conflict_not_an_empty_success(
    client: httpx.AsyncClient,
    app_state: AppState,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """一帧都没有时不许回 200 + 空列表：那长得和"这条作品没有分镜"一模一样。"""
    times = [float(second) for second in range(3)]
    seam = FakeFfmpeg(broken={f"shot-{int(second)}.jpg" for second in times})
    monkeypatch.setattr(ffmpeg_module, "run_subprocess", seam)
    video_id, _ = await _video(app_state)

    resp = await client.post(f"/api/videos/{video_id}/shots", json={"at_seconds": times})
    assert resp.status_code == 409
    assert "Invalid data" in str(resp.json()["detail"])


# ---------------------------------------------------------------------------
# 出图边界：越界一律拒
# ---------------------------------------------------------------------------


def _escape_spellings(rel: str) -> list[str]:
    """把一条"从 `shots/` 走出去"的相对路径变成各种写法。

    全部由**实际算出来的相对路径**派生，不是手抄的：手抄的那版把诱饵埋在
    `media/bilibili/某UP/BV9-别的片子/`，而真实目录名带作品 id 前缀，
    于是那些 payload 在磁盘上根本指向不存在的地方 —— 把三道判据全拆掉它照样绿。
    （2026-09-25 那条空转用例就是这么来的，见 `docs/lessons.md`。）

    编码的几种是**真会打到应用层的**；字面 `..` 那几种会被 httpx 按 RFC 3986
    在客户端就把 `.` 段消掉，永远进不了路由。所以这个清单不能只用"状态码不是 2xx"
    来判定自己在测什么 —— 下面那条 `reached` 计数看的就是这件事。
    """
    up = rel.replace("\\", "/")
    variants = {
        up,  # 字面（多半被客户端消掉）
        up.replace("..", "%2e%2e"),
        up.replace("/", "%2f"),
        up.replace("..", "%2e%2e").replace("/", "%2f"),
        up.replace("/", "\\"),
        "..%2f" + up.split("../", 1)[-1] if up.startswith("../") else up,
        up.replace("../", "....//", 1),
    }
    return sorted(variants)


async def test_the_shot_image_endpoint_cannot_be_walked_out_of_its_own_directory(
    client: httpx.AsyncClient, app_state: AppState, fake: FakeFfmpeg
) -> None:
    """从 `shots/` 走出去的写法都拿不到别的文件：既拿不到凭证，也拿不到**别人的图**。

    断的是"响应里没有那串字节"，不是"状态码是 403"：一个把内容塞进 detail 的实现
    只断 403 也算绿，而那是同一个洞换了个出口。

    两份诱饵的扩展名不一样，是因为它们各挡一层：
    - `cookies/*.txt` 由**图片白名单**挡（判据 2）。
    - 邻居作品的一张 `.jpg` 名字与扩展名都合规，只差目录 —— 只有"不许走出自己那个
      `shots/`"这一条挡得住它（判据 1 / 3）。

    这一条**被变异验证过**（2026-09-25，harness 见 `docs/progress/2026-09-25.md`）：
    同时拆掉判据 1 与 3 → 本条转红（那张 `.jpg` 真的被交出去了）；只拆 1 → 仍绿（3 接住）。
    它的前身没做过这件事，而前身是空转的：诱饵按**猜的目录名**埋（真目录名带作品 id 前缀），
    payload 在磁盘上指向一个不存在的路径，三层全拆也照样绿。所以上面那截
    "相对路径必须真的指到诱饵"不是仪式，是这一条有意义的前提。
    """
    app_state.files.cookies_dir.mkdir(parents=True, exist_ok=True)
    cookie_file = app_state.files.cookies_dir / "www.douyin.com.txt"
    cookie_file.write_text(COOKIE_TEXT, encoding="utf-8")

    video_id, media_dir = await _video(app_state)
    await client.post(f"/api/videos/{video_id}/shots", json={"at_seconds": [8]})
    shots_dir = app_state.files.shots_dir(media_dir)

    # 邻居作品：同一个博主、另一条作品的目录（命名规则与真目录一致，才走得到）。
    neighbour_dir = app_state.files.media_dir("bilibili", "某UP", "BV9", "别的片子")
    neighbour_dir.mkdir(parents=True, exist_ok=True)
    neighbour_jpg = neighbour_dir / "cover.jpg"
    neighbour_jpg.write_bytes(SECRET_JPEG)

    # ---- 防空转的前置：这两个相对路径**确实**指到那两份诱饵上 ----
    # 没有这一截，上面的 payload 全部可以指向空气，而用例仍然全绿。
    cookie_rel = os.path.relpath(cookie_file, shots_dir).replace("\\", "/")
    jpg_rel = os.path.relpath(neighbour_jpg, shots_dir).replace("\\", "/")
    for rel, target in ((cookie_rel, cookie_file), (jpg_rel, neighbour_jpg)):
        assert (shots_dir / rel).resolve() == target.resolve(), (
            f"诱饵埋错了地方：从 {shots_dir} 走 {rel!r} 到不了 {target}"
        )
        assert target.is_file(), target

    escapes = [
        f"/api/videos/{video_id}/shots/{sp}"
        for rel in (cookie_rel, jpg_rel)
        for sp in _escape_spellings(rel)
    ]
    assert escapes, "一条逃逸写法都没生成 = 这条用例在空跑"

    reached_handler = 0
    for url in escapes:
        resp = await client.get(url)
        assert not (200 <= resp.status_code < 300), f"{url} 居然给了 2xx：{resp.status_code}"
        assert resp.content != SECRET_JPEG, f"{url} 把别人作品的图交出去了"
        assert SECRET_JPEG.hex() not in resp.text, url
        assert COOKIE_TEXT not in resp.text, url
        if resp.status_code == 403:
            reached_handler += 1

    # ---- 这一条才是"用例没被客户端归一化悄悄废掉"的看护 ----
    # 只断"没有 2xx"的话，一个"所有写法都被 httpx 消成别的 URL、一次都没进路由"的
    # 环境变化会让这条用例变成零断言。编码那几种今天必进路由，所以给了下限。
    assert reached_handler >= 3, (
        f"{len(escapes)} 条写法里只有 {reached_handler} 条打到应用层被判 403 —— "
        "剩下的都在客户端就被消掉了，这条用例的覆盖面正在消失"
    )


async def test_a_file_name_that_escapes_the_shots_dir_is_rejected_by_the_containment_check(
    tmp_path: Path,
) -> None:
    r"""判据 3（解析后仍在 `shots/` 底下）单测它自己 —— 判据 1 在场时它**从 HTTP 打不到**。

    为什么单独测函数而不是补一条走 transport 的用例：判据 1 的正则已经把正斜杠、反斜杠
    与 `..` 全挡在门外，而一个不含任何分隔符的名字在数学上走不出目录。所以今天没有任何
    输入能走到第 3 层，硬造一条"通过 HTTP 覆盖三层"的用例会是一句假话。
    但它**不是死代码**：2026-09-25 变异实测 —— 只拆判据 1（`if False`）时那三条
    `%2e%2e/` 写法照样被这一层拦下（用例仍绿），同时拆掉 1 与 3 才漏（用例转红）。
    也就是说它是"哪天有人放宽入口闸门"时唯一还在的那道，值得有自己的可变异用例：
    把 `relative_to` 那三行改成 `return True`，这一条必须红（实测红）。
    """
    root = tmp_path / "shots"
    root.mkdir()
    assert shots_module._is_under(root / "shot-8.jpg", root)
    assert shots_module._is_under(root, root), "等于自己也算在内（只读目录名那一步）"
    assert not shots_module._is_under(tmp_path / "cookies" / "a.txt", root)
    assert not shots_module._is_under(Path("C:/other/shot.jpg"), root)
    # Windows 上 `Path` 吃反斜杠，所以这一形也钉一下（判据 1 之外它走不到这里，
    # 但 `_is_under` 是纯函数，将来被别处复用时不该只认一种分隔符）。
    assert not shots_module._is_under(Path("C:\\other\\shot.jpg"), root)


async def test_only_image_extensions_come_out_of_the_shots_directory(
    client: httpx.AsyncClient, app_state: AppState, fake: FakeFfmpeg
) -> None:
    """ "在 shots 目录下"不等于"这是能给 `<img>` 的字节"。"""
    video_id, media_dir = await _video(app_state)
    shots = media_dir / "shots"
    shots.mkdir(parents=True, exist_ok=True)
    (shots / "notes.txt").write_text("一段稿子，不该被这个端点出去", encoding="utf-8")
    (shots / "clip.mp4").write_bytes(MP4_BYTES)

    for name in ("notes.txt", "clip.mp4"):
        resp = await client.get(f"/api/videos/{video_id}/shots/{name}")
        assert resp.status_code == 403, name
        assert "稿子" not in resp.text
    missing = await client.get(f"/api/videos/{video_id}/shots/shot-404.jpg")
    assert missing.status_code == 404


async def test_a_zero_byte_frame_is_not_served_as_a_thumbnail(
    client: httpx.AsyncClient, app_state: AppState, fake: FakeFfmpeg
) -> None:
    """0 字节的帧是失败产物：如实 409 并给出修法，别让 `<img>` 猜。"""
    video_id, media_dir = await _video(app_state)
    shots = media_dir / "shots"
    shots.mkdir(parents=True, exist_ok=True)
    (shots / "shot-7.jpg").write_bytes(b"")
    resp = await client.get(f"/api/videos/{video_id}/shots/shot-7.jpg")
    assert resp.status_code == 409
    assert "0 字节" in str(resp.json()["detail"])


async def test_images_of_a_video_whose_media_went_missing_are_not_served(
    client: httpx.AsyncClient, app_state: AppState, fake: FakeFfmpeg
) -> None:
    """出图依赖"这条作品还有成片"：媒体没了，帧也不该继续可取（原文说清是哪一头坏）。"""
    video_id, media_dir = await _video(app_state, platform_video_id="BVgone2")
    await client.post(f"/api/videos/{video_id}/shots", json={"at_seconds": [8]})
    (media_dir / "media.mp4").unlink()
    resp = await client.get(f"/api/videos/{video_id}/shots/shot-8.jpg")
    assert resp.status_code == 404
    assert "media.mp4" in str(resp.json()["detail"])


# ---------------------------------------------------------------------------
# 契约：响应形状与 ffmpeg 附注
# ---------------------------------------------------------------------------


async def test_the_response_carries_which_ffmpeg_produced_the_frames(
    client: httpx.AsyncClient, app_state: AppState, fake: FakeFfmpeg
) -> None:
    """真的截了帧 → 响应要说清是哪一支 ffmpeg 干的（V1 §7.19 的"换了个二进制而没人知道"）。"""
    video_id, _ = await _video(app_state)
    body = (await client.post(f"/api/videos/{video_id}/shots", json={"at_seconds": [0]})).json()
    assert "FAKE-6.1" in str(body["ffmpeg_version_or_error"])


async def test_a_purely_reused_batch_does_not_claim_a_version(
    client: httpx.AsyncClient, app_state: AppState, fake: FakeFfmpeg
) -> None:
    """纯复用时不去问版本：那一栏说的是"谁产的"，没产就不该有。"""
    video_id, _ = await _video(app_state)
    await client.post(f"/api/videos/{video_id}/shots", json={"at_seconds": [0]})
    body = (await client.post(f"/api/videos/{video_id}/shots", json={"at_seconds": [0]})).json()
    assert body["cached"] is True
    assert body["ffmpeg_version_or_error"] is None


async def test_the_route_shape_the_frontend_relies_on(
    client: httpx.AsyncClient, app_state: AppState, fake: FakeFfmpeg
) -> None:
    """响应里的键集合是前端 `ShotsBody` 的同源契约，多一少一都算漂。"""
    video_id, _ = await _video(app_state)
    resp = await client.post(f"/api/videos/{video_id}/shots", json={"at_seconds": [0]})
    assert set(resp.json()) == {
        "video_id",
        "requested_at",
        "shots",
        "cached",
        "failures",
        "ffmpeg_version_or_error",
    }
    assert set(resp.json()["shots"][0]) == {
        "at_seconds",
        "path",
        "url",
        "size_bytes",
        "produced",
    }
    spec = json.loads((await client.get("/openapi.json")).text)
    assert "/api/videos/{video_id}/shots" in spec["paths"]
    assert "post" in spec["paths"]["/api/videos/{video_id}/shots"]
