"""/api/export：把 creators / videos 两张表导成 CSV 或 JSON。

契约来源：`docs/plans/v2.1-migration-plan.md` T6.4。V1 的对应物是启动器里的
`export_csv` 那一路（终点是飞书表格与本地 Excel），V2 只保留"给人拿走数据"这一件事。

四条不显眼但会决定这份数据能不能用的规矩：

1. **CSV 必须防公式注入**。标题、昵称、口播稿、评论都是**外部输入**（`AGENTS.md` §1），
   而以 `=` `+` `-` `@` 开头的单元格在 Excel / WPS 里会被当公式执行 —— 一条标题写成
   `=cmd|' /C calc'!A0` 就能在打开的人机器上跑东西。导出的东西是要在别人电脑打开的，
   这比页面上的 XSS 更难追回。见 `_csv_cell()`。
2. **CSV 带 UTF-8 BOM**。中文 Windows 的 Excel 按 GBK 猜编码，没有 BOM 时整篇乱码，
   而"乱码"会被读成"数据丢了"。（`docs/lessons.md` 经验 51 是同一族的另一半：代码里
   读写要显式 utf-8，这里是给外部程序看的。）
3. **分页上限不许悄悄截断导出**。`/api/videos` 的 `size` 门是 200（列表页要的），
   导出要的是全表 —— 直接复用一次分页调用会得到一份"看起来完整、其实只有前 200 行"的
   CSV，而这种缺失**在文件里完全看不出来**。所以这里是翻页翻到 `total` 对齐为止。
4. **文件名里不许有外部输入**。它要进 `Content-Disposition` 响应头，一个带 `\r\n` 的
   platform 参数就能往响应头里塞东西（header injection）。文件名只由
   "实体 + 时间戳 + 扩展名"构成，参数一个字都不进。
"""

from __future__ import annotations

import csv
import io
import json
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Literal

from fastapi import APIRouter, Depends, Query, Response

from intelligence_hub_v2.api.deps import get_state
from intelligence_hub_v2.models.video import Page, VideoFilters

if TYPE_CHECKING:
    from collections.abc import Sequence

    from intelligence_hub_v2.api.deps import AppState
    from intelligence_hub_v2.models.creator import Creator
    from intelligence_hub_v2.models.video import Video
    from intelligence_hub_v2.storage.db import SqliteStorage

router = APIRouter(tags=["export"])

ExportEntity = Literal["videos", "creators"]
ExportFormat = Literal["csv", "json"]
HiddenMode = Literal["visible", "hidden", "all"]

_PAGE_SIZE = 200
"""一次取多少行。**只是游标大小，不是导出上限** —— 见模块 docstring 第 3 条。"""

_CSV_LINE_ENDING = "\r\n"
"""RFC 4180 要求 CRLF。`csv.writer` 默认也是 `\r\n`，写出来是为了不让下一个人"顺手改成 `\n`"。"""

#: 列清单是手写的，不是从模型字段推出来的。两个理由：
#: (1) 新增一个模型字段不该**自动**出现在导出里（那等于把内部结构当对外契约）；
#: (2) `metadata_json` 里装着 V1 的整段 raw_data（几十 KB 一行），
#:     人要看的是计数与标题，不是转义过的 JSON 字符串。
_COLUMNS: dict[str, tuple[str, ...]] = {
    "videos": (
        "id",
        "platform",
        "platform_video_id",
        "title",
        "creator_id",
        "published_at",
        "duration_seconds",
        "view_count",
        "like_count",
        "comment_count",
        "share_count",
        "media_path",
        "media_source",
        "is_hidden",
        "created_at",
    ),
    "creators": (
        "id",
        "platform",
        "platform_id",
        "name",
        "follower_count",
        "is_tracking",
        "profile_url",
        "created_at",
    ),
}

_UNSAFE_PREFIXES = ("=", "+", "-", "@", "\t", "\r")
"""CSV 注入的起手字符。`-` 也在列里：`=1+1` 那类公式的常见变体。"""


