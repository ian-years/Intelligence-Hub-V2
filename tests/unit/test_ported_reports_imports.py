"""T5.4 搬运区唯一的一条用例：`ported/reports/` 的每个模块 import 得通，且 import 期不落文件。

形状照 `tests/unit/test_ported_feishu_imports.py`（T5.1 的先例），理由也一样：
ADR-0018 把 `ported/` 从 mypy / ruff / coverage 三道门里都排除了，立论是"它在运行时不在场"。
这句话有两个失败模式，本文件各钉一条：

1. **import 不通** —— 那连"以后要用时先 import 一下"都是假的，薄壳一接就炸。
   T5.4 这里特别脆：三份脚本原本靠 `sys.path.insert(ROOT)` + `from utils import …` 找 V1 地面，
   搬进包里之后那条路指向的是 `src/…/ported/reports/`，改错一处就是 ImportError。
2. **import 就有副作用** —— V1 那类平铺脚本爱在顶层 `mkdir` / 写文件；更糟的是这里的 `ROOT`
   现在指**包目录**，一次顶层写盘就直接落在源码树里（`DEFAULT_OUTPUT_ROOT` 那几行就在那儿算）。

模块清单从目录里枚举（`pkgutil.walk_packages`），不手写：手写的那份会在有人加文件时静默漏掉，
而"漏掉"恰好是这条用例唯一能犯的错。`onerror` 必须给，否则 `walk_packages` 进不去子包时
**默认静默跳过**，清单少一截也没人知道。

除此之外不给搬运代码写任何用例（`ported/` 已从覆盖率里排除，测它是浪费）。
真要验报告能不能生成，是 `tools/render_reports.py` 与 `tests/tools/test_render_reports.py` 的事。
"""

from __future__ import annotations

import importlib
import pkgutil
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest

from intelligence_hub_v2.ported import reports


def _walk_error(name: str) -> None:
    raise AssertionError(f"pkgutil 进不去 {name}：那份包的 __init__ 本身坏了")


def _report_module_names() -> list[str]:
    return sorted(
        info.name
        for info in pkgutil.walk_packages(
            reports.__path__,
            prefix=f"{reports.__name__}.",
            onerror=_walk_error,
        )
    )


MODULE_NAMES = _report_module_names()
PACKAGE_DIR = Path(reports.__file__).resolve().parent


def _disk_state() -> dict[str, set[str]]:
    """`cwd`、`ported/reports/` 与仓库 `src/` 三处各列一份清单。

    三处都要看：这条用例问的是"import 期有没有产生文件"，而搬运代码里的 `ROOT` 就是
    `ported/reports/`，V1 那些 `ROOT / "outputs"` 的默认值一旦有人在顶层摸了盘，脏的就是源码树。
    `__pycache__` 不算 —— 那是解释器的字节码缓存，不是脚本的产物。
    """
    src_root = PACKAGE_DIR.parents[2]  # .../src/intelligence_hub_v2/ported/reports -> .../src
    state: dict[str, set[str]] = {}
    for label, root in (("cwd", Path.cwd()), ("reports", PACKAGE_DIR), ("src", src_root)):
        state[label] = {
            path.relative_to(root).as_posix()
            for path in root.rglob("*")
            if "__pycache__" not in path.parts
        }
    return state


@pytest.fixture
def importing_cwd(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """在空目录里 import，并把 import 造成的 `sys.path` / `sys.modules` 改动挡回本条用例。

    不是测试卫生问题：V1 的 `prepare_creator_analysis.py` 与 `render_creator_analysis_report.py`
    为了当脚本跑，模块顶层就有 `sys.path.insert(0, ROOT)`（按计划原地保留）。不还原的话，
    `ported/reports/` 会在整个 pytest 会话里继续当一条隐式 import 路径用，把同名模块吸过去。

    `sys.modules` 只清 `ported` 子树：不清的话，先被当依赖 import 过的模块（比如 `utils`）
    在自己的那条用例里只是查缓存，"import 期没有副作用"就变成空断言。
    """
    monkeypatch.chdir(tmp_path)
    path_before = list(sys.path)
    modules_before = set(sys.modules)
    try:
        yield tmp_path
    finally:
        sys.path[:] = path_before
        for name in set(sys.modules) - modules_before:
            if name.startswith("intelligence_hub_v2.ported"):
                del sys.modules[name]


def test_the_reports_port_area_has_modules_so_the_parametrise_is_not_a_no_op() -> None:
    """空清单 = 下面那条一条都不跑 = 零断言。先把目录钉住。

    3 = 搬进来的三份 V1 脚本。`walk_packages` 从本包的 `__path__` 起走，**不含本包的
    `__init__`**（它是容器，`test_ported_feishu_imports.py` 数到的那两个"子包 `__init__`"
    是 `feishu/` 与 `v1_shared/`，同理不在这里）。下限而不是等号：加文件不该红。
    """
    assert MODULE_NAMES, "ported/reports/ 里一个模块都没枚举到"
    assert len(MODULE_NAMES) >= 3, MODULE_NAMES
    assert {Path(name.rsplit(".", 1)[-1]).name for name in MODULE_NAMES} >= {
        "prepare_creator_analysis",
        "render_creator_analysis_report",
        "generate_creator_insight_dashboard",
    }, MODULE_NAMES


@pytest.mark.parametrize("module_name", MODULE_NAMES)
def test_every_reported_module_imports_without_touching_the_disk(
    module_name: str,
    importing_cwd: Path,
) -> None:
    before = _disk_state()

    module = importlib.import_module(module_name)

    assert module.__name__ == module_name, f"{module_name} import 出来的不是它自己"
    assert _disk_state() == before, f"import {module_name} 在磁盘上留下了文件"
    assert list(importing_cwd.rglob("*")) == [], f"import {module_name} 在 cwd 里产生了东西"
