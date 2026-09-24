"""T5.1 搬运区唯一的一条用例：搬进来的每个模块 import 得通，且 import 期不在磁盘上留东西。

只验这件事，是因为 ADR-0018 把 `ported/` 从 mypy / ruff / coverage 三道门里都排除了 ——
它的立论是"它在运行时不在场"。这句话有两个失败模式：

1. **import 不通** —— 那连"以后要用时先 import 一下"这条路都是假的，薄壳一接就炸；
2. **import 就有副作用** —— V1 那类平铺脚本最爱在模块顶层 `mkdir` / 写文件（更糟的是这里的
   `ROOT` 搬进 `src/` 之后指向包目录本身，顶层副作用会直接落在源码树里）。

清单不手写：`pkgutil.walk_packages` 自己扫。手写的那份会在有人加文件时静默漏掉，
而"漏掉"恰好是这条用例唯一能犯的错。

除此之外不写任何用例 —— 给未适配代码编"凑覆盖率"的测试正是 ADR-0018 与
AGENTS.md §1.3 要挡的形状。要验飞书管线真的能跑，是 T5.1 薄壳与
`tests/integration/test_feishu_sync.py` 的事。
"""

from __future__ import annotations

import importlib
import pkgutil
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

from intelligence_hub_v2 import ported

PORTED_DIR = Path(ported.__file__).resolve().parent


def _walk_error(name: str) -> None:
    """`walk_packages` 进不去某个子包时默认**静默跳过**，那会让下面的清单少一截。"""
    raise AssertionError(f"pkgutil 进不去 {name}：那份包的 __init__ 本身坏了")


def _ported_module_names() -> list[str]:
    return sorted(
        info.name
        for info in pkgutil.walk_packages(
            ported.__path__,
            prefix=f"{ported.__name__}.",
            onerror=_walk_error,
        )
    )


MODULE_NAMES = _ported_module_names()


def _disk_state() -> dict[str, set[str]]:
    """`cwd` 与 `ported/` 两处各列一份文件清单（`__pycache__` 不算：那是解释器的字节码缓存）。

    两处都要看是因为这条用例问的是"import 期有没有产生文件"，而搬运代码里的 `ROOT`
    指的是**包目录**（V1 里那是仓库根）：只盯 `cwd` 的话，一次顶层 `ROOT.mkdir()` 恰好
    看不见。
    """
    state: dict[str, set[str]] = {}
    for label, root in (("cwd", Path.cwd()), ("ported", PORTED_DIR)):
        state[label] = {
            path.relative_to(root).as_posix()
            for path in root.rglob("*")
            if "__pycache__" not in path.parts
        }
    return state


@pytest.fixture
def importing_cwd(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """在空目录里 import，并把 import 造成的 `sys.path` / `sys.modules` 改动挡回本条用例。

    不是测试卫生问题：`run_feishu_local_sync.py` 与两个 sync 脚本为了当脚本跑，模块顶层就有
    `sys.path.insert(0, ROOT)`（V1 形状，按计划原地保留）。不还原的话，`ported/feishu/`
    会在整个 pytest 会话里继续当一条隐式 import 路径用。

    `sys.modules` 只清 `ported` 子树：不清的话，先被当依赖 import 过的模块（比如
    `feishu_core`）在自己的那条用例里只是查缓存，"import 期没有副作用"就变成空断言。
    """
    monkeypatch.chdir(tmp_path)
    path_before = list(sys.path)
    modules_before = set(sys.modules)
    ported_prefix = f"{ported.__name__}."
    try:
        yield tmp_path
    finally:
        sys.path[:] = path_before
        for name in set(sys.modules) - modules_before:
            if name.startswith(ported_prefix):
                del sys.modules[name]


def test_the_port_area_has_modules_so_the_parametrise_is_not_a_no_op() -> None:
    """和 `test_ported_boundary.py` 同一条顾虑：空清单 = 下面那条一条都不跑 = 零断言。

    13 = 11 份搬进来的 V1 文件 + 2 个子包的 `__init__`。下限而不是等号：加文件不该红。
    """
    assert len(MODULE_NAMES) >= 13, MODULE_NAMES


@pytest.mark.parametrize("module_name", MODULE_NAMES)
def test_every_ported_module_imports_without_touching_the_disk(
    module_name: str,
    importing_cwd: Path,
) -> None:
    before = _disk_state()

    module = importlib.import_module(module_name)

    assert module.__name__ == module_name, f"{module_name} import 出来的不是它自己"
    assert _disk_state() == before, f"import {module_name} 在磁盘上留下了文件"
    assert list(importing_cwd.rglob("*")) == [], f"import {module_name} 在 cwd 里产生了东西"
