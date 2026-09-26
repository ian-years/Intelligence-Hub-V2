"""`ROADMAP.md` 的自洽看护：勾了的判据，正文不许再说"这一格没勾"。

来历（2026-09-26 对账，`docs/progress/2026-09-26.md` §2）：`ROADMAP.md` 挂着 20 条 `[ ]`，
逐条对代码与 live 库核完发现 9 组早就做完了；同一轮还发现"迁移脚本"那一格
**勾选框是 `[x]`，而同一格正文最后一句写"整条不勾"** —— 两者分属两次编辑，
markdown 不会为"这一格自洽"报错，所以它一直躺在那儿。

方向性也要记一句：这批漂移**全是往悲观方向错的**（该勾没勾）。乐观错会有人撞见
（看板绿了而活没干），悲观错谁也不撞 —— 少勾一格不影响任何人跑命令，所以它只会单向积累。
本仓库另一处看护（`test_contract_guard_index.py`）挡的是反方向（"文档说有名看护而实际没有"），
两边都不挡就成了漏勺。
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]

#: 一格判据：`- [ ]` / `- [x]` 开头，到下一个 bullet、下一个标题、分隔线**或文件结尾**为止。
#: 那个 `\Z` 不是凑数：少了它，文件里最后一格永远扫不到 —— 防空转那条用例就是这么抓出来的。
_ITEM = re.compile(r"^- \[( |x)\] (.+?)$(.*?)(?=^- \[|^#{2,3} |^---$|\Z)", re.MULTILINE | re.DOTALL)

#: 说"这一格没做完/没勾"的措辞。刻意不含"未验/没验"：
#: 那两类是**合法的**残留说明（本仓库的规矩是勾了也要写下还差哪一跑），
#: 而"不勾/未落地"与一个已勾的框同时出现就是自相矛盾。
_NOT_CHECKED_WORDS = ("整条不勾", "这条不勾", "这一条不勾", "没勾上", "未落地")


def _strip_quoted(text: str) -> str:
    """去掉引号里的内容与行内代码 —— 它们是在**转述**别人的话，不是在陈述本页的判断。

    不加这一步会误伤：对账时我在"迁移脚本"那一格里引用了原来那句错话（"整条不勾……"）
    用来记录"这里曾经说谎"，引用本身不该被这条看护判红。
    """
    patterns = (r"`[^`]*`", r"“[^”]*”", r"「[^」]*」", r'"[^"\n]*"')
    for pattern in patterns:
        text = re.sub(pattern, "", text)
    return text


def contradicting_items(roadmap_text: str) -> list[str]:
    """返回"勾了但正文说不勾"的那些判据标题（正常应为空）。"""
    found: list[str] = []
    for marked, title, body in _ITEM.findall(roadmap_text):
        if marked != "x":
            continue
        plain = _strip_quoted(body)
        if any(word in plain for word in _NOT_CHECKED_WORDS):
            found.append(title.strip()[:60])
    return found


def test_shipped_roadmap_has_no_self_contradicting_criterion() -> None:
    text = (REPO_ROOT / "ROADMAP.md").read_text(encoding="utf-8")
    bad = contradicting_items(text)
    assert not bad, f"这些判据勾着 [x] 而正文说它没勾/未落地：{bad}"
    # 防空转：这页确实解析得出东西来（0 格的话上一条断言是恒真的）
    total = len(_ITEM.findall(text))
    checked = sum(1 for marked, _t, _b in _ITEM.findall(text) if marked == "x")
    assert total > 30 and checked > 10, f"解析到的判据格数不像话：total={total} checked={checked}"


def test_the_rule_actually_fires_on_a_contradiction() -> None:
    """**这条是上一条的防空转前置**：造一个真的自相矛盾，看护必须抓到它。

    没有这一条，`contradicting_items` 只要写坏（正则不匹配、引号剥太多）就会让上一条
    永久地"零违规"通过 —— 而那正是本仓库反复踩的形状：一个会说谎的看护比没有看护更糟。
    """
    sample = "\n".join(
        [
            "### 里程碑",
            "- [x] **甲**：做完了",
            "  —— 整条不勾，因为真机还没跑。",
            "- [ ] **乙**：没做",
            "  —— 整条不勾。",
            "- [x] **丙**：做完了",
            "  —— 原来这里写过“整条不勾”那句话，是错的（弯引号转述，不该判红）。",
            "- [x] **丁**：做完了",
            '  —— 这一段原来还写着"整条不勾：某某前提"那句话，是错的（直引号转述，同样不该判红）。',
            "- [x] **戊**：做完了",
            "  —— 这一格仍未落地，等真机那一跑。",
        ]
    )
    assert contradicting_items(sample) == ["**甲**：做完了", "**戊**：做完了"], (
        "规则没抓到该抓的（甲/戊），或误伤了对转述的引用（丙/丁）"
    )
