"""爆款拆解引擎：规则表的**活性**与产出的一致性关系。

信条：断言写成关系，不写成样本；每条不变量配一个防空转的前置断言。

这一份里最要紧的是 `test_every_hook_pattern_is_reachable` 与
`test_every_role_is_assigned`：模式匹配类代码最常见的失效方式不是算错，而是**某条规则
被悄悄删掉 / 被前面的规则遮住**，然后所有用例照样绿。所以两条都做成
"从表本身参数化 + 条数前置断言 + 只命中这一条的前置断言"三件套。
"""

from __future__ import annotations

import re

import pytest

from intelligence_hub_v2.core.analysis.benchmark_engine import (
    AVOID_RULES,
    BORROW_RULES,
    CTA_TAIL_RATIO,
    DENSITY_HIGH_CHARS,
    DENSITY_HIGH_LABEL,
    DENSITY_STANDARD_LABEL,
    HOOK_PATTERNS,
    PROGRESS_BANDS,
    SCREEN_EVIDENCE_WORDS,
    SENTENCE_ROLES,
    STRUCTURE_BEAT_TEXT,
    BenchmarkAnalysis,
    HookBreakdown,
    analyze_benchmark,
    classify_sentence,
    split_transcript_lines,
)

NEUTRAL_TITLE = "那件小事"
"""一个 5 条钩子模式都不命中的标题：让"命中谁"只由首句决定。"""


def _hook(first_line: str) -> HookBreakdown:
    """只要钩子那一段的便利函数：标题固定成中性值，命中与否只由这一行决定。"""
    return analyze_benchmark(title=NEUTRAL_TITLE, transcript_text=first_line).hook


# 每条钩子模式一个**只有它能命中**的输入（前置断言会核这一点）。
HOOK_SAMPLES: dict[str, str] = {
    "痛点反常识": "你有没有发现自己每天都在手动整理稿子",
    "强承诺教程": "如何三步把这套配置跑通",
    "认知反转断言": "千万别再手写脚本了",
    "高价值资源清单": "这个插件免费还开源",
    "第一人称实战复盘": "我做了整整一个月的复盘",
}

# 一个六行都命中不了关键词的句子 —— 用来走进度兜底三档。
KEYWORD_FREE_LINE = "窗外的雨一直下个不停"


def test_the_rule_tables_have_not_been_thinned() -> None:
    """前置断言：表被删一行，参数化就少一条用例 —— 没有这一条就没人发现。

    5/7/3/4/3 是 V1 原表的条数（`launcher/engine/benchmark_engine.py`）。
    """
    assert len(HOOK_PATTERNS) == 5
    assert len(SENTENCE_ROLES) == 7
    assert len(PROGRESS_BANDS) == 3
    assert len(STRUCTURE_BEAT_TEXT) == 4
    assert len(BORROW_RULES) == 4
    assert len(AVOID_RULES) == 3
    assert len(HOOK_SAMPLES) == len(HOOK_PATTERNS)


@pytest.mark.parametrize("pattern", HOOK_PATTERNS, ids=lambda p: p.hook_type)
def test_every_hook_pattern_is_reachable(pattern) -> None:
    """每条钩子模式都存在一个能让它赢的输入，并且拆解结果真的带上它的文案。"""
    sample = HOOK_SAMPLES[pattern.hook_type]
    winners = [p.hook_type for p in HOOK_PATTERNS if re.search(p.pattern, sample)]
    assert winners == [pattern.hook_type], "这条样本被别的规则命中，测不到本条"

    result = analyze_benchmark(title=NEUTRAL_TITLE, transcript_text=sample)
    assert result.hook.type == pattern.hook_type
    assert result.hook.description == pattern.description
    assert result.hook.sentence == sample


def test_a_line_that_matches_nothing_falls_back_to_the_suspense_type() -> None:
    result = analyze_benchmark(title=NEUTRAL_TITLE, transcript_text=KEYWORD_FREE_LINE)
    assert result.hook.type == "悬念吸引型"
    assert result.hook.description == "开篇通过抛出核心思考引发观众好奇心与停留意愿。"


def test_the_title_is_a_second_chance_for_the_hook() -> None:
    """首句不中但标题中 → 仍按标题那条模式定性（V1 的 `or re.search(..., title)`）。"""
    result = analyze_benchmark(
        title=HOOK_SAMPLES["高价值资源清单"], transcript_text=KEYWORD_FREE_LINE
    )
    assert result.hook.type == "高价值资源清单"


# (role_key, 行, index, total) —— total=5 时 index=1 既不是首行也不在末段，只看关键词。
ROLE_SAMPLES: list[tuple[str, str, int, int]] = [
    ("hook", "随便一句话首行恒为钩子", 0, 5),
    ("problem", "最大的痛点在这里", 1, 5),
    ("concept", "先看清本质", 1, 5),
    ("solution", "跟着步骤做一遍", 1, 5),
    ("proof", "先看这段演示", 1, 5),
    ("bonus", "还有一个避坑点", 1, 5),
    ("cta", "收尾这一句位置决定一切", 4, 5),
]


