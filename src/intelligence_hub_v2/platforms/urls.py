"""跨平台共用的**纯解析**助手（ADR-0016）。

这里只放"不需要知道是哪个平台、也不碰网络"的东西。为什么要有这一个模块：
`as_http_url` / `require_http_url` / 中文计数解析在抖音与 B站 各有一份，
`platforms/bilibili/urls.py` 的 docstring 早就写了判据 ——

> 两处各十几行的重复比那个流程便宜……**第三个平台进来时就该通过 ADR 提到公共层** ——
> 三份重复就不再是权衡，而是漂移的开始。

现在第三个平台进来了（V2.1 小红书），所以提上来。为什么不放进 `platforms/base.py`：
`base.py` 是 V2→V3 的 Locked 契约模块（Protocol 与契约模型都住在那），
加实现层助手等于把契约面扩大；本模块是**实现细节**，V3 可以整个换掉。
也不放 `core/`：`core/` 在依赖图上方，而 `HttpUrl` 这种类型属于平台适配层的语义。
"""

from __future__ import annotations

import re

from pydantic import HttpUrl, TypeAdapter, ValidationError

__all__ = ["absolute_http_url", "parse_cn_count"]

_HTTP_URL_ADAPTER: TypeAdapter[HttpUrl] = TypeAdapter(HttpUrl)


def absolute_http_url(value: object) -> HttpUrl | None:
    """外部字符串 → `HttpUrl`，认不出返回 None。**协议相对地址会补成 `https:`**。

    页面与接口给出来的头像 / 封面 / 图片直链经常长成 `//sns-webpic-qc.xhscdn.com/…`
    或 `//i0.hdslb.com/…`。原样塞给 Pydantic 会 `ValidationError`（它要求带协议），
    而"这一张图的地址少个协议头"不该让整轮采集红掉 —— 补上就行。
    空串与真正不像 URL 的东西返回 None（对应字段都是可空的）。

    **抖音那份 `as_http_url()` 没有这一手**（`//…` 在抖音那边是直接返回 None 的）。
    这个差异是既有的，本模块不顺手统一：把抖音的行为改成"补协议"是一次真实的行为变更，
    要拿它自己的用例当证据另做（ADR-0016 的"后果"里记着这条欠账）。
    """
    text = str(value or "").strip()
    if not text:
        return None
    candidate = f"https:{text}" if text.startswith("//") else text
    try:
        return _HTTP_URL_ADAPTER.validate_python(candidate)
    except ValidationError:
        return None


_CN_UNIT_MULTIPLIERS = {"万": 10_000, "亿": 100_000_000, "w": 10_000, "W": 10_000}
_NUMBER_WITH_UNIT = re.compile(r"(\d+(?:\.\d+)?)\s*([万亿wW])?")


def parse_cn_count(value: object) -> int | None:
    """把页面上各种形状的**中文计数**收成 int，收不出返回 **None**（不是 0）。

    粉丝数与点赞/收藏数共用这一份：三个平台的作品卡片与主页 DOM 上都是
    "1.2万" / "3.5亿" / "10w+" / "粉丝 1.2万" 这种形状。

    与 V1 的方向**相反**：V1 的 `format_follower_count()` 是 int → "1.2万"，
    因为它的终点是飞书表格里的展示单元格。V2 的 `creators.follower_count` 与
    `videos.like_count` 都是 `INTEGER` 列，Feed 页要按它们排序 ——
    所以适配层要的是**解析**，把解析结果再格式化一遍等于把排序搞挂。

    返回 None 而不是 0 的理由：0 是一个**合法的**粉丝数（新号），
    而"读不出来"必须是可区分的第二种状态 —— 存 0 会让"这个号 0 粉"和
    "我们没解析出来"在看板上长得一模一样，而那正是 V1 §1.3 说的"看起来在跑"。
    """
    if isinstance(value, bool):
        # True 是 int 的子类，不挡一下会写出 follower_count=1
        return None
    if isinstance(value, (int, float)):
        return int(value) if value >= 0 else None
    text = str(value or "").strip().replace(",", "").replace(" ", "")
    if not text:
        return None
    match = _NUMBER_WITH_UNIT.search(text)
    if match is None:
        return None
    number = float(match.group(1))
    unit = match.group(2)
    if unit:
        number *= _CN_UNIT_MULTIPLIERS[unit]
    return int(number) if number >= 0 else None
