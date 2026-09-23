"""V1 §7 陷阱 → 具体看护用例的**索引漂移看护**（`docs/specs/contract-tests.md §3`）。

这张映射表是**文档**；文档会说谎 —— 一个用例被改名或删掉，`docs/specs` 里那一行还在，
读文档的人以为有看护、其实没有。

两件事分开钉：

1. **`GUARD_INDEX` 的每一条都要"存在、会被收集、没被 skip、真的在断言"**。键是
   `(文件, 用例名)` 对而不是裸名字：`test_update_fields_does_not_clobber_other_fields`
   在 `test_creators_repo.py` 与 `test_videos_repo.py` 里**同名存在**，按裸名字匹配时
   删掉 videos 那一份（§7.4 真正指的那条）索引照样绿。判据走 AST 而不是"本次运行收集到的
   nodeid"：后者单跑本文件会假红，而**一个会说谎的看护比没有看护更糟**。
2. **§7.1–§7.25 每条都要有归属**：契约看护 / 结构性消除 / 明确没看护，三选一。
   只查"表里的名字还在"永远看不见自己的盲区 —— §7.18 与 §7.19 就长期在两份文档里都没家。

**还抓不到的**：把断言换成"逻辑上恒真但写得像真断言"的空壳（AST 只能挡 `pass`
和字面量 `assert True`）。那要靠变异验证：改坏实现、看它会不会红。本轮两条
（手搓模板那条、`since` 那条）就是这么确认的。
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

from tests.contracts.test_platform_adapter import (
    PlatformAdapterContractTests,
    TestBilibiliContract,
    TestDouyinContract,
)

_TESTS_ROOT = Path(__file__).resolve().parent.parent

_CONTRACTS = "tests/contracts"
_UNIT_STORAGE = "tests/unit/storage"
_DOUYIN = f"{_CONTRACTS}/test_douyin_adapter.py"
_BILI = f"{_CONTRACTS}/test_bilibili_adapter.py"
_BILI_HELPERS = f"{_CONTRACTS}/test_bilibili_helpers.py"
_VIDEOS_REPO = f"{_UNIT_STORAGE}/test_videos_repo.py"
_CREATORS_REPO = f"{_UNIT_STORAGE}/test_creators_repo.py"
_PLATFORMS_REPO = f"{_UNIT_STORAGE}/test_platforms_repo.py"
_FILES = f"{_UNIT_STORAGE}/test_files.py"

#: 每条 V2.0 契约看护的代表用例，`(文件, 用例名)`。**文件限定**是重点（见模块 docstring）。
#: 一条陷阱可以点多个文件 —— §7.4 同时护 creators 与 videos。
GUARD_INDEX: dict[str, tuple[tuple[str, str], ...]] = {
    "§7.1 sec_uid 不是 URL": ((_DOUYIN, "test_share_link_follows_302_to_sec_uid"),),
    "§7.2 兜底留 yt-dlp 原文": (
        (_DOUYIN, "test_fallback_marks_the_source_and_keeps_the_yt_dlp_original_text"),
    ),
    "§7.3 cookie 顺序真源": (
        (_DOUYIN, "test_order_comes_from_capabilities_and_starts_with_the_exported_file"),
    ),
    "§7.4 update_fields 字段级": (
        (_VIDEOS_REPO, "test_update_fields_does_not_clobber_other_fields"),
        (_CREATORS_REPO, "test_update_fields_does_not_clobber_other_fields"),
    ),
    "§7.5 转写目录全平台统一": ((_FILES, "test_transcript_path_is_identical_across_platforms"),),
    "§7.8 safe_filename": (
        ("tests/unit/test_safe_filename.py", "test_result_is_always_a_single_path_segment"),
    ),
    "§7.13 清单红并点名生产者": (
        (_BILI, "test_an_unreadable_manifest_raises_naming_the_producer"),
    ),
    "§7.14 缺生产者红不是 skip": (
        (_BILI, "test_a_missing_manifest_file_is_reported_with_its_path"),
    ),
    "§7.15 两条路都带 cookie": (
        (_BILI, "test_the_enumeration_carries_the_same_cookie_rungs_as_download"),
    ),
    "§7.16 兜底未实现如实红": (
        (_BILI, "test_search_fallback_is_refused_rather_than_silently_skipped"),
    ),
    "§7.20 桥 503=浏览器没了": (
        ("tests/unit/infra/test_cdp_bridge.py", "test_503_means_browser_dead_not_bridge_down"),
    ),
    "§7.20 测不到≠正常": ((_PLATFORMS_REPO, "test_is_healthy_only_trusts_an_explicit_ok"),),
    "§7.21 DASH 认音频轨": (
        (_BILI_HELPERS, "test_a_pair_artifact_points_the_transcriber_at_the_audio_track"),
    ),
    "§7.24 跟踪必须真布尔": ((_CREATORS_REPO, "test_set_tracking_rejects_non_bool"),),
    "§7.25 墓碑一列三字段": ((_VIDEOS_REPO, "test_hide_removes_from_list_but_get_still_works"),),
    "§2 契约二 清单强制终态": (
        (
            "tests/integration/test_manifest_finalization.py",
            "test_manifest_finalizes_on_every_exit_path",
        ),
    ),
}

#: 现在**没有看护**的几条，逐条写清为什么。列出来是为了不把它们当成"已经守住了" ——
#: 漏掉和漏实现看起来一样，所以它们也必须进 §7 编号的完整覆盖检查。
NOT_YET_GUARDED: tuple[str, ...] = (
    "§7.9 SenseVoice 标点注入（ASR 在 V2.1）",
    "§7.18 桥 profile 登录态跨会话持久（`cdp_bridge_server.py` 还没移植进 V2）",
    "§7.19 注册表 PATH 合并（`prepare_runtime_environment()` 从未实现，今天只有"
    " preflight 的 `shutil.which`；`contract-tests.md §3` 那一行点名的用例也不存在）",
    "§7.22 按位抓取不退化全库扫描（V2.1）",
)

#: 与 `AGENTS.md §5` 的"结构性消除"那一栏一一对应（V2 的设计让它不可能再发生）。
STRUCTURALLY_ELIMINATED: tuple[str, ...] = (
    "§7.4 整行覆盖",
    "§7.6 LocalCreatorStore 参数",
    "§7.7 双源",
    "§7.10 路由靠记忆",
    "§7.11 三种命名",
    "§7.12 预检主库错位",
    "§7.17 head 接常驻服务",
    "§7.23 用时抖动门禁",
    "§7.25 墓碑散落",
)

_TRAP_NUMBER = re.compile(r"§7\.(\d+)")


def _numbers(labels: tuple[str, ...]) -> set[int]:
    return {int(match) for match in _TRAP_NUMBER.findall(" ".join(labels))}


_SKIP_MARKERS = ("skip", "skipif", "xfail")


def _find_guard(
    tree: ast.Module, name: str
) -> tuple[ast.FunctionDef | ast.AsyncFunctionDef, list[str]]:
    """按名字找函数，连带返回它外层的类名（用来判"pytest 会不会收集它"）。

    只扫"模块层 + 一层类内"两种位置 —— 仓库里的用例就长这样，写得更深会当场
    LookupError 响，而不是静默漏判。
    """
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return node, []
    for node in tree.body:
        if isinstance(node, ast.ClassDef):
            for inner in node.body:
                if (
                    isinstance(inner, (ast.FunctionDef, ast.AsyncFunctionDef))
                    and inner.name == name
                ):
                    return inner, [node.name]
    raise LookupError(name)


def _guard_problem(file: str, name: str) -> str | None:
    """这个看护用例现在**会不会真的跑并断言点东西**。返回问题描述，None 表示健康。

    用 AST 而不是"读本次运行收集到的 nodeid"：后者只在整仓一起跑时成立，
    单跑本文件会假红 —— 一个会说谎的看护比没有看护更糟。
    AST 能查的四件事都对应一种真实的"名字还在但它已经不跑了"：
    函数没了 / 外层类不叫 `Test*` / 被 skip 掉了（§7.14 的老病）/ 断言被掏空成恒真。
    """
    path = _TESTS_ROOT.parent / file
    if not path.is_file():
        return f"{file} 不存在"
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    try:
        func, classes = _find_guard(tree, name)
    except LookupError:
        return f"{file} 里没有 {name}（改名或删掉了）"

    for cls in classes:
        if not cls.startswith("Test"):
            return f"{file}::{name} 外层的 {cls} 不以 Test 开头，pytest 不会收集"

    decorator_text = ast.dump(ast.Tuple(elts=list(func.decorator_list), ctx=ast.Load()))
    if any(marker in decorator_text for marker in _SKIP_MARKERS):
        return f"{file}::{name} 带 skip/skipif/xfail 标记 —— 跳过的看护不是看护（§7.14）"

    real_assert = any(
        isinstance(node, ast.Assert)
        and not (isinstance(node.test, ast.Constant) and node.test.value is True)
        for node in ast.walk(func)
    )
    raises = "raises" in ast.dump(ast.Tuple(elts=list(func.body), ctx=ast.Load()))
    if not (real_assert or raises):
        return f"{file}::{name} 里没有任何非恒真断言 —— 空壳看护"
    return None


def test_every_mapped_guard_will_actually_run() -> None:
    """索引里每条都要"存在 + 会被收集 + 没被 skip + 真的在断言"。"""
    problems = [
        f"  {trap}  →  {file}::{name}：{problem}"
        for trap, guards in GUARD_INDEX.items()
        for file, name in guards
        if (problem := _guard_problem(file, name)) is not None
    ]
    assert not problems, "这些 §7 看护已经不算守卫了：\n" + "\n".join(problems)


def test_every_v1_pitfall_has_a_home() -> None:
    """§7.1–§7.25 每条都落在三桶之一：契约看护 / 结构性消除 / 明确未落地。

    旧写法是 `len(GUARD_INDEX) >= 16` —— 加一条垃圾映射就永久满足，
    而且完全看不见"某条陷阱根本没有家"（§7.18 就是这样长期没人认领）。
    """
    for trap in GUARD_INDEX:
        assert _TRAP_NUMBER.search(trap) or trap.startswith("§2"), f"{trap} 不是 §7.NN 形式"
    declared = (
        _numbers(tuple(GUARD_INDEX)) | _numbers(STRUCTURALLY_ELIMINATED) | _numbers(NOT_YET_GUARDED)
    )
    all_pitfalls = set(range(1, 26))
    assert declared == all_pitfalls, (
        f"没有归属的陷阱编号：{sorted(all_pitfalls - declared)}；"
        f"引用了不存在的编号：{sorted(declared - all_pitfalls)}"
    )


_AGENTS_COLUMN_FOR = {
    "结构性消除": "structural",
    "契约测试看护": "guarded",
    "说得出名字但今天没看护": "unguarded",
}


def test_agents_md_triage_matches_this_file() -> None:
    """`AGENTS.md §5` 的三栏必须与这里的三个集合对得上，且互斥、合起来正好 25 条。

    之前那一节写"15 条契约看护 + 9 条结构性消除 = 25 条"，实际只数到 24，
    §7.18 在两份文档里都没有家；`docs/lessons.md` 第一部分还把 §7.1/§7.5 放进了
    另一桶。入口文档说"这条有看护"而实际没有，是本仓库踩过两次的形状。

    §7.4 与 §7.25 既在"结构性消除"里也有看护用例 —— 但 AGENTS 的"契约测试看护"那一栏
    只列**没被结构性消除**的那些，所以这里按 `GUARD_INDEX − 结构性消除` 对齐。
    """
    agents = (_TESTS_ROOT.parent / "AGENTS.md").read_text(encoding="utf-8")
    section = agents.split("## 5. 已知陷阱", 1)[1].split("## 6.", 1)[0]

    columns: dict[str, set[int]] = {}
    for line in section.splitlines():
        if not line.startswith("- **"):
            continue
        for title, key in _AGENTS_COLUMN_FOR.items():
            if line.startswith(f"- **{title}**"):
                columns[key] = {int(n) for n in _TRAP_NUMBER.findall(line)}

    assert set(columns) == set(_AGENTS_COLUMN_FOR.values()), (
        f"AGENTS.md §5 少了栏位，只找到 {sorted(columns)}"
    )
    expected = {
        "structural": _numbers(STRUCTURALLY_ELIMINATED),
        "guarded": _numbers(tuple(GUARD_INDEX)) - _numbers(STRUCTURALLY_ELIMINATED),
        "unguarded": _numbers(NOT_YET_GUARDED),
    }
    for key, want in expected.items():
        assert columns[key] == want, (
            f"AGENTS.md §5 的「{key}」栏与测试里对不上："
            f"文档 {sorted(columns[key])} vs 测试 {sorted(want)}"
        )
    everything = columns["structural"] | columns["guarded"] | columns["unguarded"]
    overlap = (
        (columns["structural"] & columns["guarded"])
        | (columns["structural"] & columns["unguarded"])
        | (columns["guarded"] & columns["unguarded"])
    )
    assert not overlap, f"一条陷阱落进了两栏：{sorted(overlap)}"
    assert everything == set(range(1, 26)), (
        f"三栏合起来不是 25 条，缺 {sorted(set(range(1, 26)) - everything)}"
    )


_BACKTICK = re.compile(r"`([^`]+)`")
_TEST_NAME_ONLY = re.compile(r"^test_\w+$")


def test_contract_tests_doc_rows_point_at_real_tests() -> None:
    """`docs/specs/contract-tests.md §3` 那张表点名的每条用例都要真的在它写的文件里。

    表已经漂了一片（§7.19 指着一个从未存在的函数与文件、§7.20 指着还没移植的
    `tests/contracts/test_bridge_client.py`）。不猜哪边对，只把不一致全报出来 ——
    读表的人据此以为"这条有看护"，而那份看护并不存在。

    被声明成"没有看护"的编号（`NOT_YET_GUARDED`）不查用例是否存在，但**必须**在那一行
    写明未落地；否则表看起来仍然是一条已交付的看护。
    """
    doc = (_TESTS_ROOT.parent / "docs" / "specs" / "contract-tests.md").read_text(encoding="utf-8")
    table = doc.split("## 3. 契约测试看护", 1)[1].split("### 3.1", 1)[0]
    unguarded = _numbers(NOT_YET_GUARDED)
    problems: list[str] = []

    for row in table.splitlines():
        if not row.startswith("| §"):
            continue
        cells = [cell.strip() for cell in row.strip("|").split("|")]
        if len(cells) < 4:
            problems.append(f"这一行的列数不对（期望 4 列）：{cells[0]}")
            continue
        trap = cells[0]
        found = _TRAP_NUMBER.search(trap)
        number = int(found.group(1)) if found else -1
        names = [tok for tok in _BACKTICK.findall(cells[1]) if _TEST_NAME_ONLY.match(tok)]
        files = [tok for tok in _BACKTICK.findall(cells[3]) if tok.endswith(".py")]

        if number in unguarded:
            if not any(word in row for word in ("未落地", "V2.1", "留到", "尚未", "没有看护")):
                problems.append(
                    f"{trap}: 测试里声明它没有看护，但 §3 这一行没写明——读表的人会以为有"
                )
            continue

        for tok in names:
            exists = any(
                f"def {tok}(" in (_TESTS_ROOT.parent / file).read_text(encoding="utf-8")
                for file in files
                if (_TESTS_ROOT.parent / file).is_file()
            )
            if not exists:
                problems.append(
                    f"{trap}: 点名的 {tok} 不在 {' 或 '.join(files) or '(这一行没写文件)'} 里"
                )

    assert not problems, "docs/specs/contract-tests.md §3 与 tests/ 已经漂了：\n" + "\n".join(
        f"  - {line}" for line in problems
    )


def test_abstract_base_is_subclassed_by_both_shipped_platforms() -> None:
    assert issubclass(TestDouyinContract, PlatformAdapterContractTests)
    assert issubclass(TestBilibiliContract, PlatformAdapterContractTests)
    contract_tests = {n for n in dir(PlatformAdapterContractTests) if n.startswith("test_")}
    # 抽象基类至少带这几条通用契约，V3 继承即得。
    assert {
        "test_platform_name_is_a_valid_token",
        "test_config_schema_is_platform_config_subclass",
        "test_healthcheck_returns_structured_report",
        "test_parse_creator_url_yields_non_url_platform_id",
    } <= contract_tests
