"""导入顺序看护：**从任何一个入口先进来**都得是绿的。

起因是 Task 6 接适配器时撞的一个循环导入，它**只在某个特定导入顺序下出现**：
`platforms/__init__.py` 末尾要在导入期把适配器装上（注册表要"实现了哪些平台"
在导入时就固定），而链条是

    infra.cookies → infra.ytdlp → platforms.base → platforms/__init__
      → douyin.adapter → infra.ytdlp（还没执行完）→ ImportError

只要测试或应用**先**碰 `infra.*` 就炸，先碰 `platforms.*` 就完全没事 ——
所以单跑一个文件绿、整套跑红，或者反过来。这类"取决于谁先被 import"的坑
只有把每个入口都单独起一个进程走一遍才守得住（同进程内模块只会加载一次）。

解法在 `infra/ytdlp.py` 那侧：`CookieVariant` 只出现在注解里，
挪进 `TYPE_CHECKING` 之后 infra 就不再运行期反引 platforms 了
（依赖图 `platforms/ → infra/` 那条边由此恢复成单向）。
"""

from __future__ import annotations

import subprocess
import sys

import pytest

# 每个都是一个"独立进程里的第一个 import"。挑的是依赖图的四条边各一端，
# 不是全部模块 —— 全列的话这条看护要跑一分钟，而它守的只是"谁先入门"。
ENTRY_POINTS = [
    "intelligence_hub_v2.infra",
    "intelligence_hub_v2.infra.cookies",  # Task 6 那天就是它先被导入时炸的
    "intelligence_hub_v2.platforms",
    "intelligence_hub_v2.platforms.registry",
    "intelligence_hub_v2.storage.db",
    "intelligence_hub_v2.core.event_bus",
]

ADAPTERS_ARE_REGISTERED_AT_IMPORT = """
from intelligence_hub_v2.platforms.registry import PLATFORMS
import intelligence_hub_v2.platforms  # noqa: F401  确保包被完整执行
assert "douyin" in PLATFORMS, sorted(PLATFORMS)
"""


@pytest.mark.parametrize("module", ENTRY_POINTS)
def test_importing_it_first_does_not_hit_a_cycle(module: str) -> None:
    result = subprocess.run(  # noqa: S603 - argv 是本文件里写死的常量，没有外部输入
        [sys.executable, "-X", "utf8", "-c", f"import {module}"],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=120,
        check=False,
    )
    assert result.returncode == 0, f"import {module} 失败：\n{result.stderr[-800:]}"


@pytest.mark.parametrize(
    "entry",
    ["", "import intelligence_hub_v2.infra.cookies  # 先从 infra 进门（Task 6 炸的就是这条序）\n"],
    ids=["platforms-first", "infra-first"],
)
def test_the_douyin_adapter_is_registered_whichever_way_you_arrive(entry: str) -> None:
    """注册不能"看运气"：先进哪个模块，`PLATFORMS` 里都得有抖音。

    `infra-first` 那一份是本地复现当时那个 `ImportError` 的最小形状。
    """
    result = subprocess.run(  # noqa: S603
        [sys.executable, "-X", "utf8", "-c", entry + ADAPTERS_ARE_REGISTERED_AT_IMPORT],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=120,
        check=False,
    )
    assert result.returncode == 0, result.stderr[-800:]
