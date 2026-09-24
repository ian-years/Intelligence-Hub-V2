"""文本 I/O 必须写死 `encoding`：一台 GBK 机器上"绿"不等于换环境也绿。

起因（2026-09-24，T6.1 收尾）：两条 postprocess 用例在 `PYTHONUTF8=1` 的那一跑里绿，
在默认（`locale.getpreferredencoding() == 'cp936'`）的那一跑里红，红在
`clean.read_text()` —— 读的是中文口播稿。`AGENTS.md` §4 早就写了"这台机器上 Python
命令都要 `-X utf8`"，所以**只要有人习惯性带上那个开关，这类用例就能长期假装通过**，
而它守的其实是"读的时候按写的编码读"。这与 T1.3 里删掉的那条"看本机装没装权重"的用例
同属一类：结果取决于环境、而测试没把环境钉住。

为什么不靠 ruff：`PLW1514 unspecified-encoding` 正是这条规矩，但它在 preview 里，
要开只能全局 `--preview`（一次引入一批未稳定的规则）。所以用几十行 AST 扫全仓。
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import Final

REPO_ROOT = Path(__file__).resolve().parents[2]
SCAN_DIRS: Final = ("src", "tools", "tests")

# 文本读写：`Path.read_text` / `Path.write_text` / `Path.read_lines` 与内建 `open`。
TEXT_METHODS: Final = frozenset({"read_text", "write_text", "read_lines"})
INTERESTING: Final = TEXT_METHODS | {"open"}


class _Visitor(ast.NodeVisitor):
    def __init__(self, path: Path) -> None:
        self.path = path
        self.hits: list[str] = []

    def visit_Call(self, node: ast.Call) -> None:  # ast 约定的方法名，不是本仓库的命名风格
        name = _called_name(node)
        if name is not None and _needs_encoding(node, name):
            self.hits.append(f"{self.path}:{node.lineno}  {name}(...) 没写 encoding=")
        self.generic_visit(node)


def _called_name(node: ast.Call) -> str | None:
    """这条调用是不是"读写文本"那一个形状。不是就返回 None。

    接收者是变量也要抓：真红掉的那条就是 `clean.read_text()`，`clean` 是个局部变量 ——
    按"只认路径表达式"来扫正好放过它。唯一放行的是 `wave.open(path, "rb")` 这类
    **模块函数**（接收者是 Name 且方法名叫 `open`）：它的第一个实参是文件名，不是 mode。
    """
    func = node.func
    if isinstance(func, ast.Name):
        return func.id if func.id in INTERESTING else None
    if isinstance(func, ast.Attribute) and func.attr in INTERESTING:
        if func.attr == "open" and isinstance(func.value, ast.Name):
            return None
        return func.attr
    return None


def _needs_encoding(node: ast.Call, name: str) -> bool:
    """`encoding=` 给了就不是问题；`open(..., "rb")` 是二进制，本来就不需要编码。"""
    if any(_pins_encoding(kw) for kw in node.keywords):
        return False
    return name != "open" or not _is_binary(node)


def _pins_encoding(kw: ast.keyword) -> bool:
    """写了 `encoding=` 但写成 `None`，等于没写。"""
    if kw.arg != "encoding":
        return False
    return not (isinstance(kw.value, ast.Constant) and kw.value.value is None)


def _is_binary(node: ast.Call) -> bool:
    mode = next((kw.value for kw in node.keywords if kw.arg == "mode"), None)
    if mode is None and len(node.args) >= 2:
        mode = node.args[1]
    return isinstance(mode, ast.Constant) and isinstance(mode.value, str) and "b" in mode.value


def _hits_of(source: str, path: Path) -> list[str]:
    visitor = _Visitor(path)
    visitor.visit(ast.parse(source))
    return visitor.hits


def _scan(root: Path) -> tuple[list[str], int]:
    hits: list[str] = []
    count = 0
    for file in sorted(root.rglob("*.py")):
        if "__pycache__" in file.parts:
            continue
        count += 1
        source = file.read_text(encoding="utf-8")
        hits.extend(_hits_of(source, file.relative_to(REPO_ROOT)))
    return hits, count


def test_every_text_read_or_write_pins_its_encoding() -> None:
    offenders: list[str] = []
    scanned = 0
    for name in SCAN_DIRS:
        hits, count = _scan(REPO_ROOT / name)
        # 先钉"这个目录真的扫到了文件"：目录名写错时下面那条断言会永远绿（防空转）。
        assert count > 0, f"{name}/ 一个 .py 都没扫到，看护空转了（REPO_ROOT={REPO_ROOT}）"
        offenders += hits
        scanned += count
    assert scanned > 100, f"只扫到 {scanned} 个文件，不像全仓（REPO_ROOT={REPO_ROOT}）"
    assert not offenders, (
        "读写文本要显式 encoding（本机默认 cp936，见本文件 docstring）：\n" + "\n".join(offenders)
    )


def test_the_guard_itself_catches_a_bare_read_text() -> None:
    """防空转：违规样本必须被报出来，且只报违规那两条。

    前两条样本的形状就是本次真红掉的那个 —— 接收者是个变量（`clean.read_text()`），
    不是路径表达式；`wave.open(..., "rb")` 与给了 encoding 的那些不该出现。
    """
    source = "\n".join(
        [
            "from pathlib import Path",
            "import wave",
            "clean = Path('a.txt')",
            "clean.read_text()",
            "Path('b.txt').read_text(encoding='utf-8')",
            "wave.open('c.wav', 'rb')",
            "open('d.txt', 'rb')",
            "open('e.txt')",
            "",
        ]
    )
    assert _hits_of(source, Path("sample.py")) == [
        "sample.py:4  read_text(...) 没写 encoding=",
        "sample.py:8  open(...) 没写 encoding=",
    ]
