#!/usr/bin/env python3
# ruff: noqa
# TODO(v2-adapt): V1 内容分析层的闸门式渲染器，未适配。它吃的三份文件全是 V1 的文件名协议：
# <out-dir>/<window>-input-pack.json（上一份脚本的产物，渲染前重算 pack_sha256 校验）、
# <window>-report.json（**由已随 CREATOR_ANALYSIS.md 一起丢失的三个 AI skill 手写**，本文件
# SCHEMA_HELP 那份 schema 是从幸存的输入包字段重建的，不是原作者契约的逐字复制）、
# <window>-report.html；语料哈希还要回到 downloads/feishu-docs/ 下的 Markdown 复核。
# 它不查 SQLite，所以是三者里搬起来最干净的，但"报告 JSON 由谁产生"这一环在 V2 没有对应物。
# 适配要做的那件事：把 report.json 定成 V2 的任务产物（TaskDefinition + FileStorage 路径），
# 那两段内联 CSS/JS 换成本仓库的设计令牌（frontend/src/styles/tokens.css）。
"""内容分析层 · 报告渲染程序（严格只读镜像，只写一份独立 HTML）

契约来源：`FEISHU_LOCAL_DATABASE.md`「内容分析层」章节。原文只留了一句可核查的约定：

    AI 写入结构化报告 JSON，`render_creator_analysis_report.py` 统一生成独立离线 HTML 最终报告。
    分析只读本地镜像，不向 SQLite 视图或飞书写回。

而 `prepare_creator_analysis.py` 写出的输入包里带着本程序的调用方式和三条写回规则：

    报告 JSON 的 schema 见 render_creator_analysis_report.py --help
    只读输入包与 content_profile，不得自行扩大样本范围
    不得改写 corpus.content_sha256 指向的语料文件

**必须说清楚的缺失**：文档称每日/三日/每周分析以 `CREATOR_ANALYSIS.md` 为唯一契约，但仓库副本里
这个文件连同 `creator-daily-analysis` / `creator-three-day-analysis` / `creator-weekly-analysis`
三个 skill 一起不存在，无法还原。因此本程序的报告 JSON schema 是**依据 surviving 输入包字段和上面
三条规则重建的**，不是原作者那份契约的逐字复制。它按内容组织而不按窗口特化：三种窗口共用一份 schema，
所以换窗口只需换 `--window`。

本程序的价值在于**闸门式渲染**：报告由 AI 手写，所以渲染前逐条核对——样本范围是否被扩大、语料哈希
是否被改动、引用的评论/指标快照是否真的存在、转写引文是否真的出现在口播稿里。任何一条不过就拒绝出报告，
而不是把幻觉排版得好看一点。

退出码
------
- `0` 校验通过，HTML 已写出
- `1` 环境或入参问题（输入包/报告 JSON 不存在、不是 JSON 对象、窗口不匹配）
- `2` 报告违反契约（越界样本、假引文、未知枚举值……逐条列出，不出报告）
- `3` 输入包自身不可信（`pack_sha256` 重算不一致、语料文件被改动或丢失）

窗口：daily=24h、three-day=72h、weekly=168h。本程序不查 SQLite——数据一律来自
`prepare_creator_analysis.py` 已经校验过的输入包，避免两条路径对同一条视频给出不同结论。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from html import escape
from pathlib import Path
from typing import Any, Iterator

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from intelligence_hub_v2.ported.v1_shared.utils import now_str  # noqa: E402

DEFAULT_OUTPUT_ROOT = ROOT / "outputs" / "creator-analysis"

WINDOWS = ("daily", "three-day", "weekly")
PACK_SCHEMA_VERSION = "creator-analysis-input-pack/v1"
REPORT_SCHEMA_VERSION = "creator-analysis-report/v1"

EXIT_OK = 0
EXIT_ENVIRONMENT = 1
EXIT_REPORT_INVALID = 2
EXIT_PACK_UNTRUSTWORTHY = 3

# 输入包里这几个字段是在算完 pack_sha256 之后才补上的，重算时必须剔除。
PACK_HASH_EXCLUDED_KEYS = (
    "pack_sha256",
    "hash_scope",
    "generated_at",
    "cache_reuse",
    "output_path",
)

# content_profile 是 prepare 程序算出来的可数事实，报告引用它时必须用这些键名。
PROFILE_BASIS_KEYS = (
    "char_count",
    "line_count",
    "body_line_count",
    "heading_count",
    "headings",
    "sentence_count",
    "avg_sentence_chars",
    "median_sentence_chars",
    "max_sentence_chars",
    "question_sentence_count",
    "number_token_count",
    "latin_token_count",
    "top_cjk_bigrams",
    "top_latin_tokens",
    "opening_sentences",
    "closing_sentences",
)

CONFIDENCE_LEVELS = ("high", "medium", "low")
PRIORITIES = ("P0", "P1", "P2")
EVIDENCE_KINDS = ("transcript", "comment", "metric", "profile", "base_field")

# 报告顶层的展示顺序，也是渲染器的章节表；未列出的键只会在「未识别章节」里出现一次。
SECTION_ORDER = (
    ("headline", "核心结论"),
    ("executive_summary", "摘要要点"),
    ("findings", "主要发现"),
    ("topics", "值得做的选题"),
    ("actions", "可执行动作"),
    ("audience_signals", "观众信号"),
    ("style_patterns", "表达与结构模式"),
    ("risks", "风险与反例"),
    ("carryaways", "值得带走的观点 / 工具"),
)

# 报告顶层允许的键；拼错章节名不会报错的话，内容就会静默消失。
KNOWN_TOP_LEVEL = frozenset(
    {k for k, _ in SECTION_ORDER}
    | {"schema_version", "window", "generated_at", "author", "pack_ref", "appendix"}
)

_WS_RE = re.compile(r"\s+")


def eprint(message: str) -> None:
    print(message, file=sys.stderr)


# --------------------------------------------------------------------------- #
# 加载与完整性
# --------------------------------------------------------------------------- #


def load_json_object(path: Path, label: str) -> tuple[dict[str, Any] | None, str]:
    """读取必须为 JSON 对象的文件。返回 (payload, 错误说明)。"""
    if not path.is_file():
        return None, f"{label}不存在：{path}"
    try:
        raw = path.read_bytes()
    except OSError as exc:
        return None, f"{label}读取失败：{path} · {exc}"
    try:
        payload = json.loads(raw.decode("utf-8"))
    except UnicodeDecodeError as exc:
        return None, f"{label}不是合法 UTF-8：{path} · {exc}"
    except json.JSONDecodeError as exc:
        return (
            None,
            f"{label}不是合法 JSON：{path} · 第 {exc.lineno} 行第 {exc.colno} 列：{exc.msg}",
        )
    if not isinstance(payload, dict):
        return None, f"{label}顶层必须是 JSON 对象，实际是 {type(payload).__name__}：{path}"
    return payload, ""


def recompute_pack_sha256(pack: dict[str, Any]) -> str:
    """按 prepare 程序的算法重算输入包哈希，用来发现包被手工改过。"""
    core = {k: v for k, v in pack.items() if k not in PACK_HASH_EXCLUDED_KEYS}
    canonical = json.dumps(core, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def resolve_corpus_path(raw: Any) -> Path | None:
    text = str(raw or "").strip()
    if not text:
        return None
    candidate = Path(text)
    if candidate.is_absolute():
        return candidate
    joined = ROOT / text.replace("\\", "/")
    if joined.is_file():
        return joined
    alt = ROOT / "downloads" / text.replace("\\", "/").lstrip("/")
    return alt if alt.is_file() else joined


def file_sha256(path: Path) -> tuple[str, int, str]:
    try:
        data = path.read_bytes()
    except OSError as exc:
        return "", 0, str(exc)
    return hashlib.sha256(data).hexdigest(), len(data), ""


class CorpusStore:
    """按视频去重读取语料文本，并复核 SHA-256 是否与输入包记录一致。"""

    def __init__(self) -> None:
        self._texts: dict[str, str] = {}
        self.bad: list[str] = []

    def check(self, video: dict[str, Any]) -> bool:
        """语料可用且哈希一致返回 True；否则记录原因并返回 False。"""
        corpus = video.get("corpus") or {}
        record_id = str(video.get("video_record_id") or "")
        expected = str(corpus.get("content_sha256") or "").strip().lower()
        path = resolve_corpus_path(corpus.get("local_markdown_path"))
        if path is None:
            self.bad.append(f"{record_id}: corpus.local_markdown_path 为空")
            return False
        if not path.is_file():
            self.bad.append(f"{record_id}: 本地语料文件不存在 {path}")
            return False
        actual, _size, error = file_sha256(path)
        if error:
            self.bad.append(f"{record_id}: 语料不可读 {path} · {error}")
            return False
        if not expected:
            self.bad.append(f"{record_id}: 输入包里 content_sha256 为空，无法校验 {path}")
            return False
        if actual != expected:
            self.bad.append(
                f"{record_id}: 语料哈希与输入包不一致 {path}\n"
                f"        输入包 content_sha256 = {expected}\n"
                f"        实际文件 sha256       = {actual}"
            )
            return False
        return True

    def text(self, video: dict[str, Any]) -> str:
        record_id = str(video.get("video_record_id") or "")
        if record_id in self._texts:
            return self._texts[record_id]
        corpus = video.get("corpus") or {}
        path = resolve_corpus_path(corpus.get("local_markdown_path"))
        text = ""
        if path is not None and path.is_file():
            try:
                text = path.read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError) as exc:
                self.bad.append(f"{record_id}: 语料按 UTF-8 解码失败 · {exc}")
        self._texts[record_id] = text
        return text


# --------------------------------------------------------------------------- #
# 报告遍历与校验
# --------------------------------------------------------------------------- #


def index_videos(pack: dict[str, Any]) -> dict[str, dict[str, Any]]:
    out: dict[str, dict[str, Any]] = {}
    for video in pack.get("videos") or []:
        if isinstance(video, dict):
            record_id = str(video.get("video_record_id") or "")
            if record_id:
                out[record_id] = video
    return out


def walk(node: Any, path: str = "$") -> Iterator[tuple[str, Any]]:
    """产出 (JSON 路径, 节点)，只走 dict / list。"""
    yield path, node
    if isinstance(node, dict):
        for key, value in node.items():
            yield from walk(value, f"{path}.{key}")
    elif isinstance(node, list):
        for index, value in enumerate(node):
            yield from walk(value, f"{path}[{index}]")


def human_location(path: str) -> str:
    """把 `$.findings[2].evidence[0]` 缩写成 `findings[2].evidence[0]`。"""
    trimmed = path[2:] if path.startswith("$.") else path.lstrip("$")
    return trimmed or "$"


def normalize_quote(text: str) -> str:
    return _WS_RE.sub("", str(text or "")).strip()


def collect_quotable(node: dict[str, Any], key: str) -> str:
    """证据里的引文允许写在 quote / excerpt / text 任一键下。"""
    for candidate in (key, "quote", "excerpt", "text"):
        value = node.get(candidate)
        if isinstance(value, str) and value.strip():
            return value
    return ""


def validate_report(
    pack: dict[str, Any],
    report: dict[str, Any],
    videos: dict[str, dict[str, Any]],
    corpora: CorpusStore,
    quote_mode: str,
    pack_hash: str,
) -> tuple[list[str], list[str], dict[str, Any]]:
    """返回 (错误列表, 警告列表, 核验统计)。错误非空就不该出报告。"""
    errors: list[str] = []
    warnings: list[str] = []
    stats = {
        "video_refs": 0,
        "comment_refs": 0,
        "snapshot_refs": 0,
        "quotes_checked": 0,
        "quotes_matched": 0,
        "profile_basis_checked": 0,
    }

    version = str(report.get("schema_version") or "").strip()
    if version != REPORT_SCHEMA_VERSION:
        errors.append(
            f"$.schema_version = {version!r}，本渲染器只认 {REPORT_SCHEMA_VERSION!r}。"
            "改窗口不换 schema；要换 schema 必须同时升级本程序。"
        )

    window = str(report.get("window") or "").strip()
    if not window:
        errors.append("缺少 $.window（daily / three-day / weekly）。")
    elif window != str(pack.get("window") or ""):
        errors.append(f"$.window={window!r} 与输入包 window={pack.get('window')!r} 不一致。")

    pack_ref = report.get("pack_ref")
    if not isinstance(pack_ref, dict):
        errors.append("缺少 $.pack_ref 对象：报告必须声明它写 against 哪一份输入包。")
    else:
        claimed = str(pack_ref.get("pack_sha256") or "").strip().lower()
        if not claimed:
            errors.append("$.pack_ref.pack_sha256 为空：无法证明报告对应这份输入包。")
        elif claimed != pack_hash:
            errors.append(
                f"$.pack_ref.pack_sha256={claimed[:16]}… 与输入包重算值 {pack_hash[:16]}… 不一致："
                "报告写的不是这份包，或包在报告生成后被重新生成过。"
            )

    if not str(report.get("headline") or "").strip():
        errors.append("缺少 $.headline：一句话结论必须存在，渲染器不补写。")
    if not any(
        isinstance(report.get(key), list) and report.get(key)
        for key in ("findings", "topics", "actions")
    ):
        errors.append(
            "$.findings / $.topics / $.actions 至少一个必须是非空数组，否则这份报告没有可交付结论。"
        )

    for key in report:
        if str(key) not in KNOWN_TOP_LEVEL:
            warnings.append(
                f"$.{key}: 未识别的顶层键，不会出现在报告中（章节名拼错会让整节静默消失）。"
            )

    for path, node in walk(report):
        location = human_location(path)
        if not isinstance(node, dict):
            continue

        if "confidence" in node:
            confidence = str(node.get("confidence") or "").strip()
            if confidence not in CONFIDENCE_LEVELS:
                errors.append(
                    f"{location}.confidence={confidence!r}，只能是 {'/'.join(CONFIDENCE_LEVELS)}。"
                )

        if "priority" in node:
            priority = str(node.get("priority") or "").strip()
            if priority not in PRIORITIES:
                errors.append(f"{location}.priority={priority!r}，只能是 {'/'.join(PRIORITIES)}。")

        if "kind" in node:
            kind = str(node.get("kind") or "").strip()
            if kind not in EVIDENCE_KINDS:
                errors.append(
                    f"{location}.kind={kind!r} 不在允许的证据类型 {list(EVIDENCE_KINDS)} 内。"
                )

        # 样本范围：source_video_record_ids 与同级 video_record_id 都必须来自输入包。
        referenced: list[str] = []
        direct = node.get("video_record_id")
        if isinstance(direct, str) and direct.strip():
            referenced.append(direct.strip())
        blob = node.get("source_video_record_ids")
        if isinstance(blob, list):
            for item in blob:
                if isinstance(item, str) and item.strip():
                    referenced.append(item.strip())
        for record_id in referenced:
            stats["video_refs"] += 1
            if record_id not in videos:
                errors.append(
                    f"{location} 引用了输入包之外的视频 record_id={record_id!r}："
                    "违反「不得自行扩大样本范围」。输入包共 "
                    f"{len(videos)} 条视频。"
                )

        # 引文核验：只有 transcript 类证据要求逐字出现在口播稿里。
        quote = collect_quotable(node, "quote")
        kind = str(node.get("kind") or "").strip()
        if quote and kind == "transcript":
            anchor = str(node.get("video_record_id") or "").strip()
            video = videos.get(anchor)
            if video is None:
                errors.append(
                    f"{location}: transcript 引文缺少可定位的 video_record_id（{anchor!r} 不在输入包内）。"
                )
            elif corpora.check(video):
                stats["quotes_checked"] += 1
                if normalize_quote(quote) in normalize_quote(corpora.text(video)):
                    stats["quotes_matched"] += 1
                else:
                    message = (
                        f"{location}: 引文在该视频口播稿里找不到原文（record_id={anchor}）："
                        f"{quote[:60]}…\n        语料 {video.get('corpus', {}).get('local_markdown_path')}"
                    )
                    if quote_mode == "strict":
                        errors.append(message)
                    elif quote_mode == "warn":
                        warnings.append(message)
            if not anchor:
                errors.append(f"{location}: transcript 证据必须与 video_record_id 同级。")

        # 评论引用必须真属于这条视频。
        comment_id = str(node.get("comment_record_id") or "").strip()
        if comment_id:
            stats["comment_refs"] += 1
            anchor = str(node.get("video_record_id") or "").strip()
            video = videos.get(anchor)
            if video is None:
                errors.append(
                    f"{location}: comment_record_id 缺少可定位的 video_record_id={anchor!r}。"
                )
            else:
                pool = {
                    str(c.get("record_id") or "")
                    for c in video.get("audience_evidence") or []
                    if isinstance(c, dict)
                }
                if comment_id not in pool:
                    errors.append(
                        f"{location}.comment_record_id={comment_id!r} 不在视频 {anchor} 的 audience_evidence 里"
                        f"（该视频共 {len(pool)} 条证据评论）。"
                    )
                elif (
                    quote
                    and collect_quotable(node, "quote")
                    and normalize_quote(quote)
                    not in normalize_quote(
                        str(_find_comment(video, comment_id).get("comment_text") or "")
                    )
                ):
                    errors.append(
                        f"{location}: 评论引文与库中 comment_text 不一致（comment_record_id={comment_id}）。"
                    )

        # 指标快照同理。
        snapshot_key = str(node.get("snapshot_key") or "").strip()
        if snapshot_key:
            stats["snapshot_refs"] += 1
            anchor = str(node.get("video_record_id") or "").strip()
            video = videos.get(anchor)
            if video is None:
                errors.append(f"{location}: snapshot_key 缺少可定位的 video_record_id={anchor!r}。")
            else:
                pool = {
                    str(s.get("snapshot_key") or "")
                    for s in video.get("metric_snapshots") or []
                    if isinstance(s, dict)
                }
                if snapshot_key not in pool:
                    errors.append(
                        f"{location}.snapshot_key={snapshot_key!r} 不在视频 {anchor} 的 metric_snapshots 里"
                        f"（可用值：{sorted(p for p in pool if p)[:8] or '（无）'}）。"
                    )

        # content_profile 依据必须是真实存在的画像键。
        basis = node.get("basis")
        if isinstance(basis, str) and basis.strip():
            stats["profile_basis_checked"] += 1
            if basis.strip() not in PROFILE_BASIS_KEYS:
                errors.append(
                    f"{location}.basis={basis.strip()!r} 不是 content_profile 的字段名，"
                    f"可用键：{', '.join(PROFILE_BASIS_KEYS)}。"
                )
        basis_list = node.get("basis_keys")
        if isinstance(basis_list, list):
            for item in basis_list:
                key_name = str(item or "").strip()
                if not key_name:
                    continue
                stats["profile_basis_checked"] += 1
                if key_name not in PROFILE_BASIS_KEYS:
                    errors.append(f"{location}.basis_keys 含非法画像键 {key_name!r}。")

    appendix = report.get("appendix")
    if isinstance(appendix, dict):
        for index, gap in enumerate(appendix.get("data_gaps") or []):
            if isinstance(gap, dict):
                record_id = str(gap.get("video_record_id") or "").strip()
                if record_id and record_id not in videos:
                    errors.append(
                        f"$.appendix.data_gaps[{index}] 引用了输入包外的视频 {record_id!r}。"
                    )

    return errors, warnings, stats


def _find_comment(video: dict[str, Any], comment_record_id: str) -> dict[str, Any]:
    for item in video.get("audience_evidence") or []:
        if isinstance(item, dict) and str(item.get("record_id") or "") == comment_record_id:
            return item
    return {}


# --------------------------------------------------------------------------- #
# HTML 渲染
# --------------------------------------------------------------------------- #

CSS = """
:root {
  --bg: #f5f7fa; --card: #ffffff; --ink: #1f2937; --muted: #6b7280;
  --line: #e5e7eb; --brand: #059669; --warn: #d97706; --bad: #dc2626;
  --chip: #eef2f7;
}
* { box-sizing: border-box; }
body { margin: 0; background: var(--bg); color: var(--ink);
  font: 14px/1.7 -apple-system, "Segoe UI", "Microsoft YaHei", sans-serif; }
