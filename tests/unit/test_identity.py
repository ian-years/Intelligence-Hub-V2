"""T4.3 跨平台主体匹配的用例：`core/identity.py`。

**先说这份基线有多薄**（量出来的，不是感觉）：V1 `cross_platform_model.py` 那 399 行里，
`tests/` 只碰过 `compact_number_to_int` 一个函数，而且**不是直接测它** ——
是拿它当哨兵去测小红书采集侧"不许有第二套解析"：
`test_metric_parsing_results_match_repo_wide_parser`、
`test_metric_parsing_delegates_instead_of_doing_its_own_arithmetic`、
`test_only_yi_suffix_is_scaled_locally`。`publication_match` / `MatchEvidence` /
`checkpoint_target` / `snapshot_unique_key` / `work_key_from_video` / 三套归一
在 V1 **一条用例都没有**（`grep -c "publication_match" tests/*.py` = 0，
而全仓也没有任何调用方）。所以：

- 搬过来的判据：那三条指标口径用例的**意图**（同口径 / 委派而不是自己算 / 只有亿自乘），
  以及"脏值必须抛、不许降级成 0"这条 V1 注释里写着的判据。
- 没能搬的：匹配打分与阈值在 V1 没有任何外部基线可抄。这正是要小心的地方 ——
  没有基线时最容易写出"永远不可能失败"的断言。所以下面这些用例一律按
  **关系 + 防空转前置** 来写：要么用一个独立算得出答案的量当预言
  （`_v1_route_verdict`、`SequenceMatcher` 自己算、集合真相交），
  要么先证明这组输入真的走到了要验的那个分支再断言结果
  （`docs/lessons.md` 经验 49 把"先量一轮"记成了教训：合成夹具只会复现你相信的东西）。
- 阈值改动：`_V1_*` 那批常数是从 V1 源码里逐字抄的（不是从 V2 实现里读的），
  改 V2 的任何一格都会红在 `test_auto_match_agrees_with_the_v1_route_oracle` 上。
"""

from __future__ import annotations

import gc
import math
import re
import sys
import warnings
from datetime import UTC, datetime, timedelta, timezone
from difflib import SequenceMatcher

import pytest

from intelligence_hub_v2.core import identity
from intelligence_hub_v2.core.identity import (
    CHECKPOINT_LIVE,
    CHECKPOINT_T3,
    CHECKPOINT_T7,
    PUBLISH_GAP_MAX_HOURS,
    MatchEvidence,
    Publication,
    normalize_content_text,
    normalize_creator_name,
    normalize_title,
)

UTC_PLUS_8 = timezone(timedelta(hours=8), "UTC+8")

DAY0 = datetime(2026, 5, 1, 12, 0, tzinfo=UTC)

IDENT = frozenset({"alex-lab"})
"""共同的跨平台主体标识 —— 五条自动匹配路都要它才可能成立。"""

LONG = "这是一段足够长的口播稿正文用来验证内容相似度那道四十字的门槛到底是不是真的在起作用"


@pytest.fixture(scope="module", autouse=True)
def _zhconv_dictionary_loads_outside_the_warning_gate() -> None:
    """把 zhconv 的惰性词典加载引到 setup 阶段，并只在那里吞掉它自己的资源泄漏。

    实测：`zhconv.loaddict()` 用 `get_module_res(name).read()` 取词典而不关文件，
    词典第一次加载完、那个 `BufferedReader` 被 GC 掉时就抛
    `ResourceWarning: unclosed file`。本仓库 `filterwarnings = ["error", ...]`
    （`pyproject.toml`）会把它经 unraisable hook 变成**红了那条用例**，
    而红的位置离原因（第三方库、第一次调用）隔了整个模块。

    所以这里只在 setup 里预热一次并只在该块内忽略 `ResourceWarning`，
    **不给整个模块挂 `ignore::ResourceWarning`** —— 那会把自己代码里的泄漏一起藏掉。
    """
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", ResourceWarning)
        identity.to_simplified_zh("繁體")
        gc.collect()


# --- V1 基线（逐字抄自 `Intelligence-Hub/cross_platform_model.py:365-380`）---
_V1_EXACT_MIN_CHARS = 8
_V1_SAME_CREATOR = (0.82, 0.84)
_V1_DURATION = (0.72, 0.74, 0.9)
_V1_STRONG_TITLE = (0.96, 12)
_V1_CONTENT = ((0.9, 0.4), (0.72, 0.55))
_V1_GAP_HOURS = 7 * 24


def _pub(
    platform: str,
    title: str,
    *,
    published_at: datetime | None = None,
    duration: float | None = None,
    record_ids: frozenset[str] = frozenset(),
    ident: frozenset[str] = frozenset(),
    names: tuple[str, ...] = (),
    content: str = "",
) -> Publication:
    return Publication(
        platform=platform,
        title=title,
        published_at=published_at,
        duration_seconds=duration,
        creator_record_ids=record_ids,
        creator_identity_keys=ident,
        creator_names=names,
        content_text=content,
    )


def _ratio(left: str, right: str) -> float:
    return SequenceMatcher(None, left, right).ratio()


