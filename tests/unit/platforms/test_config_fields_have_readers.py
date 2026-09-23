"""每个平台配置字段都必须**要么被代码读，要么标 `ui:hidden` 并说明原因**（ADR-0012）。

判据抄 `docs/adr/0011` 自己那条：「一个前端能渲染出来、后端没有实现路径的配置取值，
就是在给用户撒谎」。ADR-0011 用这条删掉了 `persist_play_url`，但没有任何东西阻止
下一个长出来 —— 而 `/api/platforms/{name}/schema` **今天就在吐**这些模型的
JSON Schema，Settings 页（Task 12）就是照它渲染表单的。

所以修法是标 `ui:hidden` 而不是记账：这个标记是**已发布 API 的一部分**，
前端不必去读一个 Python 测试文件才知道哪个开关不能渲染。

为什么不干脆删字段：这些模型是 `extra="forbid"`，删字段等于让老用户手上那份
写过该键的 `platforms.yaml` 下次启动 `ConfigError` —— 那是把"表单上一个空开关"
换成"升级后服务起不来"。

三条断言合起来是个双向棘轮：新增没人读的字段 → 红；字段被实现了却还留着隐藏 → 也红。
"""

from __future__ import annotations

import ast
import re
from functools import cache
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from intelligence_hub_v2.platforms import PLATFORM_CONFIG_SCHEMAS

# 仓库根在 parents[3]（platforms → unit → tests → 根）。写错一级得到的是一个不存在的
# 目录、corpus 变成空串、于是**所有字段都被报成没人读** —— 一个自己坏掉的守卫。
_SRC = Path(__file__).resolve().parents[3] / "src" / "intelligence_hub_v2"

#: 只排"声明模型"的那些文件。用文件名 `config.py` 粗筛会连 `core/config.py` 与
#: `api/v1/config.py` 一起排掉，而那两个恰恰是读 `cfg.enabled` 的地方（第一版就这么错过）。
_DEFINITION_FILES = re.compile(r"platforms/(base\.py|[a-z0-9]+/config\.py)$")

#: 被隐藏字段的描述里必须出现其中之一 —— 隐藏而不解释，等于把撒谎从表单挪进注释缺失。
_EXPLANATION_MARKERS = ("V2.1", "未实现", "不受本字段控制", "没有效果", "由 capabilities", "真源")


@cache
def _reader_corpus(platform: str) -> str:
    """读判据的语料：**AST 去过 docstring 与注释的纯代码**，且**按平台切**。

    两个"为什么要这样"都是实测出来的：

    1. 不对 code 做 AST 处理时，`platforms/bilibili/listing.py` 模块 docstring 里那句
       「走哪条由 `capabilities.list_strategy` 与配置决定」就把 `bilibili.list_strategy`
       判成了"有人读" —— 而 `BilibiliAdapter._enumerate` 实际是按
       `external_browser_manifest_path is not None` 分支的，那个字段一行代码都没读。
       **散文不能当读取路径**，否则这个守卫会稳定地把最要命的那类谎言放过去。
    2. 语料必须按平台切：`retry_max` 在 B站 侧被读一次，不等于抖音那个也有人读 ——
       两个平台的 `advanced` 正是同名字段撞车的重灾区。
    """
    others = [name for name in PLATFORM_CONFIG_SCHEMAS if name != platform]
    skip_other = re.compile(f"platforms/({'|'.join(others)})/") if others else None
    chunks: list[str] = []
    for path in sorted(_SRC.rglob("*.py")):
        rel = path.relative_to(_SRC).as_posix()
        if _DEFINITION_FILES.search(rel):
            continue
        if skip_other is not None and skip_other.search(rel):
            continue
        chunks.append(_code_only(path.read_text(encoding="utf-8")))
    return "\n".join(chunks)


def _code_only(source: str) -> str:
    """去掉 docstring / 裸字符串语句 / 注释，只留语法等价的代码文本。"""
    tree = ast.parse(source)
    for node in ast.walk(tree):
        if (
            isinstance(node, ast.Expr)
            and isinstance(node.value, ast.Constant)
            and isinstance(node.value.value, str)
        ):
            node.value = ast.Constant(value=None)
    return ast.unparse(tree)


def _leaf_fields(model: type[BaseModel], prefix: str = "") -> dict[str, Any]:
    """展开到叶子字段（`advanced.retry_max` 也算一个字段），返回 path → FieldInfo。"""
    out: dict[str, Any] = {}
    for name, field in model.model_fields.items():
        key = f"{prefix}{name}"
        annotation = field.annotation
        if isinstance(annotation, type) and issubclass(annotation, BaseModel):
            out.update(_leaf_fields(annotation, prefix=f"{key}."))
            continue
        out[key] = field
    return out


