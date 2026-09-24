"""`ported/` 的边界看护（ADR-0018 决定 1、4）。

未适配搬运区之所以可以不受 mypy / coverage / ruff 检查，全部押在一句话上：
**它在运行时不在场**。这句话一旦被破坏（有人从主流程 import 它），豁免就从
"记了数额的债"变成"藏起来的洞" —— 而那正是本仓库反复付学费的形状。

所以这条不许靠约定：把整棵树扫一遍，谁 import 了 `ported` 就得在下面的清单里，
清单里的人却 import 了 → 也红（防的是"壳搬走了、豁免还挂着"）。

顺带钉两件同一条链上的事：搬运文件第一行必须是 `# ruff: noqa`（lint 豁免写在文件里
而不是 pyproject 里，见 `ported/__init__.py`），且必须带 `# TODO(v2-adapt)`。
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
SRC = REPO_ROOT / "src" / "intelligence_hub_v2"
PORTED = SRC / "ported"
PORTED_PACKAGE = "intelligence_hub_v2.ported"

#: 允许 import 搬运区的薄壳（相对仓库根）。它们自己受全套门禁，职责只是"转交一次调用"。
#: 新增一条 = 在 ADR-0018 里说明为什么这次转交不能等到适配之后。
#:
#: 今天为空：**`ported/` 里还没有文件，也就还没有人需要转交**。T5.1（飞书）与 T5.4（报告）
#: 落薄壳时把它们加进来 —— 由反向那条用例逼着加，忘了加就红，不会静默放过。
SHELLS_ALLOWED_TO_IMPORT_PORTED: frozenset[str] = frozenset()


def _py_files(root: Path) -> list[Path]:
    return sorted(p for p in root.rglob("*.py") if "__pycache__" not in p.parts)


def _imports_ported(path: Path) -> bool:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            mod = node.module or ""
            if mod == PORTED_PACKAGE or mod.startswith(f"{PORTED_PACKAGE}."):
                return True
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name == PORTED_PACKAGE or alias.name.startswith(f"{PORTED_PACKAGE}."):
                    return True
    return False


def _rel(path: Path) -> str:
    return path.relative_to(REPO_ROOT).as_posix()


def test_the_ported_package_exists_so_this_guard_is_not_a_no_op() -> None:
    """目录不在 = 下面两条用例都在空集合上成立 = 零断言。先把它钉住。"""
    assert (PORTED / "__init__.py").is_file()


def test_only_the_declared_shells_reach_into_the_port_area() -> None:
    """`ported/` 之外，只有清单里那几个薄壳可以 import 它。"""
    offenders: list[str] = []
    for path in [*_py_files(SRC), *_py_files(REPO_ROOT / "tools")]:
        if PORTED in path.parents:
            continue
        if _imports_ported(path) and _rel(path) not in SHELLS_ALLOWED_TO_IMPORT_PORTED:
            offenders.append(_rel(path))
    assert offenders == [], f"主流程 import 了未适配搬运区：{offenders}"


def test_every_allowed_shell_still_reaches_in() -> None:
    """反向：豁免清单上的人必须真的还在用。壳被删了而豁免还挂着，
    下一个想开壳的人就会拿它当先例（"看，已经有两个了"）。"""
    existing = {_rel(p) for p in [*(SRC.rglob("*.py")), *(REPO_ROOT / "tools").rglob("*.py")]}
    for rel in sorted(SHELLS_ALLOWED_TO_IMPORT_PORTED):
        if rel not in existing:
            pytest.fail(f"{rel} 不在了，但它还在 `ported/` 的豁免清单里 —— 把那条删掉")


def test_each_ported_file_declares_its_own_two_exemptions() -> None:
    """**前两行之内**要有 `# ruff: noqa`（lint 豁免在文件里，不在 pyproject 里），
    外加一处 `TODO(v2-adapt)`。为什么不是"钉死第 1 行"：V1 那三个脚本的首行是 shebang，
    把它挪走就等于搬掉了一个入口（`./sync_feishu_base_to_local.py` 直接跑不了），
    而"原样搬运"里就包括"还能照着老办法跑"。

    这条不是在管风格：`# ruff: noqa` 是**唯一**让 lint 放过它的机制，写歪（比如写成
    `#noqa`、或把它塞进 docstring 里）就会让这份文件在下一个人的提交里当场红 ——
    而红在一个没人调用、没人打算近期修的脚本上，只会教出"这个门禁是噪音"这个结论。
    另一个方向也真实咬过一次：豁免句里照抄 `# ruff: noqa` 这几个字符，ruff 会把注释
    里那句也当成指令并抱怨它格式不对，所以这句现在写成散文。
    """
    offenders: list[str] = []
    for path in _py_files(PORTED):
        if path.name == "__init__.py":
            continue
        head = path.read_text(encoding="utf-8").splitlines()[:12]
        if not any(line.strip() == "# ruff: noqa" for line in head[:2]):
            offenders.append(f"{_rel(path)}: 前两行里没有整行的 `# ruff: noqa`")
        elif not any("TODO(v2-adapt)" in line for line in head):
            offenders.append(f"{_rel(path)}: 前 12 行里没有 `TODO(v2-adapt)`")
    assert offenders == [], "\n".join(offenders)