def _v1_route_verdict(left: Publication, right: Publication) -> bool:
    """按 V1 源码的字面常数重算一遍 `auto_match`。

    与实现的唯一关系是"同一套判据"：常数是抄 V1 的，不是从 `identity` 里 import 的，
    所以改实现里的任何一格都会在这里对不上。
    """
    evidence = identity.publication_match(left, right)
    if not evidence.eligible:
        return False
    left_title = normalize_title(left.title)
    right_title = normalize_title(right.title)
    title = evidence.title_score
    creator = evidence.creator_score
    content = evidence.content_score or 0.0
    duration = evidence.duration_score or 0.0
    delta = evidence.publish_delta_hours

    exact = left_title == right_title and len(left_title) >= _V1_EXACT_MIN_CHARS
    same_creator = creator >= _V1_SAME_CREATOR[0] and title >= _V1_SAME_CREATOR[1]
    duration_route = (
        creator >= _V1_DURATION[0] and title >= _V1_DURATION[1] and duration >= _V1_DURATION[2]
    )
    strong_title = (
        title >= _V1_STRONG_TITLE[0]
        and min(len(left_title), len(right_title)) >= _V1_STRONG_TITLE[1]
    )
    content_route = any(
        content >= floor and title >= floor_title for floor, floor_title in _V1_CONTENT
    )
    timely = delta is None or delta <= _V1_GAP_HOURS
    return bool(timely and evidence.creator_identity_match) and bool(
        exact or same_creator or duration_route or strong_title or content_route
    )


# 覆盖五条路 + 缺证据的组合。每条都是"两条不同平台的记录"，因为同平台进不去判定。
CORPUS: list[Publication] = [
    # 0,1: `exact` 路（归一后完全相同且 ≥8 字）
    _pub("bilibili", "五个人的复盘笔记", ident=IDENT),
    _pub("douyin", "五个人的复盘笔记!!", ident=IDENT),
    # 2: `same_creator` 路独占（title 0.8889，不到 0.96，也不相等）
    _pub("bilibili", "ABCDEFGH1", published_at=DAY0, ident=IDENT),
    # 3
    _pub("douyin", "ABCDEFGH2", published_at=DAY0, ident=IDENT),
    # 4,5: `duration` 路独占（title 0.75 掉到 0.84 以下，靠时长补）
    _pub("bilibili", "abcdefgh", published_at=DAY0, duration=600.0, ident=IDENT),
    _pub("douyin", "abcdefwx", published_at=DAY0, duration=600.0, ident=IDENT),
    # 6,7: `content` 路独占（标题几乎不相干，口播稿同文）
    _pub(
        "bilibili",
        "完全不同的一个甲乙丙丁标题",
        published_at=DAY0,
        ident=IDENT,
        content=LONG,
    ),
    _pub(
        "douyin",
        "另外一件事的乙丙丁戊标题",
        published_at=DAY0,
        ident=IDENT,
        content=LONG,
    ),
    # 8,9: `strong_title` 路（0.96 恰好命中，长度 ≥12）
    _pub("bilibili", "abcdefghijklmnopqrstuvwxy", published_at=DAY0, ident=IDENT),
    _pub("douyin", "abcdefghijklmnopqrstuvwxq", published_at=DAY0, ident=IDENT),
    # 10,11: 同样的强标题，但**没有**主体标识（名字相同也不许自动合并）
    _pub("bilibili", "五个人的复盘笔记", published_at=DAY0, names=("Alex实验室",)),
    _pub("douyin", "五个人的复盘笔记!!", published_at=DAY0, names=("Alex实验室",)),
]


def _pairs() -> list[tuple[Publication, Publication]]:
    """把语料按声明顺序两两配对（步长 2），并对每对做双向检查。"""
    return [(CORPUS[i], CORPUS[i + 1]) for i in range(0, len(CORPUS) - 1, 2)]


# ---------------------------------------------------------------------------
# 判定：预言与不变量
# ---------------------------------------------------------------------------


def test_the_corpus_itself_is_not_degenerate() -> None:
    """防空转：语料如果全是同一条记录，预言那条用例就永远绿。"""
    assert len(CORPUS) % 2 == 0, "语料要成对，最后一条落单就说明配对写错了"
    pairs = _pairs()
    assert len(pairs) == len(CORPUS) // 2 == 6, pairs
    for left, right in pairs:
        assert left != right
        assert left.platform != right.platform, "同平台的对子进不了判定，等于没测"
        assert normalize_title(left.title) and normalize_title(right.title)
    verdicts = [identity.publication_match(a, b).auto_match for a, b in pairs]
    assert set(verdicts) == {True, False}, "语料只覆盖了一边，预言没有对手"


@pytest.mark.parametrize("index", range(6))
def test_auto_match_agrees_with_the_v1_route_oracle(index: int) -> None:
    left, right = _pairs()[index]
    for a, b in ((left, right), (right, left)):
        evidence = identity.publication_match(a, b)
        assert evidence.auto_match == _v1_route_verdict(a, b), evidence.reason