def test_every_role_row_is_assignable() -> None:
    """前置：职能表 7 行都有对应样本，且样本彼此不撞（撞了就有一行测不到）。"""
    assert len(ROLE_SAMPLES) == len(SENTENCE_ROLES)
    assert [row[0] for row in ROLE_SAMPLES] == [role.role_key for role in SENTENCE_ROLES]


@pytest.mark.parametrize(
    ("role_key", "line", "index", "total"),
    ROLE_SAMPLES,
    ids=[row[0] for row in ROLE_SAMPLES],
)
def test_every_role_is_assigned(role_key: str, line: str, index: int, total: int) -> None:
    entry = next(role for role in SENTENCE_ROLES if role.role_key == role_key)
    assert classify_sentence(line=line, index=index, total=total) == (
        entry.role_key,
        entry.job,
        entry.visual,
        entry.reusable,
    )


def test_position_beats_keywords_at_both_ends() -> None:
    """首行/末两行由位置定性，关键词表在这两处不参与（V1 的 `[1:6]` 切片就是这个意思）。"""
    cta_words = "总结收藏关注下期评论区领取源码试试建议一起来"
    assert classify_sentence(line=cta_words, index=0, total=9)[0] == "hook"
    assert classify_sentence(line=cta_words, index=8, total=9)[0] == "cta"
    # 中段：cta/hook 两行的关键词表**从来没被读过**，所以这一行进进度兜底、没有 role_key
    assert classify_sentence(line=cta_words, index=1, total=9)[0] is None
    # 尾段判据的两条各挡一段：`total - 2` 在比例还没到 0.9 时就得生效（10 行里的第 9 行）
    assert classify_sentence(line=KEYWORD_FREE_LINE, index=8, total=10)[0] == "cta"
    assert CTA_TAIL_RATIO > 8 / 9, "样本没跨过比例那条线，上面那句断言在空转"
    assert classify_sentence(line=KEYWORD_FREE_LINE, index=7, total=10)[0] is None


def test_the_three_progress_bands_cover_the_progress_axis() -> None:
    """没命中关键词的行按进度兜底，三档各有一个位置能落到。"""
    total = 10
    got = [
        classify_sentence(line=KEYWORD_FREE_LINE, index=index, total=total) for index in (1, 5, 7)
    ]
    assert [band.job for band in PROGRESS_BANDS] == [item[1] for item in got]
    assert all(item[0] is None for item in got), "兜底档没有 role_key（V1 把键丢了）"


def test_lines_are_split_verbatim_and_comments_dropped() -> None:
    lines = split_transcript_lines("# 元信息\n\n第一句。\n  \n  第二句。  \n#收尾")
    assert lines == ["第一句。", "第二句。"]
    assert split_transcript_lines("") == []


def test_line_by_line_mirrors_the_transcript_itself() -> None:
    """逐句拆解**不许增删改句子**：数量、顺序、原文一一对上。"""
    transcript = "\n".join(
        [
            "你有没有发现每天都在手动整理稿子",
            "这里的痛点是重复劳动太多",
            "跟着步骤做一遍就好",
            "最后记得收藏起来",
        ]
    )
    result = analyze_benchmark(title=NEUTRAL_TITLE, transcript_text=transcript)
    assert [line.sentence for line in result.line_by_line] == transcript.splitlines()
    assert [line.index for line in result.line_by_line] == [1, 2, 3, 4]
    assert result.total_chars == sum(len(line) for line in transcript.splitlines())


def test_timestamps_start_at_zero_and_never_go_backwards() -> None:
    transcript = "\n".join([f"第{index}句的口播内容长这样" for index in range(1, 9)])
    result = analyze_benchmark(
        title=NEUTRAL_TITLE, transcript_text=transcript, duration_seconds=120
    )
    stamps = [line.timestamp for line in result.line_by_line]
    assert stamps[0] == "00:00"
    assert stamps == sorted(stamps), "时间戳按累计语速推进，不能倒流"
    assert len(stamps) == len(transcript.splitlines())