@router.get("/export")
async def export_table(
    state: AppState = Depends(get_state),
    entity: ExportEntity = Query(default="videos"),
    export_format: ExportFormat = Query(default="csv", alias="format"),
    platform: str | None = None,
    hidden: HiddenMode = Query(default="visible"),
    search: str | None = None,
) -> Response:
    """导出一张表。`hidden` 的默认值与 `/api/videos` 一致（visible）——
    导出默认含隐藏行会让人以为"删掉的作品还在"，那是另一种口径分叉。"""
    rows = await _collect(
        state.storage, entity=entity, platform=platform, hidden=hidden, search=search
    )
    stamped = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    filename = f"intelligence-hub-{entity}-{stamped}.{export_format}"
    disposition = f'attachment; filename="{filename}"'
    columns = _COLUMNS[entity]
    if export_format == "json":
        return Response(
            content=_to_json(rows, columns=columns),
            media_type="application/json; charset=utf-8",
            headers={"Content-Disposition": disposition},
        )
    return Response(
        content=_to_csv(rows, columns=columns),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": disposition},
    )


async def _collect(
    storage: SqliteStorage,
    *,
    entity: ExportEntity,
    platform: str | None,
    hidden: HiddenMode,
    search: str | None,
) -> list[Video] | list[Creator]:
    if entity == "creators":
        return await storage.creators.list_all(platform=platform)
    is_hidden: bool | None = {"visible": False, "hidden": True, "all": None}[hidden]
    # `search` 也在：Feed 页那个"导出"按钮承诺的是**屏幕上这一列**，
    # 少一个筛选项就是"看起来导出了当前的，其实导出了全部"（同 §1.3 那一族）。
    filters = VideoFilters(platform=platform, is_hidden=is_hidden, search=search)
    collected: list[Video] = []
    page = 1
    while True:
        result = await storage.videos.list_visible(
            filters=filters, page=Page(page=page, size=_PAGE_SIZE)
        )
        collected.extend(result.items)
        if not result.items or len(collected) >= result.total:
            return collected
        page += 1


def _scalar(value: object) -> object:
    """模型字段 → 可序列化标量。布尔转 `true/false` 小写、datetime 走 isoformat。"""
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, datetime):
        return value.isoformat()
    return value


def _csv_cell(value: object) -> str:
    """一个单元格的文本，**带公式注入中和**。

    中和方式是加一个前导 `'`（Excel/WPS 与 Google Sheets 都认它"这是文本"），
    而不是删掉那个字符 —— 数据要留着可读，`-5` 那种合法取值删掉首字符就变成 `5` 了。

    **只中和字符串**。数字与日期不会是公式（`-5` 在 Excel 里就是个数），
    给它们加前缀反而把 `-5` 变成文本、把求和搞坏；真正危险的是标题/昵称/口播稿这些
    **外部输入**，而它们全是 `str`。前导空白也判：`" =SUM(A1)"` 同样会被当公式执行。
    """
    if value is None:
        return ""
    if not isinstance(value, str):
        return str(_scalar(value))
    head = value.lstrip()[:1]
    return f"'{value}" if head in _UNSAFE_PREFIXES else value


def _to_csv(rows: Sequence[object], *, columns: tuple[str, ...]) -> bytes:
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator=_CSV_LINE_ENDING)
    writer.writerow(columns)
    for row in rows:
        writer.writerow([_csv_cell(getattr(row, name, None)) for name in columns])
    # BOM：见模块 docstring 第 2 条。没有它，中文 Windows 的 Excel 会把 UTF-8 当 GBK 猜。
    return b"\xef\xbb\xbf" + buffer.getvalue().encode("utf-8")


def _to_json(rows: Sequence[object], *, columns: tuple[str, ...]) -> bytes:
    payload = [{name: _scalar(getattr(row, name, None)) for name in columns} for row in rows]
    return json.dumps(payload, ensure_ascii=False, indent=1).encode("utf-8")