def test_every_declared_route_is_reachable_and_the_two_redundant_ones_are_pinned(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """每条路都要有样本，且**命中集合**要等于实测出来的那份覆盖关系。

    判据是独立算出来的：用 V1 常数在 evidence 的四个分量上重算五条布尔。
    `exact` 与 `strong_title` 各自还带着 `same_creator` —— 因为主体标识一命中
    `creator_score` 就是 1.0，那两路的标题门槛（1.0 / ≥0.96）必然越过 0.84。
    这不是"测试写松了"，而是 V1 公式的真实性质；下面第二条循环用**降低门槛**的
    办法把这两路单独逼出来，证明它们不是恒假、也不是无人接管。
    """
    samples = {
        "exact": _pairs()[0],
        "same_creator": _pairs()[1],
        "duration": _pairs()[2],
        "content": _pairs()[3],
        "strong_title": _pairs()[4],
        "no_identity": _pairs()[5],
    }
    expected_hits = {
        "exact": {"exact", "same_creator"},
        "same_creator": {"same_creator"},
        "duration": {"duration"},
        "content": {"content"},
        "strong_title": {"strong_title", "same_creator"},
        "no_identity": {"exact", "same_creator"},
    }
    for name, (left, right) in samples.items():
        evidence = identity.publication_match(left, right)
        lt, rt = normalize_title(left.title), normalize_title(right.title)
        hits = {
            "exact": lt == rt and len(lt) >= _V1_EXACT_MIN_CHARS,
            "same_creator": evidence.creator_score >= _V1_SAME_CREATOR[0]
            and evidence.title_score >= _V1_SAME_CREATOR[1],
            "duration": evidence.creator_score >= _V1_DURATION[0]
            and evidence.title_score >= _V1_DURATION[1]
            and (evidence.duration_score or 0.0) >= _V1_DURATION[2],
            "strong_title": evidence.title_score >= _V1_STRONG_TITLE[0]
            and min(len(lt), len(rt)) >= _V1_STRONG_TITLE[1],
            "content": any(
                (evidence.content_score or 0.0) >= floor and evidence.title_score >= title_floor
                for floor, title_floor in _V1_CONTENT
            ),
        }
        assert {k for k, on in hits.items() if on} == expected_hits[name], f"{name}: {hits}"
        assert evidence.auto_match is (name != "no_identity"), (name, evidence.reason)

    # 两路冗余的独立取证：把 `same_creator` 的门抬到样本之上，`exact` / `strong_title`
    # 必须还能单独把判定顶起来 —— 说明它们不是恒假，只是今天被覆盖。
    for name, floor in (("exact", 1.01), ("strong_title", 1.01)):
        left, right = samples[name]
        monkeypatch.setattr(identity, "_SAME_CREATOR_MIN_TITLE", floor)
        monkeypatch.setattr(identity, "_SAME_CREATOR_MIN_CREATOR", floor)
        evidence = identity.publication_match(left, right)
        assert not (evidence.creator_score >= floor and evidence.title_score >= floor), "样本不满足"
        assert evidence.auto_match, f"{name} 路在 same_creator 关掉后应当接管"


def test_exact_and_strong_routes_are_covered_by_same_creator() -> None:
    """钉住一条实测出来的冗余，而不是假装五条路都独立。

    `auto_match` 要求主体标识命中，而标识命中时 `creator_score` 恒为 1.0，
    于是 title=1.0（`exact`）与 title≥0.96（`strong_title`）必然也满足
    `same_creator`（门槛 0.84）。这两路今天不改变判定结果 —— 实现变了
    （比如 creator 不再是 1.0）这条就会红，那是**该重新评估**的信号。
    """
    for index in (0, 4):
        left, right = _pairs()[index]
        evidence = identity.publication_match(left, right)
        assert evidence.creator_identity_match and evidence.creator_score == 1.0
        assert evidence.title_score >= _V1_SAME_CREATOR[1], "样本不满足覆盖前提"
        assert evidence.auto_match


def test_matching_is_symmetric_and_score_is_a_convex_combination() -> None:
    for left, right in _pairs():
        forward = identity.publication_match(left, right)
        backward = identity.publication_match(right, left)
        assert forward == backward, "同一对记录换个顺序就该给出同一份证据"
        assert 0.0 <= forward.score <= 1.0, forward.reason
        parts = [forward.title_score, forward.creator_score]
        parts.append(0.5 if forward.publish_delta_hours is None else 1.0)
        if forward.content_score is not None:
            parts.append(forward.content_score)
        if forward.duration_score is not None:
            parts.append(forward.duration_score)
        assert min(parts) <= forward.score <= max(parts), (forward.score, parts)


def test_score_is_a_weighted_mean_of_its_own_parts() -> None:
    """四套权重表的和都是 1.0 —— 用一条**独立**算式反解出 time 那一项再对答案。

    防空转：先确认这对输入真的走到"正文 + 时长都在"那一路（五元权重表），
    否则反解出来的是另一张表，等式也会巧合成立。
    """
    left = _pub(
        "bilibili",
        "五个人的复盘笔记",
        published_at=DAY0,
        duration=600.0,
        record_ids=frozenset({"c"}),
        ident=IDENT,
        content=LONG,
    )
    right = _pub(
        "douyin",
        "五个人的复盘笔记!!",
        published_at=DAY0 + timedelta(hours=1),
        duration=300.0,
        record_ids=frozenset({"d"}),
        ident=IDENT,
        content=LONG + "改动一点点让它不是满分",
    )
    evidence = identity.publication_match(left, right)
    assert evidence.content_score is not None and evidence.duration_score is not None
    assert evidence.duration_score < 1.0, "时长必须不等，否则 time 项被抵消"
    assert evidence.publish_delta_hours == 1.0

    implied_time = (
        evidence.score
        - 0.35 * evidence.title_score
        - 0.25 * (evidence.content_score or 0.0)
        - 0.2 * evidence.creator_score
        - 0.1 * evidence.duration_score
    ) / 0.1
    # `score` 出口前 `round(..., 4)`，所以反解出来的 time 项只能对上到 1e-3
    assert implied_time == pytest.approx(1 - 1 / PUBLISH_GAP_MAX_HOURS, abs=1e-3)


def test_weight_tables_partition_by_evidence_presence() -> None:
    """换表条件就是"正文/时长在不在"，没有第五种组合。"""
    for content, duration in ((None, None), (1.0, None), (None, 1.0), (1.0, 1.0)):
        parts = identity._score_parts(
            title_score=1.0,
            creator_score=1.0,
            time_score=1.0,
            content_score=content,
            duration_score=duration,
        )
        expected = 3 + (content is not None) + (duration is not None)
        assert len(parts) == expected, (content, duration, parts)
        assert sum(weight for _, weight in parts) == pytest.approx(1.0)


def _without_identity(record: Publication) -> Publication:
    """同一条记录，只剥掉主体标识与共同博主记录，其余照抄。"""
    return Publication(
        platform=record.platform,
        title=record.title,
        published_at=record.published_at,
        duration_seconds=record.duration_seconds,
        creator_names=record.creator_names,
        content_text=record.content_text,
    )


def test_auto_match_never_fires_without_a_shared_creator_identity() -> None:
    """同一批对子只去掉主体标识，原来为真的必须**全部**变假（名字再像也不行）。"""
    stripped = [_without_identity(p) for p in CORPUS]
    indices = range(0, len(CORPUS) - 1, 2)
    before = [identity.publication_match(CORPUS[i], CORPUS[i + 1]) for i in indices]
    after = [identity.publication_match(stripped[i], stripped[i + 1]) for i in indices]
    assert [e.auto_match for e in before] == [True, True, True, True, True, False], before
    assert all(e.creator_identity_match for e in before[:5])
    assert not any(e.creator_identity_match for e in after), "剥标识剥得不干净，这条就在验空气"
    assert [e.auto_match for e in after] == [False] * len(after), [e.reason for e in after]
    # 反证：最后一对的名字完全相似（creator_score=1.0），缺的只有标识
    assert after[-1].creator_score == 1.0 and stripped[-2].creator_names


# ---------------------------------------------------------------------------
# 三道闸
# ---------------------------------------------------------------------------


def test_the_platform_gate_wins_over_everything_else() -> None:
    left = _pub("bilibili", "五个人的复盘笔记", ident=IDENT, content=LONG, duration=600.0)
    same = _pub("bilibili", "五个人的复盘笔记", ident=IDENT, content=LONG, duration=600.0)
    evidence = identity.publication_match(left, same)
    assert evidence.reason == "same_or_missing_platform"
    assert not evidence.eligible and not evidence.auto_match
    assert evidence.score == 0.0
    # 防空转：这两条除了平台之外全部相同，拦住它的只有平台那一项
    assert identity.normalize_title(left.title) == identity.normalize_title(same.title)
    assert left.content_text == same.content_text and left.duration_seconds == same.duration_seconds


@pytest.mark.parametrize("platform", ["", "   ", "\t"])
def test_a_blank_platform_is_missing_not_different(platform: str) -> None:
    """空白平台值走"缺失"那一条，与 V1 `video_platform()` 认不出返回空串同形。"""
    left = _pub(platform, "五个人的复盘笔记", ident=IDENT)
    right = _pub("douyin", "五个人的复盘笔记!!", ident=IDENT)
    assert identity.publication_match(left, right).reason == "same_or_missing_platform"


def test_missing_title_gate_sits_behind_the_platform_gate() -> None:
    both_missing = _pub("", "  !!  "), _pub("", "##  ##")
    assert normalize_title(both_missing[0].title) == ""
    assert normalize_title(both_missing[1].title) == ""
    assert identity.publication_match(*both_missing).reason == "same_or_missing_platform"

    left, right = _pub("bilibili", "!!"), _pub("douyin", "##")
    assert identity.publication_match(left, right).reason == "missing_title"


def test_the_seven_day_gate_boundary_is_inclusive_on_the_far_side() -> None:
    left = _pub("bilibili", "五个人的复盘笔记", published_at=DAY0, ident=IDENT)
    at_boundary = Publication(
        platform="douyin",
        title="五个人的复盘笔记!!",
        published_at=DAY0 + timedelta(hours=PUBLISH_GAP_MAX_HOURS),
        creator_identity_keys=IDENT,
    )
    evidence = identity.publication_match(left, at_boundary)
    assert evidence.publish_delta_hours == float(PUBLISH_GAP_MAX_HOURS)
    assert evidence.eligible and evidence.auto_match, "V1 是 `> 7*24`，恰好 7 天不算超"

    past = Publication(
        platform="douyin",
        title="五个人的复盘笔记!!",
        published_at=DAY0 + timedelta(hours=PUBLISH_GAP_MAX_HOURS, seconds=1),
        creator_identity_keys=IDENT,
    )
    late = identity.publication_match(left, past)
    assert late.reason == "publish_gap_over_7_days"
    assert not late.eligible and late.score == 0.0
    # 闸 3 排在标题分之后：不 eligible 也要把证据留着给人回看
    assert late.title_score == pytest.approx(1.0)
    assert late.creator_identity_match
    assert late.publish_delta_hours is not None and late.publish_delta_hours > PUBLISH_GAP_MAX_HOURS


def test_the_gap_gate_uses_the_same_number_as_the_decay_window() -> None:
    """硬闸、`time_score` 的衰减分母、`timely` 的重述必须是同一个数。"""
    assert PUBLISH_GAP_MAX_HOURS == _V1_GAP_HOURS == 7 * 24
    assert identity._time_similarity(PUBLISH_GAP_MAX_HOURS) == 0.0
    assert identity._time_similarity(PUBLISH_GAP_MAX_HOURS + 1) == 0.0
    assert identity._time_similarity(None) == 0.5
    assert identity._time_similarity(0) == 1.0


# ---------------------------------------------------------------------------
# 空串占位与主体标识
# ---------------------------------------------------------------------------


def test_whitespace_only_identity_placeholders_are_not_a_match() -> None:
    left = _pub(
        "bilibili", "五个人的复盘笔记", record_ids=frozenset({"  "}), ident=frozenset({" "})
    )
    right = _pub(
        "douyin",
        "五个人的复盘笔记!!",
        record_ids=frozenset({"  ", ""}),
        ident=frozenset({" ", ""}),
    )
    # 防空转：原始集合**真的**相交，拦住它的是"空白不算成员"这一条
    assert left.creator_record_ids & right.creator_record_ids
    assert left.creator_identity_keys & right.creator_identity_keys
    evidence = identity.publication_match(left, right)
    assert not evidence.creator_identity_match
    assert evidence.creator_score == 0.0
    assert not evidence.auto_match


def test_a_shared_creator_record_counts_as_identity_even_across_platforms() -> None:
    left = _pub("bilibili", "五个人的复盘笔记", record_ids=frozenset({"c-42"}))
    right = _pub("douyin", "五个人的复盘笔记!!", record_ids=frozenset({"c-7", "c-42"}))
    evidence = identity.publication_match(left, right)
    assert evidence.creator_identity_match
    assert evidence.creator_score == 1.0
    assert evidence.auto_match


def test_shared_identity_key_beats_completely_different_display_names() -> None:
    left = _pub("bilibili", "五个人的复盘笔记", ident=IDENT, names=("甲",))
    right = _pub("douyin", "五个人的复盘笔记!!", ident=IDENT, names=("完全不同的乙",))
    assert _ratio(normalize_creator_name("甲"), normalize_creator_name("完全不同的乙")) < 0.5
    evidence = identity.publication_match(left, right)
    assert evidence.creator_score == 1.0 and evidence.auto_match


def test_without_identity_the_creator_score_is_the_best_pair_not_the_first() -> None:
    left = _pub("bilibili", "五个人的复盘笔记", names=("张三", "五个人"))
    right = _pub("douyin", "五个人的复盘笔记!!", names=("毫无关系的一个名字", "五个人"))
    evidence = identity.publication_match(left, right)
    assert not evidence.creator_identity_match
    assert evidence.creator_score == 1.0, "两组里有一对相同就该给满分"
    assert evidence.title_score == 1.0
    assert not evidence.auto_match, "creator 相似度再高，没有主体标识也不自动合并"


# ---------------------------------------------------------------------------
# 归一
# ---------------------------------------------------------------------------

_SUFFIX_LABELS = ["抖音", "哔哩哔哩", "bilibili", "Bilibili", "小红书", "YouTube", "YOUTUBE"]
_UNRECOGNISED_SUFFIXES = ["B站", "快手", "douyin", "xiaohongshu"]


@pytest.mark.parametrize("label", _SUFFIX_LABELS)
def test_only_the_recognised_suffix_labels_get_stripped(label: str) -> None:
    """关系：**在 V1 那张表上的**尾巴被剥掉，不在表上的留着 —— 两个方向都断言。

    `B站` 与两个 V2 slug 都不在表上，这是 V1 的既有形状（它的标题尾巴是从页面上抄来的
    那几种写法，而 `B站` 是 V1 内部的**平台显示名**，不会出现在标题里）。
    接线时如果有人往标题里写 slug，要么改这张表、要么在写库前把尾巴去掉。
    """
    assert normalize_title(f"如何做复盘 - {label}") == "如何做复盘"


@pytest.mark.parametrize("label", _UNRECOGNISED_SUFFIXES)
def test_suffixes_outside_the_v1_table_are_kept_as_content(label: str) -> None:
    assert normalize_title(f"如何做复盘 - {label}") == f"如何做复盘{label.lower()}"
    assert normalize_title(f"如何做复盘 - {label}") != normalize_title("如何做复盘")


def test_hashtags_and_full_width_layout_are_folded() -> None:
    assert normalize_title("如何做复盘 #干货分享 #职场") == "如何做复盘"
    assert normalize_title("如何做复盘　#干货#职场") == "如何做复盘"
    # 防空转：没归一之前这几串两两都不相等
    assert len({normalize_title(t) for t in _HASHTAG_VARIANTS}) == 1
    assert len(_HASHTAG_VARIANTS) > 1


_HASHTAG_VARIANTS = ("如何做复盘", "如何做复盘 #干货", "如何做复盘#干货#职场", "如何做复盘 ＃干货")


def test_normalizers_route_through_the_simplifier_rather_than_ignoring_it(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """哨兵替换（V1 `test_metric_parsing_delegates_instead_of_doing_its_own_arithmetic` 的形状）：
    把简繁转换换成一个显眼的实现，三套归一的结果必须跟着变。
    """
    seen: list[str] = []

    def sentinel(text: str) -> str:
        seen.append(text)
        return "SENTINEL"

    monkeypatch.setattr(identity, "to_simplified_zh", sentinel)
    assert normalize_title("任意标题 - YouTube") == "SENTINEL"
    assert normalize_creator_name("任意博主的抖音") == "SENTINEL"
    assert normalize_content_text("https://x.com/a #标签 任意正文") == "SENTINEL"
    assert len(seen) == 3, seen


def test_the_simplifier_degrades_to_the_raw_text_when_zhconv_is_missing(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """没装 `media` extra 的那台机器上：归一不许炸，只是不再折繁简。

    `sys.modules["zhconv"] = None` 会让 `import zhconv` 当场 ImportError ——
    这是不起子进程就能走到那条降级分支的唯一办法
    （`tests/integration/test_postprocess_task.py` 用同一个形状喂假 `sherpa_onnx`）。
    """
    monkeypatch.setitem(sys.modules, "zhconv", None)
    assert identity.to_simplified_zh("如何做好復盤") == "如何做好復盤"
    assert normalize_title("如何做好復盤") != normalize_title("如何做好复盘")
    evidence = identity.publication_match(
        _pub("bilibili", "如何做好復盤", ident=IDENT),
        _pub("douyin", "如何做好复盘", ident=IDENT),
    )
    # 降级只该少一分证据，不该让判定跑不下去
    assert evidence.eligible and 0.0 < evidence.title_score < 1.0
    assert not evidence.auto_match, "繁简没折叠时这条标题不够 0.84，就该落到人工确认"


def test_traditional_and_simplified_titles_normalize_to_the_same_key() -> None:
    """简繁归一真的在干活：先证明原串不同，再证明归一后相同。

    这条**不 skip**：`zhconv` 没装就该红（V1 §7.14 —— 绿色的 skip 会让看护静默消失）。
    """
    traditional = "如何做好復盤"
    simplified = "如何做好复盘"
    assert traditional != simplified
    assert SequenceMatcher(None, traditional, simplified).ratio() < 1.0
    assert identity.to_simplified_zh(traditional) == identity.to_simplified_zh(simplified)
    assert normalize_title(traditional) == normalize_title(simplified)
    assert normalize_title(traditional) == "如何做好复盘"


def test_creator_suffix_table_is_what_keeps_two_spellings_together() -> None:
    """去掉 `_CREATOR_SUFFIX_RE` 里的 `聊赚钱` 就会掉到所有 creator 门槛之下 —— 先量再断言。"""
    assert normalize_creator_name("张三聊赚钱") == "张三"
    assert normalize_creator_name("张三官方") == "张三"
    assert normalize_creator_name("Alex实验室的抖音") == normalize_creator_name("alex实验室")
    assert _ratio("张三聊赚钱", "张三") < _V1_DURATION[0], "前置：不靠这条规则就不够门槛"
    assert normalize_creator_name("张三聊赚钱了") == "张三聊赚钱了"


def test_content_normalisation_drops_urls_and_caps_the_length() -> None:
    body = "这是一段足够长的口播稿正文用来验证内容相似度那道四十字的门槛"
    assert normalize_content_text(f"https://example.com/a?b=1 {body} #标签") == body
    assert normalize_content_text(f"{body} http://x.cn ") == body
    assert len(normalize_content_text("字" * 9000)) == 6000
    assert normalize_content_text("短") == "短"
    # 防空转：URL 与标签这几段确实在原文里，去掉之后长度变了
    assert len(body) < len(f"https://example.com/a?b=1 {body} #标签")


def test_a_short_transcript_is_no_evidence_but_a_long_one_is() -> None:
    """40 字门槛：短正文给 `None`（缺证据）而不是 0（否证）。"""
    short_left, short_right = (
        normalize_content_text("赚钱干货"),
        normalize_content_text("赚钱干货分享"),
    )
    assert short_left != short_right and short_left and short_right
    assert len(short_left) < identity.MIN_CONTENT_COMPARE_CHARS
    assert identity._content_similarity("赚钱干货", "赚钱干货分享") is None
    score = identity._content_similarity(LONG, LONG + "多出来的几个字")
    assert score is not None and 0.0 < score < 1.0


# ---------------------------------------------------------------------------
# 指标口径
# ---------------------------------------------------------------------------

_EMPTY_METRIC_VALUES: tuple[object, ...] = (None, "", "   ", "-", "--")
_DIRTY_METRIC_VALUES: tuple[object, ...] = ("点赞失败", "1.2亿", "万", "abc", "-5", "1.2.3", "1万2")


def test_compact_counts_cover_every_documented_scale() -> None:
    assert identity.compact_number_to_int("1.2万") == 12_000
    assert identity.compact_number_to_int("12w") == 120_000
    assert identity.compact_number_to_int("12W") == 120_000
    assert identity.compact_number_to_int("3k") == 3_000
    assert identity.compact_number_to_int("3K+") == 3_000
    assert identity.compact_number_to_int("1,234") == 1_234
    assert identity.compact_number_to_int("１２３") == 123  # 全角数字靠 NFKC
    assert identity.compact_number_to_int("1.2万+") == 12_000


def test_compact_counts_are_value_equal_across_spellings_and_types() -> None:
    """V1 `test_metric_parsing_results_match_repo_wide_parser` 的意图：同一个数不论
    写成 int / float / 带千分位 / 带量级后缀，出口必须是同一个整数。
    """
    groups: list[tuple[object, ...]] = [
        ("1.2万", "12000", 12_000, 12_000.0, "12,000", "12000+"),
        ("3k", "3000", 3_000, "3,000", "3K"),
        ("12万", "120000", 120_000, "12w", "12W"),
        ("7", 7, 7.0, "07"),
    ]
    for group in groups:
        results = {identity.compact_number_to_int(value) for value in group}
        assert len(results) == 1, (group, results)
    # 防空转：每组得真的混了"写法"（字符串与数值两种入参形状），否则这条只是在复读自己
    assert all(len({type(value) for value in group}) >= 2 for group in groups)


def test_empty_metric_values_are_none_and_dirty_ones_raise() -> None:
    """两个方向都要断言：`None` 只留给"没有"，脏值不许静默变成 0 或 None。"""
    for value in _EMPTY_METRIC_VALUES:
        assert identity.compact_number_to_int(value) is None, value
    for value in _DIRTY_METRIC_VALUES:
        with pytest.raises(ValueError, match="count"):
            identity.compact_number_to_int(value)
    # 防空转：这两族不能重叠（重叠就说明有一条脏值被当成"没有"吞掉了）
    assert not set(map(repr, _EMPTY_METRIC_VALUES)) & set(map(repr, _DIRTY_METRIC_VALUES))


def test_bool_is_not_a_count_even_though_true_is_an_int() -> None:
    assert isinstance(True, int), "前置：不挡 bool 就会静默变成 1"
    with pytest.raises(ValueError, match="Boolean"):
        identity.compact_number_to_int(True)


def test_non_finite_and_negative_counts_raise_instead_of_clamping() -> None:
    for value in (float("nan"), float("inf"), -1, -0.5):
        with pytest.raises(ValueError, match="Invalid metric count"):
            identity.compact_number_to_int(value)
    assert math.isfinite(float("inf")) is False


def test_fractional_counts_follow_round_half_to_even() -> None:
    """V1 用的是 `int(round(float(x)))`，也就是 banker's rounding。别"顺手"改成 floor。"""
    assert identity.compact_number_to_int(2.5) == 2
    assert identity.compact_number_to_int(3.5) == 4
    assert identity.compact_number_to_int("0.5万") == 5_000


def test_yi_is_not_a_scale_here_and_the_caller_has_to_scale_it() -> None:
    """V1 `test_only_yi_suffix_is_scaled_locally` 的口径：亿由调用方摘出来再乘。"""
    with pytest.raises(ValueError, match="Unsupported compact metric"):
        identity.compact_number_to_int("3亿")

    def parse_like_the_collector(value: str) -> int | None:
        """V1 `download_xiaohongshu_latest.py::parse_metric_number` 的那一段。"""
        text = value.strip()
        if not text.endswith("亿"):
            return identity.compact_number_to_int(text)
        scaled = identity.compact_number_to_int(text[:-1])
        return None if scaled is None else scaled * 10**8

    assert parse_like_the_collector("3亿") == 300_000_000
    assert parse_like_the_collector("1.2万") == 12_000
    # 前置：亿确实是**这里**不吃，而不是那个包装函数没生效
    assert identity.compact_number_to_int("3") == 3


# ---------------------------------------------------------------------------
# 时间
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (1_700_000_000, datetime(2023, 11, 14, 22, 13, 20, tzinfo=UTC)),
        (1_700_000_000_000, datetime(2023, 11, 14, 22, 13, 20, tzinfo=UTC)),
        (1_700_000_000.5, datetime(2023, 11, 14, 22, 13, 20, 500_000, tzinfo=UTC)),
        ("2026-05-01", datetime(2026, 5, 1, tzinfo=UTC)),
        ("2026-05-01 12:00:00", datetime(2026, 5, 1, 12, tzinfo=UTC)),
        ("2026-05-01 12:00", DAY0),
        ("2026-05-01T12:00", DAY0),
        ("2026-05-01T12:00Z", DAY0),
        ("2026-05-01 20:00+08:00", DAY0),
        (datetime(2026, 5, 1, 20, 0, tzinfo=UTC_PLUS_8), DAY0),
        # 就是要喂一个 naive：出口必须按 UTC 解释它
        (datetime(2026, 5, 1, 12, 0), DAY0),  # noqa: DTZ001
    ],
)
def test_parse_datetime_accepts_the_documented_shapes(value: object, expected: datetime) -> None:
    parsed = identity.parse_datetime(value)
    assert parsed == expected
    assert parsed.tzinfo is not None, "出口必须是 aware —— 混帧相减是 V1 的那次 TypeError"
    assert parsed.utcoffset() == timedelta(0), "统一折到 UTC，不给本机时区留位置"


@pytest.mark.parametrize("value", [None, "", "   ", []])
def test_parse_datetime_treats_only_these_as_absent(value: object) -> None:
    assert identity.parse_datetime(value) is None


def test_parse_datetime_refuses_to_guess_a_moment_for_unreadable_text() -> None:
    """V1 只把**数字类型**当 epoch，纯数字串走文本那条路然后抛。

    这不是缺口，是 V1 的原样：吃 `"1700000000"` 的是采集侧的
    `parse_publish_time()`。合并成一份的风险是"记账侧悄悄宽容了"，
    那正好把脏数据放进来（AGENTS.md §1.3）。
    """
    for value in ("3天前", "2026-13-45", "not a date", "2026/05/01", "1700000000", True):
        with pytest.raises(ValueError, match="Unsupported datetime"):
            identity.parse_datetime(value)
    # 防空转：被拒的那些串真的不是 ISO，也不是那三种 `%Y-%m-%d` 形状
    for value in ("3天前", "2026-13-45", "not a date", "1700000000"):
        with pytest.raises(ValueError):
            datetime.fromisoformat(value.replace("T", " ").replace("Z", "+00:00"))
    # 反向防空转：接受的形状里必须**有**能被 fromisoformat 吃下的，否则上面那段在验空气
    assert datetime.fromisoformat("2026-05-01 12:00") == datetime(  # noqa: DTZ001
        2026, 5, 1, 12
    )


def test_the_delta_is_frame_independent_so_mixing_naive_and_aware_is_safe() -> None:
    """naive 按 UTC 读（`UTCDateTime` 同一判据），aware 折到 UTC —— 于是：

    naive 20:00 对 aware `20:00Z` 是同一瞬时，差 0；
    naive 20:00 对 aware `12:00Z` 差 8 小时，**与这台机器在哪个时区无关**。
    V1 的做法是把 aware 折成本机时区（本机 +8）再丢掉 tzinfo，于是这两条的答案
    恰好互换：这里 0 的地方它给 8，这里 8 的地方它给 0 —— 换台机器又是另一组数。
    """
    naive_evening = datetime(2026, 5, 1, 20, 0)  # noqa: DTZ001 - 故意 naive
    left = _pub("bilibili", "五个人的复盘笔记", published_at=naive_evening)
    same_instant = _pub(
        "douyin", "五个人的复盘笔记!!", published_at=datetime(2026, 5, 1, 20, 0, tzinfo=UTC)
    )
    other_instant = _pub("douyin", "五个人的复盘笔记!!", published_at=DAY0)
    shifted_too = _pub(
        "douyin",
        "五个人的复盘笔记!!",
        published_at=datetime(2026, 5, 1, 20, 0, tzinfo=UTC_PLUS_8),
    )
    assert identity.publication_match(left, same_instant).publish_delta_hours == 0.0
    evidence = identity.publication_match(left, other_instant)
    assert evidence.publish_delta_hours == 8.0
    # aware 的那一侧按它自己的瞬时算，不当成"已经是 UTC 了"
    assert identity.publication_match(left, shifted_too).publish_delta_hours == 8.0
    # 防空转：上面那个 8 不是凑出来的 —— 它等于 naive 被读成 UTC 与真 UTC 的差
    assert identity._publish_delta_hours(naive_evening, DAY0) == 8.0
    assert identity._publish_delta_hours(DAY0, None) is None


def test_a_missing_publish_time_never_blocks_the_gate_it_needs_to_measure() -> None:
    left = _pub("bilibili", "五个人的复盘笔记", ident=IDENT)
    right = _pub("douyin", "五个人的复盘笔记!!", ident=IDENT)
    evidence = identity.publication_match(left, right)
    assert evidence.publish_delta_hours is None
    assert evidence.eligible and evidence.auto_match
    assert "publish_delta_hours=n/a" in evidence.reason


# ---------------------------------------------------------------------------
# 快照与作品键
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(("checkpoint", "days"), [(CHECKPOINT_T3, 3), (CHECKPOINT_T7, 7)])
def test_checkpoint_target_matches_the_table(checkpoint: str, days: int) -> None:
    assert identity.CHECKPOINT_DAYS[checkpoint] == days
    assert identity.checkpoint_target(DAY0, checkpoint) == DAY0 + timedelta(days=days)
    assert identity.checkpoint_target(DAY0, checkpoint).tzinfo is UTC


@pytest.mark.parametrize(
    "checkpoint", [identity.CHECKPOINT_INITIAL, CHECKPOINT_LIVE, "T+1", "t+3", ""]
)
def test_unscheduled_checkpoints_have_no_target(checkpoint: str) -> None:
    assert checkpoint not in identity.CHECKPOINT_DAYS
    with pytest.raises(ValueError, match="Unsupported scheduled checkpoint") as excinfo:
        identity.checkpoint_target(DAY0, checkpoint)
    if checkpoint:
        assert checkpoint in str(excinfo.value), "报错要能说出是哪个检查点不行"


def test_live_keys_bucket_by_hour_while_scheduled_keys_do_not() -> None:
    """两种键的幂等语义相反，必须分别钉住。"""
    early = datetime(2026, 5, 1, 9, 5, tzinfo=UTC)
    late_same_hour = datetime(2026, 5, 1, 9, 59, tzinfo=UTC)
    next_hour = datetime(2026, 5, 1, 10, 0, tzinfo=UTC)
    assert identity.snapshot_unique_key(
        "v1", CHECKPOINT_LIVE, early
    ) == identity.snapshot_unique_key("v1", CHECKPOINT_LIVE, late_same_hour)
    assert identity.snapshot_unique_key("v1", CHECKPOINT_LIVE, early) != (
        identity.snapshot_unique_key("v1", CHECKPOINT_LIVE, next_hour)
    )
    # 调度检查点：**给**了 captured_at 也不许进键，否则 enrich 重跑一次多一行
    assert (
        identity.snapshot_unique_key("v1", CHECKPOINT_T3, early)
        == identity.snapshot_unique_key("v1", CHECKPOINT_T3, next_hour)
        == "metric:v1:T+3"
    )


def test_live_keys_bucket_in_utc_not_in_the_machines_offset() -> None:
    naive = datetime(2026, 5, 1, 9, 30)  # noqa: DTZ001 - 故意 naive
    aware = datetime(2026, 5, 1, 9, 30, tzinfo=UTC)
    assert identity.snapshot_unique_key("v1", CHECKPOINT_LIVE, naive).endswith("2026050109")
    assert identity.snapshot_unique_key("v1", CHECKPOINT_LIVE, aware) == (
        identity.snapshot_unique_key("v1", CHECKPOINT_LIVE, naive)
    )
    shifted = datetime(2026, 5, 1, 9, 30, tzinfo=UTC_PLUS_8)
    assert identity.snapshot_unique_key("v1", CHECKPOINT_LIVE, shifted).endswith("2026050101")


def test_live_key_requires_an_explicit_capture_time() -> None:
    with pytest.raises(ValueError, match="captured_at"):
        identity.snapshot_unique_key("v1", CHECKPOINT_LIVE)
    assert identity.snapshot_unique_key("v1", CHECKPOINT_T3) == "metric:v1:T+3"


def test_work_keys_are_stable_unique_and_platform_shape_free() -> None:
    ids = [f"BV1xx411c7{i}" for i in range(50)] + [
        f"{1_000_000_000_000_000 + i}" for i in range(50)
    ]
    keys = [identity.work_key_from_video(vid) for vid in ids]
    assert keys == [identity.work_key_from_video(vid) for vid in ids], "同一个输入必须同一个键"
    assert len(set(keys)) == len(keys), "50+50 个 ID 撞了"
    for key in keys:
        assert re.fullmatch(r"work_[0-9a-f]{16}", key), key
        assert "BV" not in key


# ---------------------------------------------------------------------------
# 证据的可审计性
# ---------------------------------------------------------------------------


def test_to_dict_covers_every_field_and_nothing_else() -> None:
    import dataclasses  # noqa: PLC0415 - 只在这条用例里用一次

    evidence = identity.publication_match(CORPUS[0], CORPUS[1])
    payload = evidence.to_dict()
    assert set(payload) == {f.name for f in dataclasses.fields(MatchEvidence)}
    assert payload["auto_match"] is True
    assert payload["reason"] == evidence.reason


def test_the_audit_reason_names_every_component_and_marks_absences_as_na() -> None:
    """`reason` 是给人看的审计串：五键齐全，**缺失写 `n/a`、零分写 `0.0`**。

    两者混淆过一次就再也不会发现"这项根本没算"，所以两边都要断言。
    """
    dated = identity.publication_match(CORPUS[6], CORPUS[7])
    fields = dict(item.split("=") for item in dated.reason.split(";"))
    assert set(fields) == {
        "title",
        "creator",
        "creator_identity_match",
        "content",
        "duration",
        "publish_delta_hours",
    }
    assert fields["content"] != "n/a" and fields["duration"] == "n/a"
    assert fields["publish_delta_hours"] == "0.0", "同一时刻是 0 小时，不是「没有」"
    assert fields["title"] == f"{dated.title_score:.3f}"
    assert fields["creator_identity_match"] == "True"

    undated = identity.publication_match(CORPUS[0], CORPUS[1])
    assert undated.publish_delta_hours is None
    assert (
        dict(item.split("=") for item in undated.reason.split(";"))["publish_delta_hours"] == "n/a"
    )

    no_creator = identity.publication_match(
        _pub("bilibili", "五个人的复盘笔记"), _pub("douyin", "五个人的复盘笔记!!")
    )
    assert no_creator.creator_score == 0.0
    assert dict(item.split("=") for item in no_creator.reason.split(";"))["creator"] == "0.000"
