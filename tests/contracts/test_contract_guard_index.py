"""V1 §7 陷阱 → 具体看护用例的**索引漂移看护**（`docs/specs/contract-tests.md §3`）。

这张映射表是**文档**；文档会说谎 —— 一个用例被改名或删掉，`docs/specs` 里那一行还在，
读文档的人以为有看护、其实没有。这一条不测行为，测的是"每条 §7 陷阱点名的那条用例
**真的还在 `tests/` 里**"：把整个 `tests/` 树里的 `def test_*` 名字扫出来，逐条比对。

改名 / 删除某条看护 → 这里当场红，并点名是哪条 §7 失去了守卫。

V2.1 才落地的（§7.9 ASR 标点、§7.22 按位抓取）在这里显式列成"尚未落地"，
而不是默默从表里漏掉 —— 漏掉和漏实现看起来一样。
"""

from __future__ import annotations

import re
from pathlib import Path

from tests.contracts.test_platform_adapter import (
    PlatformAdapterContractTests,
    TestBilibiliContract,
    TestDouyinContract,
)

_TESTS_ROOT = Path(__file__).resolve().parent.parent

# 每条 V2.0 契约看护对应的一条**代表性**用例名（不是全部，能证明那一条在就行）。
GUARD_INDEX: dict[str, str] = {
    "§7.1 sec_uid 不是 URL": "test_share_link_follows_302_to_sec_uid",
    "§7.2 兜底留 yt-dlp 原文": "test_fallback_marks_the_source_and_keeps_the_yt_dlp_original_text",
    "§7.3 cookie 顺序真源": (
        "test_order_comes_from_capabilities_and_starts_with_the_exported_file"
    ),
    "§7.4 update_fields 字段级": "test_update_fields_does_not_clobber_other_fields",
    "§7.5 转写目录全平台统一": "test_transcript_path_is_identical_across_platforms",
    "§7.8 safe_filename": "test_result_is_always_a_single_path_segment",
    "§7.13 清单红并点名生产者": "test_an_unreadable_manifest_raises_naming_the_producer",
    "§7.14 缺生产者红不是 skip": "test_a_missing_manifest_file_is_reported_with_its_path",
    "§7.15 两条路都带 cookie": "test_the_enumeration_carries_the_same_cookie_rungs_as_download",
    "§7.16 兜底未实现如实红": "test_search_fallback_is_refused_rather_than_silently_skipped",
    "§7.20 桥 503=浏览器没了": "test_503_means_browser_dead_not_bridge_down",
    "§7.20 测不到≠正常": "test_is_healthy_only_trusts_an_explicit_ok",
    "§7.21 DASH 认音频轨": "test_a_pair_artifact_points_the_transcriber_at_the_audio_track",
    "§7.24 跟踪必须真布尔": "test_set_tracking_rejects_non_bool",
    "§7.25 墓碑一列三字段": "test_hide_removes_from_list_but_get_still_works",
    "§2 契约二 清单强制终态": "test_manifest_finalizes_on_every_exit_path",
}

# 明确属于 V2.1 的，还没写；列出来是为了不把它们当成"已经守住了"。
DEFERRED_TO_V2_1: tuple[str, ...] = (
    "§7.9 SenseVoice 标点注入",
    "§7.22 按位抓取不退化全库扫描",
)


def _all_test_function_names() -> set[str]:
    pattern = re.compile(r"^\s*(?:async\s+)?def\s+(test_\w+)", re.MULTILINE)
    names: set[str] = set()
    for path in _TESTS_ROOT.rglob("test_*.py"):
        names.update(pattern.findall(path.read_text(encoding="utf-8")))
    return names


def test_every_mapped_guard_exists():
    present = _all_test_function_names()
    assert present, "没扫到任何 test_ 函数，扫描逻辑坏了"
    missing = {trap: name for trap, name in GUARD_INDEX.items() if name not in present}
    assert not missing, "这些 §7 陷阱点名的看护用例不在了：\n" + "\n".join(
        f"  {trap}  →  {name}" for trap, name in missing.items()
    )


def test_guard_index_covers_the_15_pinned_traps_plus_manifest_contract():
    # 数量对一下，防止"表在缩但没人发现"——V2.0 契约看护这一层至少 16 条映射。
    assert len(GUARD_INDEX) >= 16
    assert len(DEFERRED_TO_V2_1) == 2


def test_abstract_base_is_subclassed_by_both_shipped_platforms():
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