def test_the_timeline_floors_at_thirty_seconds_and_stretches_with_duration() -> None:
    """缺时长时按 4.2 字/秒推；给了时长则整条时间轴摊到那个长度，而 30 秒是地板。"""
    transcript = "\n".join(["这一句足够长可以用来推进时间轴" for _ in range(4)])
    no_duration = analyze_benchmark(title=NEUTRAL_TITLE, transcript_text=transcript)
    eight = analyze_benchmark(title=NEUTRAL_TITLE, transcript_text=transcript, duration_seconds=8)
    thirty = analyze_benchmark(title=NEUTRAL_TITLE, transcript_text=transcript, duration_seconds=30)
    longer = analyze_benchmark(
        title=NEUTRAL_TITLE, transcript_text=transcript, duration_seconds=300
    )
    assert no_duration.line_by_line[0].timestamp == "00:00"
    assert no_duration.line_by_line[-1].timestamp != "00:00", "默认语速没生效"
    assert eight.line_by_line == thirty.line_by_line, "30 秒以下的时长不该改变时间轴"
    assert longer.line_by_line[-1].timestamp > thirty.line_by_line[-1].timestamp
    assert no_duration.line_by_line[-1].timestamp < longer.line_by_line[-1].timestamp


def test_punch_score_switches_on_the_first_line_length() -> None:
    short = "一二三四五六七八九十" * 2  # 20 字
    long = "一二三四五六七八九十" * 4  # 40 字
    assert _hook(short).punch_score == 92
    assert _hook(long).punch_score == 85
    assert len(_hook(short).sentence) == 20


def test_promise_is_the_first_line_until_it_is_too_short() -> None:
    assert _hook("今天就讲一件事").promise == "今天就讲一件事"
    assert _hook("短句").promise == f"通过《{NEUTRAL_TITLE}》向观众拆解高效方法与实战路径"
    assert _hook("这一句话说完了，。").promise == "这一句话说完了"


def test_visible_evidence_follows_the_title_keywords() -> None:
    result = analyze_benchmark(title="这个插件怎么用", transcript_text=KEYWORD_FREE_LINE)
    assert result.hook.visible_evidence.startswith("真实产品/插件界面高清录屏")
    plain = analyze_benchmark(title=NEUTRAL_TITLE, transcript_text=KEYWORD_FREE_LINE)
    assert plain.hook.visible_evidence.startswith("真人近景半身出镜")
    # 标题里出现 "工具" 不算屏幕证据（`SCREEN_EVIDENCE_WORDS` 那张表就是没有它）
    assert "工具" not in SCREEN_EVIDENCE_WORDS
    tool = analyze_benchmark(title="这套工具怎么用", transcript_text=KEYWORD_FREE_LINE)
    assert tool.hook.visible_evidence == plain.hook.visible_evidence


def test_the_dos_and_donts_are_spec_constants_not_per_video_findings() -> None:
    """V1 的这个函数收下三个参数却一个都不用 —— V2 写成常量，行为照旧。"""
    first = analyze_benchmark(title="甲", transcript_text="你有没有试过")
    second = analyze_benchmark(title="乙丙丁", transcript_text="另一份完全不同的稿子内容")
    assert first.dos_and_donts.borrow == list(BORROW_RULES)
    assert second.dos_and_donts == first.dos_and_donts
    assert len(second.dos_and_donts.avoid) == 3


def test_structure_beats_ignore_the_duration_only_density_reads_input() -> None:
    """四段 beats 的时间轴是硬编码的（V1 里 `duration_sec` 从未被读）；只有密度看字数。"""
    short = analyze_benchmark(
        title=NEUTRAL_TITLE, transcript_text="你有没有试过", duration_seconds=300
    )
    long = analyze_benchmark(
        title=NEUTRAL_TITLE,
        transcript_text="\n".join(
            ["这一行足够长用来把字数堆过八百这个阈值" * 2 for _ in range(30)]
        ),
        duration_seconds=30,
    )
    assert short.structure_progression.beats == long.structure_progression.beats
    assert long.structure_progression.beats[0].time_range == "00:00 - 00:15"
    assert long.structure_progression.density == "极高密度信息流（每8-10秒提供一个新认知/操作成果）"
    assert short.structure_progression.density == "标准紧凑叙事（节奏平稳，适合教程理解）"


