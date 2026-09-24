"""转写之后的本地归一与抽取式参考材料（V1 `postprocess_platform_videos.py` 的那三个函数）。

**纯本地算法，一个 LLM 都不接**（V1 的规矩，也是这一格的全部意义）：
只搬运原文片段，不生成结论性摘要 —— 文件里第一行就写着"本文件性质"。

三个函数各自挡的是 V1 真踩过的一类事故：

- `normalize_transcript()`：字幕文件里的 `字幕BY爱好者` 署名与 CRLF 空行会一路传染到
  抽取结果与前端稿子。只做归一，**不改写语义**。
- `split_sentences()`：下游按标点断句（§7.9 补的标点就是它的输入），短于
  `SENTENCE_MIN_CHARS` 的碎片是换气与识别毛刺，留着只会把候选句挤满"嗯"。
- `extractive_reference()`：给博主看的那份 `reference.md` 与两个字段。

`MIN_TRANSCRIPT_CHARS`（V1 的 `--min-transcript-chars`，默认 30）是**反幻觉闸的第二层**：
`asr/engine.py` 那道能量闸拦住了纯音/静音，但真噪声（键盘、风噪、音乐垫底又起伏足够大）
还是会换来一串"我。我。我。"。数的是**非空白字符**，不是 len —— 换行与空格不该算内容。

一条与计划不符的实测（2026-09-24，本机 V1 库 21 条作品）：V1 的
`user_pain_points` 与 `expandable_topics` 两列**全空（0/21）**，`content_summary` 13/21、
`key_points` 11/21 非空。也就是说 V1 这条本地链路只产出"摘要 + 要点"两样，
计划里那句"摘要/要点/痛点/选题"是把 V2.2 分析层（爆款拆解 / 脚本生成）的东西
提前算到了这一格。这里照 V1 实际产出的做，痛点/选题归 V2.2。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

__all__ = [
    "KEYWORD_HINTS",
    "MIN_TRANSCRIPT_CHARS",
    "SENTENCE_MIN_CHARS",
    "ReferenceMaterial",
    "extractive_reference",
    "normalize_transcript",
    "speech_length",
    "split_sentences",
]

KEYWORD_HINTS: tuple[str, ...] = (
    "AI",
    "模型",
    "智能体",
    "工具",
    "流程",
    "方法",
    "案例",
    "数据",
    "内容",
    "视频",
    "产品",
    "代码",
)
"""候选句打分用的提示词（V1 原表，一字未改）。改它等于改"哪些句子会被挑出来"。"""

SENTENCE_MIN_CHARS = 12
"""短于此的句子不进候选：换气、"嗯啊"、识别毛刺都在这段长度里（V1 同值）。"""

MIN_TRANSCRIPT_CHARS = 30
"""非空白字符少于此就判定"这条没有口播"（V1 `--min-transcript-chars` 的默认值）。"""

MAX_SUMMARY_CHARS = 600
"""`content_summary` 与"原文开头"那一段的长度上限（V1 同值）。"""

KEY_POINT_LIMIT = 8
"""最多挑几句（V1 同值）。"""

_BLANK_RE = re.compile(r"\s+")
_SENTENCE_SPLIT_RE = re.compile(r"(?<=[。！？!?；;])\s*")
_CREDIT_RES = (
    re.compile(r"字幕\s*(?:by|BY|By)?\s*.*"),
    re.compile(r".*(?:字幕|翻译)\s*(?:by|BY|By)\s*.*"),
)
"""字幕组署名行。它们在稿子里没有任何下游用途，却会被抽取算法当成"内容句"挑进候选。"""


def speech_length(text: str) -> int:
    """非空白字符数。`MIN_TRANSCRIPT_CHARS` 与 `has_speech` 都问这一个数。"""
    return len(_BLANK_RE.sub("", str(text or "")))


def normalize_transcript(text: str) -> str:
    """压空白、丢字幕署名、合并空行；**只做归一，不改写语义**。"""
    lines: list[str] = []
    seen_blank = False
    for raw in str(text or "").splitlines():
        line = _BLANK_RE.sub(" ", raw).strip()
        if not line:
            if not seen_blank and lines:
                lines.append("")
            seen_blank = True
            continue
        seen_blank = False
        if any(credit.fullmatch(line) for credit in _CREDIT_RES):
            continue
        lines.append(line)
    collapsed = re.sub(r"\n{3,}", "\n\n", "\n".join(lines))
    return collapsed.strip()


def split_sentences(text: str) -> list[str]:
    """按标点断句，丢掉短于 `SENTENCE_MIN_CHARS` 的碎片。"""
    parts = _SENTENCE_SPLIT_RE.split(str(text or "").replace("\n", " "))
    return [part.strip() for part in parts if len(part.strip()) >= SENTENCE_MIN_CHARS]


@dataclass(frozen=True)
class ReferenceMaterial:
    """`extractive_reference()` 的产出。字段名与 V1 写进库的那几个对齐。

    `markdown` 是落盘的那份文件正文；`summary_method` 恒为 `"local-extractive"` ——
    它是"这不是 LLM 写的"这件事的凭据，将来接生成式摘要时必须能被区分开，
    否则没人知道哪几篇稿子是模型编的。
    """

    markdown: str
    key_points: str
    content_summary: str
    summary_method: str = "local-extractive"

    def as_dict(self) -> dict[str, Any]:
        return {
            "markdown": self.markdown,
            "key_points": self.key_points,
            "content_summary": self.content_summary,
            "summary_method": self.summary_method,
        }


def score_sentences(sentences: list[str]) -> list[str]:
    """提示词命中数 + 越靠前越高分（`5 - index*0.05`，V1 原式），返回按原文顺序的候选句。"""
    scored: list[tuple[float, int]] = []
    for index, sentence in enumerate(sentences):
        lowered = sentence.lower()
        hits = sum(1 for key in KEYWORD_HINTS if key.lower() in lowered)
        scored.append((hits + max(0.0, 5 - index * 0.05), index))
    picked = sorted(scored, key=lambda item: (-item[0], item[1]))[:KEY_POINT_LIMIT]
    return [sentences[index] for _, index in sorted(picked, key=lambda item: item[1])]


def extractive_reference(
    *,
    title: str,
    video_id: str,
    platform: str,
    url: str,
    cleaned_text: str,
    note_text: str = "",
) -> ReferenceMaterial:
    """本地抽取式参考材料：只搬运原文片段，不生成结论。"""
    selected = score_sentences(split_sentences(cleaned_text))
    body = cleaned_text.strip() or note_text.strip()
    note = note_text.strip()

    lines: list[str] = [
        f"# {title or video_id}",
        "",
        f"- 平台: {platform}",
        f"- 平台作品ID: {video_id}",
        f"- 链接: {url}",
        "- 本文件性质: 本地自动抽取参考，不是最终内容摘要或关键要点",
    ]
    if note:
        lines += ["", "## 作品原文案", "", note]
    if body:
        lines += ["", "## 自动抽取参考：原文开头", "", body[:MAX_SUMMARY_CHARS].strip()]
    if selected:
        lines += ["", "## 自动抽取参考：候选句"] + [f"- {sentence}" for sentence in selected]

    return ReferenceMaterial(
        markdown="\n".join(lines).strip() + "\n",
        key_points="\n".join(f"- {sentence}" for sentence in selected),
        content_summary=" ".join(cleaned_text.split())[:MAX_SUMMARY_CHARS].strip()
        or note[:MAX_SUMMARY_CHARS],
    )