a { color: var(--brand); text-decoration: none; }
a:hover { text-decoration: underline; }
.wrap { max-width: 1080px; margin: 0 auto; padding: 28px 20px 80px; }
header.report { background: var(--card); border: 1px solid var(--line); border-radius: 14px;
  padding: 22px 24px; margin-bottom: 18px; }
.eyebrow { font-size: 12px; color: var(--muted); letter-spacing: .08em; text-transform: uppercase; }
h1 { margin: 6px 0 10px; font-size: 26px; line-height: 1.35; letter-spacing: -.02em; }
.meta { display: flex; flex-wrap: wrap; gap: 8px; margin-top: 12px; }
.chip { background: var(--chip); border-radius: 999px; padding: 4px 11px; font-size: 12px; color: #374151; }
.chip.ok { background: #ecfdf5; color: #065f46; }
.chip.warn { background: #fffbeb; color: #92400e; }
.grid { display: grid; grid-template-columns: repeat(auto-fit, minmax(140px, 1fr)); gap: 10px; margin-top: 14px; }
.stat { background: #fbfcfe; border: 1px solid var(--line); border-radius: 10px; padding: 10px 12px; }
.stat b { display: block; font-size: 20px; }
.stat span { font-size: 12px; color: var(--muted); }
nav.toc { position: sticky; top: 0; z-index: 5; background: rgba(245,247,250,.94);
  backdrop-filter: blur(6px); border-bottom: 1px solid var(--line); padding: 10px 0; margin-bottom: 8px; }
nav.toc ul { display: flex; flex-wrap: wrap; gap: 6px; list-style: none; margin: 0; padding: 0; }
nav.toc a { font-size: 12.5px; padding: 4px 10px; border-radius: 8px; background: var(--card);
  border: 1px solid var(--line); color: var(--ink); }
section { background: var(--card); border: 1px solid var(--line); border-radius: 14px;
  padding: 20px 22px; margin-bottom: 16px; scroll-margin-top: 66px; }
section > h2 { margin: 0 0 14px; font-size: 18px; display: flex; align-items: center; gap: 8px; }
section > h2 .count { font-size: 12px; color: var(--muted); font-weight: 400; }
.card { border: 1px solid var(--line); border-left: 3px solid var(--brand); border-radius: 10px;
  padding: 14px 16px; margin-bottom: 12px; background: #fdfdfe; }
.card h3 { margin: 0 0 6px; font-size: 15.5px; }
.card p { margin: 6px 0; }
.card .kv { color: var(--muted); font-size: 12.5px; }
ul.tight { margin: 6px 0 0; padding-left: 20px; }
ol.tight { margin: 6px 0 0; padding-left: 20px; }
.evidence { margin-top: 10px; border-top: 1px dashed var(--line); padding-top: 10px; }
.evidence-item { background: #f8fafc; border-radius: 8px; padding: 8px 10px; margin-bottom: 7px; font-size: 13px; }
blockquote { margin: 6px 0 0; padding: 8px 12px; background: #f0fdf4; border-left: 3px solid var(--brand);
  border-radius: 0 8px 8px 0; color: #14532d; }
table { width: 100%; border-collapse: collapse; font-size: 13px; }
th, td { border-bottom: 1px solid var(--line); padding: 8px 10px; text-align: left; vertical-align: top; }
th { background: #fafbfc; font-weight: 600; color: #374151; position: sticky; top: 44px; }
tr:hover td { background: #fbfcfe; }
.tag { display: inline-block; font-size: 11.5px; padding: 1px 8px; border-radius: 999px; background: var(--chip); }
.tag.high { background: #ecfdf5; color: #065f46; }
.tag.medium { background: #eff6ff; color: #1e40af; }
.tag.low { background: #fef2f2; color: #991b1b; }
.tag.P0 { background: #fef2f2; color: #991b1b; }
.tag.P1 { background: #fffbeb; color: #92400e; }
.tag.P2 { background: #f0f9ff; color: #075985; }
.muted { color: var(--muted); }
.mono { font-family: ui-monospace, Consolas, monospace; font-size: 12px; word-break: break-all; }
.integrity dl { margin: 0; }
.integrity dt { font-size: 12px; color: var(--muted); margin-top: 10px; }
.integrity dd { margin: 2px 0 0; }
.empty { color: var(--muted); font-style: normal; padding: 10px 0; }
.toolbar { display: flex; gap: 8px; align-items: center; margin-bottom: 12px; flex-wrap: wrap; }
.toolbar button, .toolbar select { font: inherit; font-size: 12.5px; padding: 5px 10px;
  border: 1px solid var(--line); border-radius: 8px; background: var(--card); cursor: pointer; }
footer { color: var(--muted); font-size: 12px; text-align: center; margin-top: 26px; }
@media print {
  nav.toc, .toolbar { display: none; }
  body { background: #fff; }
  section, header.report { border-color: #d1d5db; break-inside: avoid; }
}
"""

JS = """
(function () {
  var root = document.getElementById('videoTable');
  if (root) {
    var picker = document.getElementById('platformFilter');
    if (picker) {
      picker.addEventListener('change', function () {
        var want = picker.value;
        root.querySelectorAll('tbody tr').forEach(function (row) {
          row.style.display = (!want || row.dataset.platform === want) ? '' : 'none';
        });
      });
    }
  }
  document.querySelectorAll('[data-collapse]').forEach(function (head) {
    head.addEventListener('click', function () {
      var target = document.getElementById(head.getAttribute('data-collapse'));
      if (target) { target.hidden = !target.hidden; }
    });
  });
})();
"""


def esc(value: Any) -> str:
    """所有外部文本进 HTML 前都必须经过这里。"""
    if value is None:
        return ""
    return escape(str(value), quote=True)


def embed_json(value: Any) -> str:
    """内嵌 JSON 时防止 `</script>` 提前闭合，也避免 U+2028/29 在部分引擎里断行。"""
    text = json.dumps(value, ensure_ascii=False, sort_keys=False)
    return (
        text.replace("<", "\\u003c")
        .replace(">", "\\u003e")
        .replace("&", "\\u0026")
        .replace("\u2028", "\\u2028")
        .replace("\u2029", "\\u2029")
    )


def chip(text: Any, tone: str = "") -> str:
    cls = f"chip {tone}".strip()
    return f'<span class="{cls}">{esc(text)}</span>'


def tag(text: Any) -> str:
    safe = re.sub(r"[^A-Za-z0-9_.-]", "", str(text or ""))
    return (
        f'<span class="tag {safe}">{esc(text)}</span>'
        if safe
        else f'<span class="tag">{esc(text)}</span>'
    )


def confidence_badge(value: Any) -> str:
    labels = {"high": "高置信", "medium": "中置信", "low": "待验证"}
    key = str(value or "").strip()
    return tag(labels.get(key, key or "未标注"))


def video_link(videos: dict[str, dict[str, Any]], record_id: Any) -> str:
    rid = str(record_id or "").strip()
    video = videos.get(rid)
    if not video:
        return f'<span class="muted mono">{esc(rid)}</span>'
    title = str(video.get("video_title") or "").strip() or rid
    return f'<a href="#video-{esc(rid)}" title="{esc(rid)}">{esc(title[:60])}</a>'


def render_evidence(items: Any, videos: dict[str, dict[str, Any]]) -> str:
    if not isinstance(items, list) or not items:
        return ""
    rows: list[str] = []
    kind_labels = {
        "transcript": "口播稿",
        "comment": "评论",
        "metric": "指标快照",
        "profile": "画像事实",
        "base_field": "Base 字段",
    }
    for item in items:
        if not isinstance(item, dict):
            rows.append(f'<div class="evidence-item">{esc(item)}</div>')
            continue
        kind = str(item.get("kind") or "").strip()
        parts = [tag(kind_labels.get(kind, kind or "证据"))]
        rid = str(item.get("video_record_id") or "").strip()
        if rid:
            parts.append(video_link(videos, rid))
        detail = item.get("detail") or item.get("point") or item.get("label")
        if detail:
            parts.append(esc(detail))
        body = " · ".join(p for p in parts if p)
        quote = collect_quotable(item, "quote")
        if quote:
            source = (
                "口播稿"
                if kind == "transcript"
                else ("评论" if item.get("comment_record_id") else "引文")
            )
            attribution = str(item.get("user_name") or "").strip()
            who = f"（{esc(attribution)}）" if attribution else ""
            body += f'<blockquote>“{esc(quote)}”<div class="kv muted">{esc(source)}{who}</div></blockquote>'
        if item.get("snapshot_key"):
            body += f'<div class="kv muted">snapshot_key={esc(item.get("snapshot_key"))}</div>'
        rows.append(f'<div class="evidence-item">{body}</div>')
    return (
        f'<div class="evidence"><div class="kv muted">证据（逐条可回查）</div>{"".join(rows)}</div>'
    )


def render_source_list(video_ids: Any, videos: dict[str, dict[str, Any]]) -> str:
    if not isinstance(video_ids, list) or not video_ids:
        return ""
    links = ", ".join(video_link(videos, rid) for rid in video_ids if str(rid).strip())
    return f'<div class="kv muted">涉及样本：{links or "（无）"}</div>' if links else ""


def section_shell(anchor: str, title: str, count: int | None, inner: str) -> str:
    badge = f' <span class="count">{count} 条</span>' if count is not None else ""
    return f'<section id="{esc(anchor)}"><h2>{esc(title)}{badge}</h2>{inner}</section>\n'


def render_findings(findings: Any, videos: dict[str, dict[str, Any]]) -> str:
    cards: list[str] = []
    for item in findings if isinstance(findings, list) else []:
        if not isinstance(item, dict):
            continue
        head = f"<h3>{esc(item.get('title') or item.get('id') or '发现')}</h3>"
        claim = (
            f"<p>{esc(item.get('claim') or item.get('summary'))}</p>"
            if (item.get("claim") or item.get("summary"))
            else ""
        )
        badges = " ".join(
            b
            for b in (
                confidence_badge(item.get("confidence")),
                tag(item.get("id")) if item.get("id") else "",
            )
            if b
        )
        action = (
            f'<p class="kv">建议动作：{esc(item.get("action"))}</p>' if item.get("action") else ""
        )
        cards.append(
            f'<div class="card">{head}<div>{badges}</div>{claim}{action}{render_source_list(item.get("source_video_record_ids"), videos)}{render_evidence(item.get("evidence"), videos)}</div>'
        )
    return "".join(cards) or '<div class="empty">本报告未给出 findings。</div>'


def render_bullets(values: Any, ordered: bool = False) -> str:
    items = [v for v in (values if isinstance(values, list) else []) if str(v or "").strip()]
    if not items:
        return '<div class="empty">无。</div>'
    body = "".join(f"<li>{esc(i)}</li>" for i in items)
    return f"<{'ol' if ordered else 'ul'} class='tight'>{body}</{'ol' if ordered else 'ul'}>"


def render_table(
    rows: Any, columns: list[tuple[str, str]], videos: dict[str, dict[str, Any]]
) -> str:
    data = [r for r in (rows if isinstance(rows, list) else []) if isinstance(r, dict)]
    if not data:
        return '<div class="empty">无。</div>'
    head = "".join(f"<th>{esc(label)}</th>" for _, label in columns)
    body: list[str] = []
    for row in data:
        cells: list[str] = []
        for key, _label in columns:
            if key == "_sources":
                cells.append(
                    f"<td>{render_source_list(row.get('source_video_record_ids'), videos) or '<span class=muted>—</span>'}</td>"
                )
            elif key == "_confidence":
                cells.append(f"<td>{confidence_badge(row.get('confidence'))}</td>")
            elif key == "_priority":
                cells.append(
                    f"<td>{tag(row.get('priority')) if row.get('priority') else '<span class=muted>—</span>'}</td>"
                )
            elif key == "_evidence":
                cells.append(
                    f"<td>{render_evidence(row.get('evidence'), videos) or '<span class=muted>—</span>'}</td>"
                )
            else:
                value = row.get(key)
                if isinstance(value, list):
                    rendered = "; ".join(str(v) for v in value if str(v or "").strip())
                else:
                    rendered = "" if value is None else str(value)
                cells.append(
                    f"<td>{esc(rendered) if rendered.strip() else '<span class=muted>—</span>'}</td>"
                )
        body.append(f"<tr>{''.join(cells)}</tr>")
    return f"<table><thead><tr>{head}</tr></thead><tbody>{''.join(body)}</tbody></table>"


def fmt_number(value: Any) -> str:
    if value is None or value == "":
        return "—"
    try:
        number = float(value)
    except (TypeError, ValueError):
        return esc(value)
    if number.is_integer():
        return f"{int(number):,}"
    return f"{number:,.2f}"


def render_video_appendix(videos: dict[str, dict[str, Any]], report: dict[str, Any]) -> str:
    if not videos:
        return '<div class="empty">输入包里没有样本。</div>'
    cited: set[str] = set()
    for _path, node in walk(report):
        if isinstance(node, dict):
            direct = node.get("video_record_id")
            if isinstance(direct, str):
                cited.add(direct.strip())
            blob = node.get("source_video_record_ids")
            if isinstance(blob, list):
                cited.update(str(x).strip() for x in blob if isinstance(x, str))
    platforms = sorted({str(v.get("platform") or "") for v in videos.values() if v.get("platform")})
    options = "".join(f'<option value="{esc(p)}">{esc(p)}</option>' for p in platforms)
    rows: list[str] = []
    for record_id in sorted(
        videos, key=lambda r: str(videos[r].get("published_at") or ""), reverse=True
    ):
        video = videos[record_id]
        profile = video.get("content_profile") or {}
        creator = video.get("creator") or {}
        corpus = video.get("corpus") or {}
        doc_url = str(corpus.get("document_url") or "").strip()
        links = ""
        if doc_url:
            links += (
                f'<a href="{esc(doc_url)}" target="_blank" rel="noreferrer noopener">飞书文档</a> '
            )
        local_path = str(corpus.get("local_markdown_path") or "").strip()
        if local_path:
            links += f'<span class="mono muted">{esc(local_path)}</span>'
        marker = "★" if record_id in cited else ""
        rows.append(
            f'<tr id="video-{esc(record_id)}" data-platform="{esc(video.get("platform"))}">'
            f"<td>{marker or '&nbsp;'}</td>"
            f"<td>{esc(video.get('published_at'))}<div class='kv muted'>{esc(video.get('platform'))} / {esc(video.get('platform_video_id'))}</div></td>"
            f"<td>{esc(video.get('video_title'))}<div class='kv muted'>{esc(creator.get('creator_name'))}"
            f"{('　' + esc(creator.get('follower_count_display'))) if creator.get('follower_count_display') else ''}</div></td>"
            f"<td class='kv'>{esc(video.get('duration_seconds'))}s<br>{fmt_number(video.get('chars_per_minute'))} 字/分</td>"
            f"<td class='kv'>{fmt_number(profile.get('sentence_count'))} 句<br>"
            f"{fmt_number(profile.get('avg_sentence_chars'))} 字/句<br>"
            f"{fmt_number(profile.get('question_sentence_count'))} 问句</td>"
            f"<td class='kv'>{esc(' / '.join(str(s) for s in (profile.get('opening_sentences') or [])[:1]))}</td>"
            f"<td class='kv'>{fmt_number(video.get('fetched_comment_count') if video.get('fetched_comment_count') is not None else len(video.get('audience_evidence') or []))} 条<br>"
            f"{fmt_number(len(video.get('metric_snapshots') or []))} 快照</td>"
            f"<td>{links}</td></tr>"
        )
    return (
        f'<div class="toolbar"><span class="muted">按平台筛选</span>'
        f'<select id="platformFilter"><option value="">全部</option>{options}</select>'
        f'<span class="muted">★ = 报告正文引用过的样本</span></div>'
        f'<table id="videoTable"><thead><tr><th>引用</th><th>发布 / 平台</th><th>标题 / 博主</th>'
        f"<th>时长</th><th>画像</th><th>开场句</th><th>证据量</th><th>语料</th></tr></thead>"
        f"<tbody>{''.join(rows)}</tbody></table>"
    )


def render_integrity(
    pack: dict[str, Any],
    report: dict[str, Any],
    stats: dict[str, Any],
    warnings: list[str],
    pack_hash: str,
    corpora: CorpusStore,
) -> str:
    prov = pack.get("data_cutoff_provenance") or {}
    reuse = pack.get("cache_reuse") or {}
    quotes = stats["quotes_checked"]
    matched = stats["quotes_matched"]
    quote_line = f"{matched} / {quotes} 条逐字命中语料" if quotes else "报告未引用 transcript 原文"
    body = f"""
<dl>
  <dt>数据截止点</dt><dd>{esc(prov.get("cutoff_at") or pack.get("data_cutoff_at"))}
      <span class="muted">（来源 {esc(prov.get("mode"))}：{esc(Path(str(prov.get("manifest_path") or "")).name or "—")}）</span></dd>
  <dt>分析窗口</dt><dd>{esc(pack.get("window"))}（{esc(pack.get("window_hours"))} 小时）
      {esc(pack.get("window_start_at"))} ~ {esc(pack.get("data_cutoff_at"))}</dd>
  <dt>输入包指纹</dt><dd class="mono">{esc(pack_hash)}
      <div class="kv muted">报告 $.pack_ref.pack_sha256 已核对一致；重算覆盖了 hash_scope 声明的全部字段。</div></dd>
  <dt>语料完整性</dt><dd>{len(pack.get("videos") or [])} 条口播稿本地 SHA-256 全部复核通过
      <div class="kv muted">缓存画像：命中 {esc(reuse.get("hit"))} / 新建 {esc(reuse.get("built"))}</div></dd>
  <dt>引文核验</dt><dd>{esc(quote_line)}
      <span class="muted">（模式 {esc(pack.get("_quote_mode"))}）</span></dd>
  <dt>引用计数</dt><dd>视频 {fmt_number(stats["video_refs"])} · 评论 {fmt_number(stats["comment_refs"])}
      · 指标快照 {fmt_number(stats["snapshot_refs"])} · 画像依据 {fmt_number(stats["profile_basis_checked"])}</dd>
</dl>
"""
    if warnings:
        body += (
            '<div class="card" style="border-left-color:var(--warn)"><h3>不阻断的提醒'
            f'（{len(warnings)} 条）</h3><ul class="tight">'
            + "".join(f"<li>{esc(w)}</li>" for w in warnings)
            + "</ul></div>"
        )
    if corpora.bad:
        body += (
            '<div class="card" style="border-left-color:var(--bad)"><h3>语料异常'
            f'（{len(corpora.bad)} 条）</h3><ul class="tight">'
            + "".join(f'<li class="mono">{esc(b)}</li>' for b in corpora.bad)
            + "</ul></div>"
        )
    gaps = (
        (report.get("appendix") or {}).get("data_gaps")
        if isinstance(report.get("appendix"), dict)
        else None
    )
    if gaps:
        body += (
            f'<dt>报告自报的数据缺口</dt><dd><ul class="tight">'
            + "".join(
                f'<li>{esc(g.get("gap"))} <span class="muted">{video_link(index_videos(pack), g.get("video_record_id")) if isinstance(g, dict) else ""}</span></li>'
                for g in gaps
                if isinstance(g, dict)
            )
            + "</ul></dd>"
        )
    missing = pack.get("missing_evidence_views") or []
    if missing:
        body += f'<div class="kv muted">镜像库缺视图：{esc("、".join(str(m) for m in missing))}，对应证据为空。</div>'
    unplaced = pack.get("unplaced_videos") or []
    if unplaced:
        body += f'<div class="kv muted">另有 {len(unplaced)} 条视频 published_at 无法解析，未参与窗口划分。</div>'
    return section_shell("integrity", "数据与核验说明", None, body)


def render_html(
    pack: dict[str, Any],
    report: dict[str, Any],
    videos: dict[str, dict[str, Any]],
    stats: dict[str, Any],
    warnings: list[str],
    pack_hash: str,
    corpora: CorpusStore,
) -> str:
    summary = pack.get("sample_summary") or {}
    title = str(report.get("headline") or f"{pack.get('window')} 内容分析报告")
    meta = "".join(
        c
        for c in (
            chip(f"窗口 {pack.get('window')}", ""),
            chip(f"样本 {summary.get('videos')} 条视频", ""),
            chip(f"{summary.get('creators')} 位博主主体", ""),
            chip(f"评论证据 {summary.get('audience_evidence_comments')}", ""),
            chip(f"指标快照 {summary.get('metric_snapshots')}", ""),
            chip(f"截止 {pack.get('data_cutoff_at')}", ""),
            chip("全部样本已校验 SHA-256", "ok"),
        )
    )
    platform_chips = "".join(
        chip(f"{name} {count}", "")
        for name, count in sorted((summary.get("platforms") or {}).items())
    )
    stats_grid = f"""
<div class="grid">
  <div class="stat"><b>{esc(summary.get("videos"))}</b><span>窗口内样本视频</span></div>
  <div class="stat"><b>{esc(summary.get("creators"))}</b><span>博主主体</span></div>
  <div class="stat"><b>{fmt_number(summary.get("total_transcript_chars"))}</b><span>口播稿总字数</span></div>
  <div class="stat"><b>{esc(stats["quotes_checked"])}</b><span>逐字核验引文</span></div>
</div>"""

    sections: list[str] = [render_integrity(pack, report, stats, warnings, pack_hash, corpora)]
    anchors = [("integrity", "数据与核验说明")]
    section_bodies: dict[str, str] = {
        "headline": f"<p>{esc(report.get('headline'))}</p>"
        + (
            f'<div class="kv muted">执笔：{esc(report.get("author"))}</div>'
            if report.get("author")
            else ""
        ),
        "executive_summary": render_bullets(report.get("executive_summary")),
        "findings": render_findings(report.get("findings"), videos),
        "topics": render_table(
            report.get("topics"),
            [
                ("topic", "选题"),
                ("rationale", "为什么现在做"),
                ("_priority", "优先级"),
                ("_sources", "样本"),
            ],
            videos,
        ),
        "actions": render_table(
            report.get("actions"),
            [
                ("action", "动作"),
                ("why", "依据"),
                ("owner", "谁来做"),
                ("due", "时点"),
                ("_sources", "样本"),
            ],
            videos,
        ),
        "audience_signals": render_table(
            report.get("audience_signals"),
            [("signal", "观众信号"), ("_evidence", "证据"), ("_confidence", "置信")],
            videos,
        ),
        "style_patterns": render_table(
            report.get("style_patterns"),
            [("pattern", "模式"), ("basis", "画像依据"), ("_sources", "样本")],
            videos,
        ),
        "risks": render_table(
            report.get("risks"), [("risk", "风险 / 反例"), ("mitigation", "应对")], videos
        ),
        "carryaways": render_table(
            report.get("carryaways"),
            [("item", "观点 / 工具"), ("why", "为什么值得带走"), ("_sources", "样本")],
            videos,
        ),
    }
    for key, label in SECTION_ORDER:
        value = report.get(key)
        if value is None or (isinstance(value, list) and not value):
            continue
        anchor = key
        sections.append(
            section_shell(
                anchor, label, len(value) if isinstance(value, list) else None, section_bodies[key]
            )
        )
        anchors.append((anchor, label))

    sections.append(
        section_shell(
            "appendix", "样本清单（输入包全量）", len(videos), render_video_appendix(videos, report)
        )
    )
    anchors.append(("appendix", "样本清单"))

    appendix = report.get("appendix")
    if isinstance(appendix, dict) and str(appendix.get("notes") or "").strip():
        sections.append(
            section_shell("notes", "附录说明", None, f"<p>{esc(appendix.get('notes'))}</p>")
        )
        anchors.append(("notes", "附录说明"))

    toc = "".join(f'<li><a href="#{esc(a)}">{esc(l)}</a></li>' for a, l in anchors)
    embedded = embed_json({"window": pack.get("window"), "video_refs": stats["video_refs"]})
    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{esc(title)}</title>
<style>{CSS}</style>
</head>
<body>
<div class="wrap">
<header class="report">
  <div class="eyebrow">对标账号内容分析 · 独立离线报告</div>
  <h1>{esc(title)}</h1>
  <div class="meta">{meta}{platform_chips}</div>
  {stats_grid}
</header>
<nav class="toc"><ul>{toc}</ul></nav>
{"".join(sections)}
<footer>
  由 <span class="mono">render_creator_analysis_report.py</span> 生成 ·
  报告 JSON {esc(report.get("schema_version"))} / 输入包 {esc(pack.get("schema_version"))} ·
  渲染时间 {esc(now_str())} · 生成时刻 {esc(report.get("generated_at") or "未声明")}<br>
  本页只读展示：不写 SQLite 视图、不写飞书、不引用任何外部资源，可离线打开。
</footer>
</div>
<script type="application/json" id="report-data">{embedded}</script>
<script>{JS}</script>
</body>
</html>
"""


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def render(args: argparse.Namespace) -> int:
    window = args.window
    out_root = Path(args.out_dir).expanduser().resolve()
    report_path = (
        Path(args.input).expanduser().resolve()
        if args.input
        else out_root / f"{window}-report.json"
    )
    pack_path = (
        Path(args.pack).expanduser().resolve()
        if args.pack
        else out_root / f"{window}-input-pack.json"
    )
    html_path = (
        Path(args.output).expanduser().resolve()
        if args.output
        else out_root / f"{window}-report.html"
    )

    pack, error = load_json_object(pack_path, "输入包")
    if error:
        eprint(
            f"[错误] {error}\n"
            f"       → 先运行：python prepare_creator_analysis.py prepare --window {window}\n"
            f"         它会在 {out_root} 下生成 {window}-input-pack.json"
        )
        return EXIT_ENVIRONMENT
    report, error = load_json_object(report_path, "报告 JSON")
    if error:
        eprint(
            f"[错误] {error}\n"
            f"       → 报告由分析任务手写，完整 schema 见本文件 --help 的「报告 JSON 契约」一节。"
        )
        return EXIT_ENVIRONMENT

    assert pack is not None and report is not None

    if str(pack.get("schema_version") or "") != PACK_SCHEMA_VERSION:
        eprint(
            f"[错误] 输入包 schema_version={pack.get('schema_version')!r}，本渲染器只认 "
            f"{PACK_SCHEMA_VERSION!r}。请重跑 prepare_creator_analysis.py 生成配套输入包。"
        )
        return EXIT_PACK_UNTRUSTWORTHY
    if str(pack.get("window") or "") != window:
        eprint(
            f"[错误] --window {window} 与输入包里的 window {pack.get('window')!r} 不一致："
            "别把三日样本当每日结论。"
        )
        return EXIT_ENVIRONMENT

    pack_hash = recompute_pack_sha256(pack)
    stored_hash = str(pack.get("pack_sha256") or "").strip().lower()
    if not stored_hash:
        eprint("[错误] 输入包没有 pack_sha256，无法判断它是否被手工改过。")
        return EXIT_PACK_UNTRUSTWORTHY
    if stored_hash != pack_hash:
        eprint(
            f"[错误] 输入包重算哈希与 pack_sha256 不一致：\n"
            f"       记录值 {stored_hash}\n"
            f"       重算值 {pack_hash}\n"
            f"       包文件被编辑过（或 prepare 与本渲染器的剔除字段不同步）：{pack_path}\n"
            f"       → 重跑 python prepare_creator_analysis.py prepare --window {window}"
        )
        return EXIT_PACK_UNTRUSTWORTHY

    videos = index_videos(pack)
    corpora = CorpusStore()
    if not args.skip_corpus_check:
        broken = [v for rid, v in videos.items() if not corpora.check(v)]
        if broken or corpora.bad:
            eprint(
                f"[错误] 语料完整性核验失败：{len(corpora.bad)} 条哈希不一致或不可读，"
                "违反「不得改写 corpus.content_sha256 指向的语料文件」，不出报告：\n"
                + "\n".join(f"  - {b}" for b in corpora.bad[:20])
            )
            eprint(
                "  → 补齐后重跑：python run_feishu_local_sync.py，再重跑 prepare_creator_analysis.py。"
            )
            return EXIT_PACK_UNTRUSTWORTHY

    pack["_quote_mode"] = args.quote_mode
    errors, warnings, stats = validate_report(
        pack, report, videos, corpora, args.quote_mode, pack_hash
    )
    if errors:
        eprint(
            f"[报告不合格] 共 {len(errors)} 处违反契约，拒绝生成 HTML"
            "（先把这些改掉，再重跑本命令）：\n"
        )
        for index, message in enumerate(errors, 1):
            eprint(f"  ({index}/{len(errors)}) {message}")
        return EXIT_REPORT_INVALID

    html = render_html(pack, report, videos, stats, warnings, pack_hash, corpora)
    html_path.parent.mkdir(parents=True, exist_ok=True)
    html_path.write_text(html, encoding="utf-8")

    print(
        f"[完成] 报告：{html_path}\n"
        f"       样本 {len(videos)} 条 · 引用核验 视频 {stats['video_refs']} / 评论 {stats['comment_refs']}"
        f" / 快照 {stats['snapshot_refs']} · 引文 {stats['quotes_matched']}/{stats['quotes_checked']} 逐字命中"
    )
    if warnings:
        print(f"       注意：{len(warnings)} 条不阻断提醒，已列在报告「数据与核验说明」里。")
    return EXIT_OK


SCHEMA_HELP = """\
报告 JSON 契约（creator-analysis-report/v1）
-------------------------------------------
报告由分析任务手写，本渲染器只做校验 + 排版，不补写任何结论。

{
  "schema_version": "creator-analysis-report/v1",
  "window": "daily | three-day | weekly",          // 必须等于输入包的 window
  "generated_at": "YYYY-MM-DD HH:MM:SS",
  "author": "creator-daily-analysis",              // 可选，写分析的 skill 名
  "pack_ref": { "pack_sha256": "<输入包的 pack_sha256>", "path": "..." },
  "headline": "一句话结论",                         // 必填
  "executive_summary": ["要点", "..."],
  "findings": [{
    "id": "F1", "title": "...", "claim": "...",
    "confidence": "high | medium | low",
    "action": "...",
    "source_video_record_ids": ["<必须在输入包 videos[] 里>"],
    "evidence": [
      { "kind": "transcript", "video_record_id": "...", "quote": "口播稿里的原文", "detail": "..." },
      { "kind": "comment",    "video_record_id": "...", "comment_record_id": "...", "quote": "comment_text 原文" },
      { "kind": "metric",     "video_record_id": "...", "snapshot_key": "...", "detail": "播放 12.4 万" },
      { "kind": "profile",    "video_record_id": "...", "basis": "avg_sentence_chars", "detail": "..." }
    ]
  }],
  "topics":   [{ "topic": "...", "rationale": "...", "priority": "P0|P1|P2", "source_video_record_ids": [] }],
  "actions":  [{ "action": "...", "why": "...", "owner": "...", "due": "...", "source_video_record_ids": [] }],
  "audience_signals": [{ "signal": "...", "confidence": "high", "evidence": [ ... ] }],
  "style_patterns":   [{ "pattern": "...", "basis": "opening_sentences", "source_video_record_ids": [] }],
  "risks":            [{ "risk": "...", "mitigation": "..." }],
  "carryaways":       [{ "item": "...", "why": "...", "source_video_record_ids": [] }],
  "appendix": { "data_gaps": [{ "video_record_id": "...", "gap": "..." }], "notes": "..." }
}

硬规则（违反任意一条即退出码 2，不出报告）
------------------------------------------
1. 所有 video_record_id / source_video_record_ids 必须来自输入包 videos[]，不得扩大样本范围。
2. kind=transcript 的 quote 必须逐字出现在该视频语料里（忽略空白差异）。--quote-mode warn 可降级为提醒。
3. comment_record_id 必须属于同一对象 video_record_id 那条视频的 audience_evidence；
   同时给出 quote 时，引文必须是该条 comment_text 的子串。
4. snapshot_key 必须出现在同一对象的 video_record_id 那条视频的 metric_snapshots 里。
5. basis / basis_keys 必须是 content_profile 的字段名：
   char_count, line_count, body_line_count, heading_count, headings, sentence_count,
   avg_sentence_chars, median_sentence_chars, max_sentence_chars, question_sentence_count,
   number_token_count, latin_token_count, top_cjk_bigrams, top_latin_tokens,
   opening_sentences, closing_sentences
6. confidence 只能是 high/medium/low；priority 只能是 P0/P1/P2；kind 只能是
   transcript/comment/metric/profile/base_field。
7. $.pack_ref.pack_sha256 必须等于输入包 pack_sha256。
8. 引用一律与它要说明的对象同级（把 video_record_id 写进同一个 evidence 对象），渲染器不做跨层推断。

未识别的顶层键只会变成一条提醒，不会渲染进报告——拼错章节名是这种手写 JSON 最常见的失误。
"""


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="render_creator_analysis_report.py",
        description="内容分析层：把 AI 手写的报告 JSON 校验后渲染成独立离线 HTML（只读输入包，不查库、不写飞书）。",
        epilog=SCHEMA_HELP,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--window", required=True, choices=WINDOWS, help="分析窗口，决定默认文件名")
    parser.add_argument(
        "--input", default="", help="报告 JSON 路径（默认 <out-dir>/<window>-report.json）"
    )
    parser.add_argument(
        "--pack", default="", help="输入包路径（默认 <out-dir>/<window>-input-pack.json）"
    )
    parser.add_argument(
        "--output", default="", help="HTML 输出路径（默认 <out-dir>/<window>-report.html）"
    )
    parser.add_argument(
        "--out-dir",
        default=str(DEFAULT_OUTPUT_ROOT),
        help=f"默认目录（默认 {DEFAULT_OUTPUT_ROOT.relative_to(ROOT)}），与 prepare 程序保持一致",
    )
    parser.add_argument(
        "--quote-mode",
        choices=("strict", "warn", "off"),
        default="strict",
        help="transcript 引文逐字核验力度：strict 不过即失败（默认）/ warn 只提醒 / off 跳过",
    )
    parser.add_argument(
        "--skip-corpus-check",
        action="store_true",
        help="跳过语料文件哈希复核（仅用于渲染流程调试；正式出报告不要用）",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return int(render(args))


if __name__ == "__main__":
    raise SystemExit(main())