def _marked(field: Any, key: str) -> bool:
    extra = field.json_schema_extra
    return isinstance(extra, dict) and extra.get(key) is True


def _hidden_by_self_or_parent(schema: type[BaseModel], path: str) -> bool:
    """字段自己或它的父段（`advanced`）标了 `ui:hidden` 都算隐藏。"""
    current: Any = schema
    for part in path.split("."):
        if not isinstance(current, type) or not issubclass(current, BaseModel):
            return False
        field = current.model_fields.get(part)
        if field is None:
            return False
        if _marked(field, "ui:hidden"):
            return True
        current = field.annotation
    return False


def _has_reader(platform: str, leaf: str) -> bool:
    # 判据粗放：`.leaf` 出现在该平台语料里就算"有人读"。宁可被同名局部变量糊过去，
    # 也不许出现"没人读还看得见"的字段 —— 单向的那一半棘轮可以误放，不可误红。
    return bool(re.search(rf"\.{re.escape(leaf)}\b", _reader_corpus(platform)))


def _each_field() -> list[tuple[str, str, Any, bool]]:
    return [
        (platform, path, field, _hidden_by_self_or_parent(schema, path))
        for platform, schema in sorted(PLATFORM_CONFIG_SCHEMAS.items())
        for path, field in _leaf_fields(schema).items()
    ]


def test_every_visible_field_has_a_reader() -> None:
    unread = [
        f"{platform}.{path}"
        for platform, path, _field, hidden in _each_field()
        if not hidden and not _has_reader(platform, path.rsplit(".", 1)[-1])
    ]
    assert not unread, (
        "这些字段前端渲染得出来、后端没有任何读取路径。要么实现它并加一条用例证明它"
        "改变了行为，要么加 json_schema_extra={'ui:hidden': True} 并在 description 里"
        "写明实际生效规则（判据见 ADR-0011/0012）：\n  " + "\n  ".join(unread)
    )


def test_a_field_with_a_reader_does_not_stay_hidden() -> None:
    """反向的那一半：字段被实现了却还留着 `ui:hidden`，Settings 页就永远看不见它。

    没有这条，隐藏会变成一个只进不出的抽屉 —— 而"哪些是纸面的"这个名单
    比现实悲观，比不写更糟（下一个人会去修一个已经不存在的坑）。
    """
    stale = [
        f"{platform}.{path}"
        for platform, path, _field, hidden in _each_field()
        if hidden and _has_reader(platform, path.rsplit(".", 1)[-1])
    ]
    assert not stale, (
        "这些字段标了 `ui:hidden`，但现在有代码读它们了 —— 去掉标记，"
        "并补一条用例证明它真的改变了行为：\n  " + "\n  ".join(stale)
    )


def test_hidden_fields_explain_themselves() -> None:
    """`ui:hidden` 只存在于 JSON Schema，而 schema 里能被人读到的只有 description。

    docstring 不算：Pydantic 只把 `Field(description=...)` 写进 schema，
    紧跟赋值的那句字符串对前端完全不存在 —— 所以这里查的确实是"契约里有没有这句话"。
    """
    missing = [
        f"{platform}.{path}"
        for platform, path, field, hidden in _each_field()
        if hidden and not any(m in (field.description or "") for m in _EXPLANATION_MARKERS)
    ]
    assert not missing, (
        "这些字段被隐藏了，但 schema 可见的 description 里没说清为什么它没有效果"
        "（下一个人仍然会照着它配）：\n  " + "\n  ".join(missing)
    )


def test_advanced_group_carries_the_collapse_marker() -> None:
    """`config-schema.md §4` 说前端按 `ui:advanced` 折叠高级字段 —— 那它就得真的在 schema 里。

    单独列一条（而不是塞进上面的叶子遍历）有两个原因：`advanced` 是**容器**，
    `_leaf_fields` 会钻进它、本身不成为一个叶子；而这里查的是"这一层的标记在不在"。
    顺带盯住一个陷阱：**子类重新标注会丢掉父类的标记** —— `advanced` 只在基类标没用，
    每个平台重新声明 `advanced: XxxAdvanced = Field(...)` 时 `json_schema_extra` 与
    `description` 都换成新的 FieldInfo（实测父类标的 `B.model_fields` 里是 `None`）。
    同理适用于 `ui:hidden` —— 标记必须打在**重新声明的那一处**。
    """
    missing = [
        platform
        for platform, schema in sorted(PLATFORM_CONFIG_SCHEMAS.items())
        if not _marked(schema.model_fields["advanced"], "ui:advanced")
    ]
    assert not missing, (
        "这些平台的 `advanced` 没有 ui:advanced 标记，前端没法按 §4 折叠它："
        "\n  " + "\n  ".join(missing)
    )