def test_the_density_label_switches_exactly_at_the_documented_threshold() -> None:
    """阈值两侧各取一个**字面量**样本：800 字算"标准"，801 字算"极高密度"。

    样本长度刻意写成常数而不是引用 `DENSITY_HIGH_CHARS` —— 引用它的话，把阈值改成
    任何值这条用例都会跟着搬家，永远测不出"阈值被挪动"（变异检查实测出来过）。
    """

    def build(chars: int) -> str:
        line = "字" * 40
        rows = [line] * (chars // 40)
        rest = chars - sum(len(row) for row in rows)
        if rest:
            rows.append("字" * rest)
        return "\n".join(rows)

    assert DENSITY_HIGH_CHARS == 800, "阈值被挪动过（V1 原值 800）"
    at = analyze_benchmark(title=NEUTRAL_TITLE, transcript_text=build(800))
    above = analyze_benchmark(title=NEUTRAL_TITLE, transcript_text=build(801))
    assert (at.total_chars, above.total_chars) == (800, 801)
    assert at.structure_progression.density == DENSITY_STANDARD_LABEL
    assert above.structure_progression.density == DENSITY_HIGH_LABEL


def test_handoff_mode_and_template_come_from_the_two_documented_rules() -> None:
    quick = analyze_benchmark(
        title=NEUTRAL_TITLE, transcript_text="你有没有试过", duration_seconds=60
    )
    assert quick.handoff.recommended_mode == "SHORT"
    # 时长够长、字数也够多 → LONG
    verbose = analyze_benchmark(
        title=NEUTRAL_TITLE,
        transcript_text="\n".join(
            ["这一行的长度是为了把总字数推过五百这条线" * 2 for _ in range(20)]
        ),
        duration_seconds=600,
    )
    assert verbose.total_chars >= 500
    assert verbose.handoff.recommended_mode == "LONG"
    # 但 SHORT 的判据是"或"：长视频只要字少照样 SHORT
    sparse = analyze_benchmark(
        title=NEUTRAL_TITLE, transcript_text="你有没有试过", duration_seconds=600
    )
    assert sparse.handoff.recommended_mode == "SHORT"

    tutorial = analyze_benchmark(title="如何学教程", transcript_text=KEYWORD_FREE_LINE)
    assert tutorial.handoff.recommended_template == "教程型收藏闭环"
    assert sparse.handoff.recommended_template == "判断先于界面"
    assert sparse.handoff.core_contradiction == sparse.hook.promise
    assert sparse.handoff.source_title == NEUTRAL_TITLE


def test_key_takeaways_are_the_first_three_reusable_sentences_in_order() -> None:
    """V1 原判据 `"可复用" in reusable` 只落在 **▲ 那一档**（实测，不是设计）。

    ★ 那几行写的是"极高复用"，不含"可复用"三个连续字，所以 `key_takeaways` 结构性地
    只会收 problem / concept / 进度兜底一档的句子，hook / solution / proof / cta 永远进不了
    交接包。这条用例把这个形状钉住。
    """
    lines = [
        "你有没有发现自己每天都在手动整理稿子",  # 0 hook：★ 极高复用
        "最大的痛点是重复劳动太多",  # 1 problem：▲ 结构可复用
        "问题在于没有人愿意改流程",  # 2 problem：▲
        "先看清本质这回事",  # 3 concept：▲ 观点可复用
        "所谓区别就在这里",  # 4 concept：▲
        KEYWORD_FREE_LINE,  # 5 进度兜底二档：★
        KEYWORD_FREE_LINE,  # 6 同上
        KEYWORD_FREE_LINE,  # 7 进度兜底三档：★
        "最后记得收藏起来",  # 8 尾段 cta：★
        "再说一句收尾的话",  # 9 尾段 cta：★
    ]
    result = analyze_benchmark(title=NEUTRAL_TITLE, transcript_text="\n".join(lines))
    reusable = [line.sentence for line in result.line_by_line if "可复用" in line.reusable]
    assert len(reusable) == 4, "样本没铺够可复用句子，这条用例在空转"
    assert result.handoff.key_takeaways == reusable[:3] == [lines[1], lines[2], lines[3]]
    assert lines[4] not in result.handoff.key_takeaways, "前 3 条这个上限没生效"
    star_rows = [line for line in result.line_by_line if line.reusable.startswith("★")]
    assert star_rows, "样本里没有 ★ 档句子，下面那句断言在空转"
    assert all(line.sentence not in result.handoff.key_takeaways for line in star_rows)


def test_an_empty_transcript_is_not_an_error_but_a_finding() -> None:
    """稿子存在却没有正文（全注释）→ 兜底结构，与"没有稿子"（路由的 404）是两件事。"""
    result = analyze_benchmark(
        title="只有一行注释的稿子", transcript_text="# 转写失败\n\n   \n", creator="  ", platform=""
    )
    assert isinstance(result, BenchmarkAnalysis)
    assert result.line_by_line == []
    assert result.total_chars == 0
    assert result.duration_seconds == 0
    assert (result.creator, result.platform) == ("未知创作者", "短视频")
    assert result.title == "只有一行注释的稿子"
    assert result.hook.type == "基础标题"
    assert result.hook.punch_score == 60
    assert result.dos_and_donts.borrow == []
    assert result.structure_progression.beats == []
    assert result.handoff.key_takeaways == []


def test_the_blank_fields_fall_back_to_the_three_labels() -> None:
    result = analyze_benchmark(title="   ", transcript_text="短句")
    assert result.title == "未命名作品"
    assert result.hook.promise == "通过《未命名作品》向观众拆解高效方法与实战路径"
    assert (result.creator, result.platform) == ("未知创作者", "短视频")


def _hook(first_line: str) -> object:
    result = analyze_benchmark(title=NEUTRAL_TITLE, transcript_text=first_line)
    return result.hook
