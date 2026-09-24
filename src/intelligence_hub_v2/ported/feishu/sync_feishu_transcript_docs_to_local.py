#!/usr/bin/env python3
# ruff: noqa
# TODO(v2-adapt): V1 的口播稿语料镜像，未适配。它读的是**镜像库**（DB_PATH 在 import 期就等于
# base_sync.DEFAULT_DB_PATH，语料表 DDL 的真源也在 base_sync 里 —— 两份必须一起搬才 import 得通），
# 产物落在 <ROOT>/downloads/feishu-docs/<record_id>/revisions/，幂等键编在文件名里（revision_id +
# extractor_version + SHA-256），lark-cli 的文档命令由 FEISHU_DOC_*_ARGS 环境变量按 `{token}` 模板
# 拼。适配要做的那件事：语料并进 V2 的 transcripts 表 + FileStorage.transcript_path，删掉这层平行
# 目录，只留 Docx 块→Markdown 的抽取（_BLOCK_TYPE_MAP 那套才是它有价值的部分）。
# 形状说明：ruff 的全局豁免写在第 2 行（第 1 行留给 shebang，否则 `./x.py` 跑不了）。
"""飞书文档「口播稿（可读版）」章节 -> 本地纯净 Markdown 语料镜像。

契约来源：``FEISHU_LOCAL_DATABASE.md`` :54-60、:140-158。

* 输入：``videos_readable.transcript_document_url``（Base 的 ``视频口播稿`` 字段只存 Docx 链接）
* 输出：``downloads/feishu-docs/<video_record_id>/revisions/`` 下的纯净 Markdown，
  文件名同时包含飞书 ``revision_id``、提取器版本与纯净内容 SHA-256
* 语料表：``video_transcript_mirrors``（列定义见 :56），可读视图
  ``video_transcript_mirrors_readable``；表与视图的 DDL 唯一真源在
  :mod:`sync_feishu_base_to_local`，本脚本只读写数据，不另立一份定义。
* 只提取唯一的 ``口播稿（可读版）`` 章节，保留文档标题、口播稿主标题、小节标题与正文；
  不保存基本信息、内容摘要、关键要点、智能体说明、来源说明、原始 ASR（:58）。
* 幂等：以 ``revision_id`` 判断源文档是否变化；**每次**都校验本地文件的 SHA-256 与字节数；
  revision 未变但本地文件被改/丢失、或 ``extractor_version`` 落后时重新生成（:60）。
* 章节缺失 / 重复 / 为空 -> 直接报错；单条文档失败 -> 保留上一份成功语料并把状态标 ``error``（:60）。
* 文档链接被清空 -> 状态 ``orphaned``，不删本地文件（:158）。
* 飞书读取主动节流；只对 ``retryable=true`` 的错误做有限退避，每次重试都打印到终端；
  重试耗尽仍然非零退出，不吞错（:158）。
* 每次运行生成 ``downloads/manifests/{ts_slug()}-feishu-transcript-mirror.json``。

真实抓取需要 ``feishu-base-config.json`` 与 ``lark-cli``；缺失时本脚本给出修复提示并非零退出，
绝不伪造同步成功。不依赖飞书的部分（视图/语料表建库、幂等判定、章节抽取、本地哈希校验）
可以通过 :func:`main` 的 ``--init-schema`` 以及模块内纯函数离线复现验证。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shlex
import sqlite3
import sys
import time
from dataclasses import dataclass, field as dc_field
from datetime import datetime
from pathlib import Path
from typing import Any, Sequence

ROOT: Path = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from intelligence_hub_v2.ported.feishu import feishu_core as fc  # noqa: E402
from intelligence_hub_v2.ported.feishu import lark_cli_runtime  # noqa: E402
from intelligence_hub_v2.ported.v1_shared.durable_progress import emit_durable  # noqa: E402

# 视图与语料表 DDL 的唯一真源在 Base 同步脚本里，这里只做复用（文档 :27）。
from intelligence_hub_v2.ported.feishu import sync_feishu_base_to_local as base_sync  # noqa: E402

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

EXTRACTOR_VERSION = 1
CONTENT_SCOPE = "readable_transcript"
DB_PATH: Path = base_sync.DEFAULT_DB_PATH
DOCS_ROOT: Path = ROOT / "downloads" / "feishu-docs"
MANIFEST_ROOT: Path = ROOT / "downloads" / "manifests"
LOG_DIR: Path = ROOT / "downloads" / "logs"
REVISIONS_DIRNAME = "revisions"

#: 唯一可读版章节标题（全角/半角括号、空格差异都归一化后比较）
READABLE_SECTION_TITLES = ("口播稿（可读版）", "口播稿(可读版)")
#: 语料里明确不允许出现的小节（文档 :58）
EXCLUDED_SUBSECTION_TITLES = (
    "基本信息",
    "内容摘要",
    "关键要点",
    "智能体说明",
    "来源说明",
    "原始asr",
    "原始 asr",
    "asr原文",
)
#: 主动节流：相邻飞书请求的最小间隔（秒）
DEFAULT_REQUEST_INTERVAL = 0.4
#: 单篇文档请求超时（秒）
DEFAULT_DOC_TIMEOUT = 90
#: 最多重试次数由 lark_cli_runtime.credential_retry_delays() 决定
MANIFEST_RECORD_LIMIT = 500

#: lark-cli 命令模板（可用同名环境变量覆盖，以适配不同 lark-cli 版本的子命令名）。
#: 模板里 ``{token}`` 会被文档 token 替换；``--format json`` 必须保留。
DOC_META_ARGS_ENV = "FEISHU_DOC_META_ARGS"
DOC_BLOCKS_ARGS_ENV = "FEISHU_DOC_BLOCKS_ARGS"
DOC_RAW_ARGS_ENV = "FEISHU_DOC_RAW_ARGS"
WIKI_NODE_ARGS_ENV = "FEISHU_WIKI_NODE_ARGS"
DOC_META_ARGS_DEFAULT = "docs +doc-get --as user --doc-token {token} --format json"
DOC_BLOCKS_ARGS_DEFAULT = "docs +doc-block-list --as user --doc-token {token} --format json"
DOC_RAW_ARGS_DEFAULT = "docs +doc-raw-content --as user --doc-token {token} --format json"
WIKI_NODE_ARGS_DEFAULT = "wiki +node-get --as user --token {token} --format json"

#: 飞书 docx block_type -> (kind, heading level)
_BLOCK_TYPE_MAP: dict[int, tuple[str, int]] = {
    1: ("title", 0),
    2: ("text", 0),
    12: ("bullet", 0),
    13: ("ordered", 0),
    14: ("code", 0),
    15: ("quote", 0),
    17: ("todo", 0),
    19: ("callout", 0),
    22: ("table", 0),
}
for _heading_type in range(3, 12):  # 3..11 == heading1..heading9
    _BLOCK_TYPE_MAP[_heading_type] = ("heading", _heading_type - 2)

_DOCX_URL_RE = re.compile(r"/(?:docx|docs|file)/([A-Za-z0-9_-]+)", re.I)
_WIKI_URL_RE = re.compile(r"/wiki/([A-Za-z0-9_-]+)", re.I)


class TranscriptSetupError(RuntimeError):
    """配置 / lark-cli 缺失，属于可修复的环境问题。"""


class DocumentFetchError(RuntimeError):
    """单篇飞书文档读取失败（重试已耗尽或错误不可重试）。"""

    def __init__(self, message: str, *, retryable: bool = False) -> None:
        super().__init__(message)
        self.retryable = retryable


class TranscriptLayoutError(RuntimeError):
    """文档结构不符合契约：章节缺失 / 重复 / 为空（文档 :60）。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class DocBlock:
    kind: str  # title | heading | text | bullet | ordered | code | quote | todo | callout | table | other
    level: int  # 仅 heading 有意义：1..9
    text: str

    @property
    def is_heading(self) -> bool:
        return self.kind == "heading" and self.level > 0


@dataclass
class Document:
    document_id: str
    revision_id: str
    title: str
    blocks: list[DocBlock] = dc_field(default_factory=list)
    source_text: str = ""  # 参与 source_content_sha256 计算的规范化源内容


# ---------------------------------------------------------------------------
# 通用工具
# ---------------------------------------------------------------------------


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_text(text: str) -> str:
    return sha256_bytes(text.encode("utf-8"))


def normalize_title(text: Any) -> str:
    value = str(text or "").strip().lower()
    value = value.replace("（", "(").replace("）", ")")
    return re.sub(r"\s+", "", value)


def is_readable_section(text: Any) -> bool:
    return normalize_title(text) in {normalize_title(item) for item in READABLE_SECTION_TITLES}


def is_excluded_section(text: Any) -> bool:
    value = normalize_title(text)
    return any(normalize_title(item) in value for item in EXCLUDED_SUBSECTION_TITLES)


def safe_dir_segment(value: str) -> str:
    """把 record_id 变成安全的目录名（飞书 record_id 本身就是 [A-Za-z0-9]+）。"""
    text = re.sub(r"[^A-Za-z0-9_.-]", "_", str(value or "").strip())
    return text.strip("._") or "unknown"


def relative_posix(path: Path) -> str:
    return base_sync.relative_posix(path)


# ---------------------------------------------------------------------------
# 章节抽取（纯函数，可离线验证）
# ---------------------------------------------------------------------------


def extract_readable_transcript(document: Document) -> str:
    """从规范化文档块里抽出唯一的 ``口播稿（可读版）`` 章节并渲染纯净 Markdown。

    章节缺失 / 重复 / 为空时抛 :class:`TranscriptLayoutError`（文档 :60 要求直接报错）。
    """
    heading_indexes = [index for index, block in enumerate(document.blocks) if block.is_heading]
    matches = [
        index for index in heading_indexes if is_readable_section(document.blocks[index].text)
    ]
    if not matches:
        found = [document.blocks[index].text for index in heading_indexes][:12]
        raise TranscriptLayoutError(
            "missing_readable_section",
            f"文档缺少唯一章节 {'、'.join(READABLE_SECTION_TITLES)}；现有标题={found}",
        )
    if len(matches) > 1:
        raise TranscriptLayoutError(
            "duplicate_readable_section",
            f"章节 {'、'.join(READABLE_SECTION_TITLES)} 出现 {len(matches)} 次，无法确定唯一可读版",
        )
    start = matches[0]
    section = document.blocks[start]
    end = len(document.blocks)
    for index in heading_indexes:
        if index > start and document.blocks[index].level <= section.level:
            end = index
            break
    body = document.blocks[start + 1 : end]

    lines: list[str] = []
    if document.title.strip():
        lines.append(f"# {document.title.strip()}")
        lines.append("")
    lines.append(f"## {section.text.strip()}")
    lines.append("")

    skipped = False
    emitted = 0
    index = 0
    while index < len(body):
        block = body[index]
        if block.is_heading and is_excluded_section(block.text):
            # 跳过整个子树：直到出现同级或更高级标题
            level = block.level
            skip_from = index + 1
            skip_to = len(body)
            for probe in range(skip_from, len(body)):
                if body[probe].is_heading and body[probe].level <= level:
                    skip_to = probe
                    break
            skipped = True
            index = skip_to
            continue
        rendered = _render_block(block, section.level)
        if rendered is not None:
            lines.extend(rendered)
            emitted += 1
        index += 1

    text = "\n".join(lines).rstrip() + "\n"
    if (
        emitted == 0
        or not text[
            len(f"# {document.title.strip()}\n\n" if document.title.strip() else "") :
        ].strip()
    ):
        raise TranscriptLayoutError(
            "empty_readable_section",
            f"章节 {'、'.join(READABLE_SECTION_TITLES)} 内没有可提取正文",
        )
    if not re.sub(r"^#\s+.*$|^##\s+.*$", "", text, flags=re.M).strip():
        raise TranscriptLayoutError(
            "empty_readable_section",
            f"章节 {'、'.join(READABLE_SECTION_TITLES)} 只有标题没有正文",
        )
    if skipped:
        emit_note_once(
            "已在可读版章节内剔除契约禁止的小节（基本信息/内容摘要/关键要点/智能体说明/来源说明/原始 ASR）"
        )
    return text


_NOTES_EMITTED: set[str] = set()


def emit_note_once(note: str) -> None:
    """同类提示只打印一次，但全部留档进 manifest.notes 供事后追溯。"""
    if note in _NOTES_EMITTED:
        return
    _NOTES_EMITTED.add(note)
    print(f"[note] {note}", flush=True)


def collected_notes() -> list[str]:
    return sorted(_NOTES_EMITTED)


def _render_block(block: DocBlock, section_level: int) -> list[str] | None:
    text = block.text.strip()
    if block.is_heading:
        if not text:
            return None
        # 相对重排：章节主标题固定为 ##，其下小节从 ### 起
        depth = 2 + max(1, block.level - max(section_level, 1))
        depth = min(depth, 6)
        return ["", "#" * depth + f" {text}", ""]
    if not text:
        return None
    if block.kind == "bullet":
        return [f"- {text}"]
    if block.kind == "ordered":
        return [f"1. {text}"]
    if block.kind == "quote":
        return [f"> {text}"]
    if block.kind == "todo":
        return [f"- [ ] {text}"]
    if block.kind == "code":
        return ["", "```", text, "```", ""]
    if block.kind == "callout":
        return [f"> {text}"]
    return [text, ""]


# ---------------------------------------------------------------------------
# 飞书文档解析（纯函数 + lark-cli 调用）
# ---------------------------------------------------------------------------


def find_first(value: Any, keys: Sequence[str], *, depth: int = 0) -> Any:
    """在任意嵌套 JSON 里按 key 顺序找第一个非空值（对 lark-cli 返回结构差异保持宽容）。"""
    if depth > 6 or not isinstance(value, (dict, list)):
        return None
    if isinstance(value, dict):
        for key in keys:
            found = value.get(key)
            if found not in (None, "", [], {}):
                return found
        for child in value.values():
            found = find_first(child, keys, depth=depth + 1)
            if found not in (None, "", [], {}):
                return found
        return None
    for child in value:
        found = find_first(child, keys, depth=depth + 1)
        if found not in (None, "", [], {}):
            return found
    return None


def harvest_text(value: Any, *, depth: int = 0) -> str:
    """递归收集块里的可读文本（text_run.content / plain_text / text / name）。"""
    if depth > 8:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "".join(harvest_text(item, depth=depth + 1) for item in value)
    if isinstance(value, dict):
        for key in ("content", "plain_text", "text", "name"):
            child = value.get(key)
            if isinstance(child, str) and child.strip():
                return child
        parts: list[str] = []
        for key, child in value.items():
            if key in {"style", "block_id", "parent_id", "children", "options", "finished"}:
                continue
            if isinstance(child, (dict, list)):
                parts.append(harvest_text(child, depth=depth + 1))
        return "".join(parts)
    return ""


def block_kind_and_level(block: dict[str, Any]) -> tuple[str, int]:
    raw_type: Any = None
    for key in ("block_type", "type", "blockType"):
        if block.get(key) is not None:
            raw_type = block[key]
            break
    if isinstance(raw_type, str):
        lowered = raw_type.strip().lower()
        match = re.match(r"heading[_\s-]*(\d)", lowered)
        if match:
            return "heading", min(max(int(match.group(1)), 1), 9)
        if lowered in {"title", "page"}:
            return "title", 0
        known = {
            "text",
            "bullet",
            "ordered",
            "code",
            "quote",
            "todo",
            "callout",
            "table",
            "image",
            "bitable",
        }
        if lowered in known:
            return (lowered if lowered != "page" else "title"), 0
        return "other", 0
    if isinstance(raw_type, bool):
        return "other", 0
    if isinstance(raw_type, int):
        kind, level = _BLOCK_TYPE_MAP.get(raw_type, ("other", 0))
        return kind, level
    # 没有显式类型时按 payload 键推断
    for key in block:
        match = re.match(r"heading[_]?(\d)", str(key), re.I)
        if match:
            return "heading", min(max(int(match.group(1)), 1), 9)
    return "other", 0


def normalize_blocks(raw_blocks: Sequence[Any]) -> list[DocBlock]:
    blocks: list[DocBlock] = []
    for item in raw_blocks:
        if not isinstance(item, dict):
            text = str(item or "").strip()
            if text:
                blocks.append(DocBlock("text", 0, text))
            continue
        kind, level = block_kind_and_level(item)
        text = harvest_text(
            {key: value for key, value in item.items() if key not in {"block_id", "parent_id"}}
        )
        blocks.append(DocBlock(kind, level, str(text or "").strip()))
    return blocks


def parse_markdown_blocks(text: str) -> list[DocBlock]:
    """raw-content 兜底解析：Markdown 标题优先，纯文本时把章节名单独一行当标题。"""
    blocks: list[DocBlock] = []
    has_md_heading = False
    for line in str(text or "").splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        match = re.match(r"^(#{1,6})\s+(.*)$", stripped)
        if match:
            has_md_heading = True
            blocks.append(
                DocBlock("heading", min(max(len(match.group(1)), 1), 9), match.group(2).strip())
            )
            continue
        bullet = re.match(r"^[-*+]\s+(.*)$", stripped)
        if bullet:
            blocks.append(DocBlock("bullet", 0, bullet.group(1).strip()))
            continue
        ordered = re.match(r"^\d+[.)]\s+(.*)$", stripped)
        if ordered:
            blocks.append(DocBlock("ordered", 0, ordered.group(1).strip()))
            continue
        if not has_md_heading and is_readable_section(stripped):
            blocks.append(DocBlock("heading", 1, stripped))
            continue
        blocks.append(DocBlock("text", 0, stripped))
    return blocks


def build_document(payload: Any, *, document_id: str) -> Document:
    """把 lark-cli 的文档响应规范化成 :class:`Document`。"""
    data = (
        payload.get("data")
        if isinstance(payload, dict) and isinstance(payload.get("data"), dict)
        else payload
    )
    blocks_raw: Any = find_first(data, ("items", "blocks", "data", "children"))
    revision = find_first(
        data, ("revision_id", "document_revision_id", "latest_revision_id", "revision", "version")
    )
    title = find_first(data, ("title", "doc_title", "document_title"))
    raw_content = find_first(data, ("raw_content", "content", "plain_text", "markdown"))
    blocks: list[DocBlock] = []
    if isinstance(blocks_raw, list) and blocks_raw:
        blocks = normalize_blocks(blocks_raw)
    elif isinstance(raw_content, str) and raw_content.strip():
        blocks = parse_markdown_blocks(raw_content)
    elif isinstance(raw_content, list):
        blocks = normalize_blocks(raw_content)
    if not isinstance(title, str) or not title.strip():
        page = next((block for block in blocks if block.kind == "title" and block.text), None)
        title = page.text if page else ""
    kept = [block for block in blocks if block.kind != "title"]
    if not any(block.is_heading for block in kept) and not any(block.text for block in kept):
        raise TranscriptLayoutError("empty_document", f"文档 {document_id} 未返回任何可解析内容")
    source_text = canonical_blocks_for_hash(kept, str(title or ""))
    return Document(
        document_id=document_id,
        revision_id=str(revision).strip() if revision is not None else "",
        title=str(title or "").strip(),
        blocks=kept,
        source_text=source_text,
    )


def canonical_blocks_for_hash(blocks: Sequence[DocBlock], title: str) -> str:
    return json.dumps(
        {"title": title, "blocks": [[block.kind, block.level, block.text] for block in blocks]},
        ensure_ascii=False,
        sort_keys=True,
    )


def parse_document_ref(url: str) -> tuple[str, str]:
    """从飞书链接解析 (kind, token)：docx / wiki / doc / 其它。"""
    text = str(url or "").strip()
    if not text:
        return "", ""
    match = _DOCX_URL_RE.search(text)
    if match:
        kind = "docx" if "/docx/" in match.group(0).lower() else "doc"
        return kind, match.group(1)
    wiki = _WIKI_URL_RE.search(text)
    if wiki:
        return "wiki", wiki.group(1)
    token = re.fullmatch(r"[A-Za-z0-9_-]{15,}", text)
    if token:
        return "docx", text
    return "unknown", text


# ---------------------------------------------------------------------------
# lark-cli 调用：节流 + 只对 retryable 错误退避
# ---------------------------------------------------------------------------


class LarkDocClient:
    def __init__(
        self, config: dict[str, Any], *, log_path: Path, request_interval: float, timeout: int
    ) -> None:
        self.config = config
        self.log_path = log_path
        self.request_interval = max(0.0, float(request_interval))
        self.timeout = int(timeout)
        self.delays = list(lark_cli_runtime.credential_retry_delays())
        self._last_request_at = 0.0
        self.request_count = 0
        self.retry_count = 0

    def _argv(self, template: str, token: str) -> list[str]:
        parts = shlex.split(template.replace("{token}", token), posix=False)
        argv = ["lark-cli", "--profile", str(self.config.get("profile") or "default")]
        argv.extend(part.strip('"') for part in parts)
        return fc.normalize_command(argv)

    def _throttle(self) -> None:
        elapsed = time.monotonic() - self._last_request_at
        if elapsed < self.request_interval:
            time.sleep(self.request_interval - elapsed)
        self._last_request_at = time.monotonic()

    def call(self, template: str, token: str, *, label: str) -> dict[str, Any]:
        argv = self._argv(template, token)
        attempt = 0
        last_detail = ""
        while True:
            self._throttle()
            self.request_count += 1
            result = lark_cli_runtime.run_lark_cli_command(
                argv,
                cwd=ROOT,
                env=fc.command_env(),
                timeout=self.timeout,
                check=False,
                retry_delays=(),  # 关闭底层重试，由本层统一节流与退避
            )
            self._last_request_at = time.monotonic()
            if result.returncode == 0:
                try:
                    payload = fc.safe_json_from_stdout(result.stdout)
                except (RuntimeError, ValueError) as exc:
                    raise DocumentFetchError(f"{label} 返回的不是 JSON：{str(exc)[:300]}") from exc
                if payload.get("ok") is False:
                    error = payload.get("error") if isinstance(payload.get("error"), dict) else {}
                    message = f"{label} 返回 ok=false: {json.dumps(error or payload, ensure_ascii=False)[:500]}"
                    raise DocumentFetchError(message, retryable=bool(error.get("retryable")))
                return payload
            classification = lark_cli_runtime.classify_lark_cli_failure(result)
            last_detail = str(result.stderr or result.stdout or "")[-500:]
            if not classification.get("retryable"):
                raise DocumentFetchError(
                    f"{label} 失败（不可重试，exit={result.returncode}）：{last_detail}",
                    retryable=False,
                )
            if attempt >= len(self.delays):
                raise DocumentFetchError(
                    f"{label} 重试 {attempt} 次后仍失败（exit={result.returncode}）：{last_detail}",
                    retryable=True,
                )
            delay = float(self.delays[attempt])
            attempt += 1
            self.retry_count += 1
            emit_durable(
                f"[retry] {label}：可重试错误（{classification.get('category')}），"
                f"第 {attempt}/{len(self.delays)} 次退避 {delay:g}s 后重跑；{last_detail[:200]}",
                log_path=self.log_path,
            )
            time.sleep(delay)


# ---------------------------------------------------------------------------
# 镜像状态读写
# ---------------------------------------------------------------------------

MIRROR_UPSERT_SQL = f"""
INSERT INTO {base_sync.TRANSCRIPT_MIRROR_TABLE} (
    {", ".join(base_sync.TRANSCRIPT_MIRROR_COLUMNS)}
) VALUES ({", ".join("?" * len(base_sync.TRANSCRIPT_MIRROR_COLUMNS))})
ON CONFLICT(video_record_id) DO UPDATE SET
    document_url = excluded.document_url,
    document_id = excluded.document_id,
    revision_id = excluded.revision_id,
    local_markdown_path = excluded.local_markdown_path,
    content_scope = excluded.content_scope,
    extractor_version = excluded.extractor_version,
    content_sha256 = excluded.content_sha256,
    content_bytes = excluded.content_bytes,
    source_content_sha256 = excluded.source_content_sha256,
    source_content_bytes = excluded.source_content_bytes,
    sync_status = excluded.sync_status,
    error_message = excluded.error_message,
    checked_at = excluded.checked_at,
    mirrored_at = excluded.mirrored_at,
    source_video_modified_at = excluded.source_video_modified_at
"""


@dataclass
class MirrorRow:
    video_record_id: str
    document_url: str
    source_video_modified_at: str | None
    revision_id: str | None = None
    document_id: str | None = None
    local_markdown_path: str | None = None
    content_sha256: str | None = None
    content_bytes: int | None = None
    extractor_version: int | None = None
    sync_status: str | None = None
    source_content_sha256: str | None = None
    source_content_bytes: int | None = None
    mirrored_at: str | None = None
    error_message: str | None = None
    checked_at: str | None = None

    def as_params(self, **overrides: Any) -> tuple[Any, ...]:
        values: dict[str, Any] = {
            "video_record_id": self.video_record_id,
            "document_url": self.document_url,
            "document_id": self.document_id,
            "revision_id": self.revision_id,
            "local_markdown_path": self.local_markdown_path,
            "content_scope": CONTENT_SCOPE,
            "extractor_version": self.extractor_version,
            "content_sha256": self.content_sha256,
            "content_bytes": self.content_bytes,
            "source_content_sha256": self.source_content_sha256,
            "source_content_bytes": self.source_content_bytes,
            "sync_status": self.sync_status,
            "error_message": self.error_message,
            "checked_at": self.checked_at,
            "mirrored_at": self.mirrored_at,
            "source_video_modified_at": self.source_video_modified_at,
        }
        values.update(overrides)
        return tuple(values[name] for name in base_sync.TRANSCRIPT_MIRROR_COLUMNS)


SELECT_VIDEOS_SQL = """
SELECT v.record_id AS video_record_id,
       IFNULL(v.transcript_document_url, '') AS document_url,
       v.feishu_modified_at AS source_video_modified_at,
       m.revision_id AS revision_id,
       m.document_id AS document_id,
       m.local_markdown_path AS local_markdown_path,
       m.content_sha256 AS content_sha256,
       m.content_bytes AS content_bytes,
       m.extractor_version AS extractor_version,
       m.sync_status AS sync_status,
       m.mirrored_at AS mirrored_at
FROM videos_readable v
LEFT JOIN video_transcript_mirrors_readable m ON m.video_record_id = v.record_id
ORDER BY CASE WHEN IFNULL(v.transcript_document_url, '') = '' THEN 1 ELSE 0 END,
         v.published_at DESC, v.record_id
-- LIMIT -1 在 SQLite 里表示不限制，因此本 SQL 始终带一个绑定参数（参数化 SQL，不做字符串拼接）。
LIMIT ?
"""


def load_targets(conn: sqlite3.Connection, max_records: int | None) -> list[MirrorRow]:
    limit = int(max_records) if max_records and max_records > 0 else -1
    rows = conn.execute(SELECT_VIDEOS_SQL, (limit,)).fetchall()
    targets: list[MirrorRow] = []
    for row in rows:
        targets.append(
            MirrorRow(
                video_record_id=str(row["video_record_id"]),
                document_url=str(row["document_url"] or ""),
                source_video_modified_at=row["source_video_modified_at"],
                revision_id=str(row["revision_id"])
                if row["revision_id"] not in (None, "")
                else None,
                document_id=str(row["document_id"])
                if row["document_id"] not in (None, "")
                else None,
                local_markdown_path=row["local_markdown_path"],
                content_sha256=row["content_sha256"],
                content_bytes=int(row["content_bytes"])
                if row["content_bytes"] is not None
                else None,
                extractor_version=int(row["extractor_version"])
                if row["extractor_version"] is not None
                else None,
                sync_status=row["sync_status"],
                mirrored_at=row["mirrored_at"],
            )
        )
    if max_records:
        targets = targets[:max_records]
    return targets


def verify_local_copy(
    local_path: str | None, expected_sha: str | None, expected_bytes: int | None
) -> tuple[bool, str]:
    """每次运行都校验本地文件的 SHA-256 与字节数（文档 :60）。"""
    if not local_path:
        return False, "missing_local_path"
    path = (
        (ROOT / Path(str(local_path)))
        if not Path(str(local_path)).is_absolute()
        else Path(str(local_path))
    )
    if not path.is_file():
        return False, "local_file_missing"
    try:
        data = path.read_bytes()
    except OSError as exc:
        return False, f"local_file_unreadable: {exc}"
    if expected_bytes is not None and len(data) != int(expected_bytes):
        return False, "local_bytes_mismatch"
    digest = sha256_bytes(data)
    if expected_sha and digest != expected_sha:
        return False, "local_sha_mismatch"
    return True, ""


def decide_refresh(
    row: MirrorRow, *, force: bool, revision_id: str | None, local_ok: bool, local_reason: str
) -> tuple[bool, str]:
    """是否需要重新拉取源文档：revision 变化 / 本地异常 / 提取器落后 / --force。"""
    if force:
        return True, "force"
    if row.extractor_version is None or int(row.extractor_version) < EXTRACTOR_VERSION:
        return True, "extractor_outdated"
    if not local_ok:
        return True, local_reason or "local_invalid"
    if not row.revision_id:
        return True, "no_stored_revision"
    if revision_id and str(revision_id) != str(row.revision_id):
        return True, "revision_changed"
    return False, "up_to_date"


def revision_file_name(revision_id: str, content_sha256: str) -> str:
    """文件名含 revision_id + 提取器版本 + 纯净内容哈希（文档 :142）。"""
    safe_revision = re.sub(r"[^A-Za-z0-9_.-]", "_", str(revision_id or "unknown"))
    return f"rev{safe_revision}-v{EXTRACTOR_VERSION}-{content_sha256}.md"


def write_markdown(video_record_id: str, revision_id: str, markdown: str) -> tuple[str, int, Path]:
    """原子写入纯净语料：文件名即 revision + 提取器版本 + 内容哈希。"""
    target_dir = DOCS_ROOT / safe_dir_segment(video_record_id) / REVISIONS_DIRNAME
    target_dir.mkdir(parents=True, exist_ok=True)
    data = markdown.encode("utf-8")
    digest = sha256_bytes(data)
    path = target_dir / revision_file_name(revision_id, digest)
    if path.is_file():
        if path.read_bytes() == data:
            return digest, len(data), path
        # 文件名里就带内容哈希，同名不同内容只可能是本地文件被改坏；直接原子覆盖自愈，
        # 并把异常留痕（文档 :60 要求每次校验本地 SHA/字节数，坏了要能修复而不是卡住）。
        emit_note_once(f"检测到本地语料与内容哈希不符，已原子覆盖修复：{relative_posix(path)}")
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_bytes(data)
    os.replace(tmp, path)
    return digest, len(data), path


# ---------------------------------------------------------------------------
# 单条视频处理
# ---------------------------------------------------------------------------


def process_row(
    conn: sqlite3.Connection,
    client: LarkDocClient,
    row: MirrorRow,
    *,
    force: bool,
    log_path: Path,
) -> dict[str, Any]:
    now = base_sync.now_text()
    result: dict[str, Any] = {
        "video_record_id": row.video_record_id,
        "document_url": row.document_url,
        "action": "",
        "sync_status": row.sync_status or "",
        "revision_id": row.revision_id,
        "local_markdown_path": row.local_markdown_path,
        "error": "",
    }

    def commit(**overrides: Any) -> None:
        params = row.as_params(
            error_message=overrides.pop("error_message", None),
            checked_at=now,
            **overrides,
        )
        conn.execute("BEGIN IMMEDIATE")
        try:
            conn.execute(MIRROR_UPSERT_SQL, params)
            conn.commit()
        except Exception:
            conn.rollback()
            raise

    url = row.document_url.strip()
    if not url:
        # 链接被清空：只标 orphaned，不删本地文件（文档 :158）
        if row.sync_status and row.sync_status != "orphaned":
            commit(
                sync_status="orphaned",
                error_message="飞书视频记录的口播稿链接已清空，本地语料保留为孤儿文件",
            )
            result.update(action="orphaned", sync_status="orphaned")
        elif not row.sync_status:
            result.update(action="skipped_no_document", sync_status="")
        else:
            commit(sync_status="orphaned")
            result.update(action="already_orphaned", sync_status="orphaned")
        return result

    kind, token = parse_document_ref(url)
    if not token or kind == "unknown":
        message = f"无法从链接解析飞书文档 token：{url[:200]}"
        commit(sync_status="error", error_message=message)
        result.update(action="error", sync_status="error", error=message)
        return result
    document_id = token
    if kind == "wiki":
        try:
            document_id = resolve_wiki_document(client, token)
        except (DocumentFetchError, RuntimeError) as exc:
            message = f"wiki 节点无法解析为文档 token：{exc}"
            commit(sync_status="error", error_message=message[:1000])
            result.update(action="error", sync_status="error", error=message)
            return result

    local_ok, local_reason = verify_local_copy(
        row.local_markdown_path, row.content_sha256, row.content_bytes
    )
    remote_revision: str | None = None
    meta_supported = True
    try:
        meta = client.call(
            os.environ.get(DOC_META_ARGS_ENV, DOC_META_ARGS_DEFAULT),
            document_id,
            label=f"doc-meta {document_id}",
        )
        remote_revision = (
            str(
                find_first(
                    meta,
                    (
                        "revision_id",
                        "document_revision_id",
                        "latest_revision_id",
                        "revision",
                        "version",
                    ),
                )
                or ""
            ).strip()
            or None
        )
    except DocumentFetchError as exc:
        if exc.retryable:
            message = f"轻量读取 revision 失败：{exc}"
            commit(sync_status="error", error_message=message[:1000])
            result.update(action="error", sync_status="error", error=message)
            return result
        meta_supported = False
        emit_note_once(f"doc-meta 命令不可用（{str(exc)[:160]}），改用完整读取判断 revision")

    if (
        meta_supported
        and not force
        and row.revision_id
        and remote_revision
        and str(remote_revision) == str(row.revision_id)
    ):
        should_refresh, reason = decide_refresh(
            row,
            force=force,
            revision_id=remote_revision,
            local_ok=local_ok,
            local_reason=local_reason,
        )
        if not should_refresh:
            commit(
                sync_status="ok",
                revision_id=row.revision_id,
                document_id=document_id,
                document_url=url,
                error_message=None,
            )
            result.update(action="unchanged", sync_status="ok", revision_id=row.revision_id)
            return result
        result["refresh_reason"] = reason

    blocks_template = os.environ.get(DOC_BLOCKS_ARGS_ENV, DOC_BLOCKS_ARGS_DEFAULT)
    raw_template = os.environ.get(DOC_RAW_ARGS_ENV, DOC_RAW_ARGS_DEFAULT)
    try:
        payload = client.call(blocks_template, document_id, label=f"doc-blocks {document_id}")
        document = build_document(payload, document_id=document_id)
    except DocumentFetchError as exc:
        if not _looks_unsupported(str(exc)):
            message = f"读取文档失败：{exc}"
            commit(sync_status="error", error_message=message[:1000])
            result.update(action="error", sync_status="error", error=message)
            return result
        emit_note_once(f"块列表命令不可用（{str(exc)[:160]}），退回 raw-content 解析")
        try:
            payload = client.call(raw_template, document_id, label=f"doc-raw {document_id}")
            document = build_document(payload, document_id=document_id)
        except (DocumentFetchError, TranscriptLayoutError) as raw_exc:
            message = f"读取文档失败：{raw_exc}"
            commit(sync_status="error", error_message=message[:1000])
            result.update(action="error", sync_status="error", error=message)
            return result
    except TranscriptLayoutError as exc:
        message = f"文档结构不符合契约（{exc.code}）：{exc}"
        commit(sync_status="error", error_message=message[:1000])
        result.update(action="error", sync_status="error", error=message)
        return result

    if not document.revision_id:
        message = f"文档响应缺少 revision_id，无法建立幂等基线（document_id={document_id}）"
        commit(sync_status="error", error_message=message[:1000])
        result.update(action="error", sync_status="error", error=message)
        return result

    try:
        markdown = extract_readable_transcript(document)
    except TranscriptLayoutError as exc:
        message = f"抽取可读版口播稿失败（{exc.code}）：{exc}"
        commit(sync_status="error", error_message=message[:1000])
        result.update(action="error", sync_status="error", error=message)
        return result

    source_blob = document.source_text.encode("utf-8")
    content_sha, content_len, path = write_markdown(
        row.video_record_id, document.revision_id, markdown
    )
    mirrored_at = base_sync.now_text()
    pre_existing = row.sync_status is not None
    commit(
        sync_status="ok",
        document_url=url,
        document_id=document.document_id or document_id,
        revision_id=document.revision_id,
        local_markdown_path=relative_posix(path),
        content_sha256=content_sha,
        content_bytes=content_len,
        source_content_sha256=sha256_bytes(source_blob),
        source_content_bytes=len(source_blob),
        extractor_version=EXTRACTOR_VERSION,
        content_scope=CONTENT_SCOPE,
        mirrored_at=mirrored_at,
        error_message=None,
    )
    result.update(
        action="mirrored",
        sync_status="ok",
        revision_id=document.revision_id,
        local_markdown_path=relative_posix(path),
        content_sha256=content_sha,
        content_bytes=content_len,
        pre_existing=pre_existing,
    )
    return result


def _looks_unsupported(text: str) -> bool:
    lowered = str(text or "").lower()
    return any(
        marker in lowered
        for marker in (
            "unknown flag",
            "unknown command",
            "unexpected argument",
            "no such command",
            "unrecognized",
            "unknown option",
        )
    )


def resolve_wiki_document(client: LarkDocClient, node_token: str) -> str:
    payload = client.call(
        os.environ.get(WIKI_NODE_ARGS_ENV, WIKI_NODE_ARGS_DEFAULT),
        node_token,
        label=f"wiki-node {node_token}",
    )
    for key in ("obj_token", "target_token", "document_id", "node_token"):
        value = find_first(payload, (key,))
        if value:
            return str(value)
    raise RuntimeError("wiki 节点响应里没有 obj_token")


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------

REMEDIATION_TRANSCRIPT = (
    "修复提示：\n"
    f"  1) 先确认 Base 已同步：python {relative_posix(Path('sync_feishu_base_to_local.py'))}"
    f"（口播稿来源读自 {base_sync.VIDEOS_SPEC.view}.transcript_document_url）；\n"
    f"  2) 缺少配置时复制 feishu-base-config.example.json 为 {relative_posix(fc.CONFIG_PATH)} 并填好 profile/base_token；\n"
    "  3) 安装并登录 lark-cli：npm install -g @larksuite/cli && lark-cli --profile <profile> auth login；\n"
    "  4) 只想离线验证语料表与视图：python sync_feishu_transcript_docs_to_local.py --init-schema"
)


def preflight_config() -> dict[str, Any]:
    if not fc.CONFIG_PATH.is_file():
        raise TranscriptSetupError(f"缺少飞书配置文件 {relative_posix(fc.CONFIG_PATH)}")
    try:
        config = fc.load_config()
    except (OSError, json.JSONDecodeError) as exc:
        raise TranscriptSetupError(f"飞书配置文件无法解析：{exc}") from exc
    if not str(config.get("profile") or "").strip():
        raise TranscriptSetupError("配置缺少 profile")
    try:
        lark_cli_runtime.resolve_lark_cli_binary()
    except Exception as exc:
        raise TranscriptSetupError(f"找不到 lark-cli 可执行文件（{exc}）") from exc
    return config


def run(
    *,
    db_path: Path,
    max_records: int | None,
    force: bool,
    request_interval: float,
    timeout: int,
    manifest_path: Path,
    log_path: Path,
) -> int:
    started_perf = time.perf_counter()
    started_at = base_sync.now_text()
    summary = {
        "total": 0,
        "mirrored": 0,
        "created": 0,
        "updated": 0,
        "unchanged": 0,
        "error": 0,
        "orphaned": 0,
        "skipped": 0,
        "failed": 0,
    }
    records: list[dict[str, Any]] = []
    notes: list[str] = []

    if not Path(db_path).is_file():
        raise TranscriptSetupError(
            f"目标数据库不存在：{relative_posix(Path(db_path))}；Base 尚未同步过，"
            "请先运行 python run_feishu_local_sync.py 或 python sync_feishu_base_to_local.py --init-schema"
        )
    config = preflight_config()
    conn = base_sync.connect_rw(db_path)
    client = LarkDocClient(
        config, log_path=log_path, request_interval=request_interval, timeout=timeout
    )
    fatal_error = ""
    try:
        base_sync.ensure_schema(conn)
        conn.commit()
        targets = load_targets(conn, max_records)
        summary["total"] = len(targets)
        if not targets:
            notes.append("videos_readable 没有任何记录，本次没有需要镜像的口播稿文档")
        emit_durable(
            f"口播稿镜像开始：候选 {len(targets)} 条（max_records={max_records} force={force}）"
            f" 提取器版本 v{EXTRACTOR_VERSION} 内容域 {CONTENT_SCOPE}",
            log_path=log_path,
        )
        for index, row in enumerate(targets, 1):
            try:
                result = process_row(conn, client, row, force=force, log_path=log_path)
            except Exception as exc:  # 兜底：单条异常不允许中断整批
                result = {
                    "video_record_id": row.video_record_id,
                    "action": "error",
                    "sync_status": "error",
                    "error": f"{type(exc).__name__}: {exc}",
                }
                try:
                    conn.rollback()
                except sqlite3.Error:
                    pass
            action = str(result.get("action") or "")
            if action == "mirrored":
                summary["mirrored"] += 1
                if result.get("pre_existing"):
                    summary["updated"] = summary.get("updated", 0) + 1
                else:
                    summary["created"] = summary.get("created", 0) + 1
            elif action == "unchanged":
                summary["unchanged"] += 1
            elif action == "error":
                summary["error"] += 1
            elif action in {"orphaned"}:
                summary["orphaned"] += 1
            else:
                summary["skipped"] += 1
            if len(records) < MANIFEST_RECORD_LIMIT:
                records.append(result)
            emit_durable(
                f"[{index}/{len(targets)}] {row.video_record_id} -> {action or 'skipped'} "
                f"status={result.get('sync_status')} {str(result.get('error') or '')[:200]}".rstrip(),
                log_path=log_path,
            )
    except Exception as exc:
        fatal_error = f"{type(exc).__name__}: {exc}"
    finally:
        conn.close()

    summary["failed"] = summary["error"]
    status = "success" if not fatal_error and summary["error"] == 0 else "failed"
    ended_at = base_sync.now_text()
    manifest = {
        "kind": "feishu_transcript_mirror",
        "task": "sync_feishu_transcript_docs_to_local",
        "status": status,
        "started_at": started_at,
        "ended_at": ended_at,
        "duration_seconds": round(time.perf_counter() - started_perf, 3),
        "db_path": relative_posix(Path(db_path)),
        "docs_root": relative_posix(DOCS_ROOT),
        "content_scope": CONTENT_SCOPE,
        "extractor_version": EXTRACTOR_VERSION,
        "options": {
            "max_records": max_records,
            "force": force,
            "request_interval_seconds": request_interval,
        },
        "feishu_requests": client.request_count,
        "feishu_retries": client.retry_count,
        "summary": {
            "total": summary["total"],
            "created": summary["created"],
            "updated": summary["updated"],
            "mirrored": summary["mirrored"],
            "skipped_existing": summary["unchanged"],
            "skipped": summary["skipped"],
            "orphaned": summary["orphaned"],
            "error": summary["error"],
            "failed": summary["failed"],
        },
        "records": records,
        "failures": [item for item in records if item.get("sync_status") == "error"],
        "notes": notes + [note for note in collected_notes() if note not in notes],
        "error": fatal_error or None,
    }
    fc.write_manifest(manifest_path, manifest)
    if fatal_error:
        emit_durable(f"口播稿镜像异常中断：{fatal_error}", log_path=log_path)
    emit_durable(
        f"口播稿镜像结束 status={status}：新语料 {summary['mirrored']} 未变 {summary['unchanged']} "
        f"错误 {summary['error']} 孤儿 {summary['orphaned']} 跳过 {summary['skipped']}",
        log_path=log_path,
    )
    return 0 if status == "success" else 1


def init_schema_only(db_path: Path, log_path: Path, manifest_path: Path | None = None) -> int:
    """离线：建语料表与视图并校验消费方查询，不访问飞书、不写任何语料状态。"""
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = base_sync.connect_rw(db_path)
    try:
        created = base_sync.ensure_schema(conn)
        rebuilt = base_sync.rebuild_readable_views(conn)
        checks = base_sync.validate_views(conn)
        conn.commit()
    finally:
        conn.close()
    bad = [item for item in checks if not item["ok"]]
    payload = {
        "kind": "feishu_transcript_mirror",
        "action": "init_schema",
        "status": "success" if not bad else "failed",
        "started_at": base_sync.now_text(),
        "ended_at": base_sync.now_text(),
        "db_path": relative_posix(db_path),
        "content_scope": CONTENT_SCOPE,
        "extractor_version": EXTRACTOR_VERSION,
        "objects_created": created,
        "views_rebuilt": rebuilt,
        "view_checks": checks,
        "summary": {
            "created": 0,
            "updated": 0,
            "skipped_existing": 0,
            "failed": len(bad),
            "views": len(rebuilt),
        },
        "error": None if not bad else json.dumps(bad, ensure_ascii=False)[:2000],
        "note": "只建 schema/视图，未访问飞书，未生成任何语料",
    }
    if manifest_path is not None:
        # 管线要求「退出码 + 本次 manifest 状态」双判定，离线模式也要落一份 manifest 才不自相矛盾。
        try:
            fc.write_manifest(Path(manifest_path), payload)
            payload["manifest_path"] = relative_posix(Path(manifest_path))
        except OSError as exc:  # pragma: no cover
            payload["manifest_write_error"] = str(exc)
    emit_durable(
        f"离线建库完成（口播稿语料层）：视图 {len(rebuilt)} 个，失败校验 {len(bad)} 项",
        log_path=log_path,
    )
    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return 0 if not bad else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="sync_feishu_transcript_docs_to_local.py",
        description=(
            "把飞书视频口播稿文档里唯一的「口播稿（可读版）」章节镜像为本地纯净 Markdown，"
            "并把版本/哈希/状态写入 video_transcript_mirrors。"
        ),
    )
    parser.add_argument(
        "--max-records", type=int, default=None, help="只处理前 N 条视频，用于小批量验证"
    )
    parser.add_argument(
        "--force", action="store_true", help="忽略已存 revision，强制重新读取并提取"
    )
    parser.add_argument(
        "--init-schema", action="store_true", help="离线模式：只建语料表/视图并校验，不访问飞书"
    )
    parser.add_argument("--db-path", default=str(DB_PATH), help="本地 SQLite 路径")
    parser.add_argument("--manifest", default=None, help="manifest 输出路径（管线会传入）")
    parser.add_argument(
        "--request-interval",
        type=float,
        default=DEFAULT_REQUEST_INTERVAL,
        help="相邻飞书请求最小间隔（秒）",
    )
    parser.add_argument(
        "--timeout", type=int, default=DEFAULT_DOC_TIMEOUT, help="单篇文档请求超时（秒）"
    )
    parser.add_argument("--log-path", default=None, help="持久进度日志路径")
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(list(argv) if argv is not None else None)
    db_path = Path(args.db_path).expanduser().resolve()
    stamp = base_sync.ts_slug()
    manifest_path = (
        Path(args.manifest).expanduser().resolve()
        if args.manifest
        else MANIFEST_ROOT / f"{stamp}-feishu-transcript-mirror.json"
    )
    log_path = (
        Path(args.log_path).expanduser().resolve()
        if args.log_path
        else LOG_DIR / f"feishu-transcript-mirror-{datetime.now():%Y-%m-%d}.log"
    )

    if args.init_schema:
        return init_schema_only(db_path, log_path, manifest_path)

    if args.max_records is not None and args.max_records <= 0:
        print("--max-records 必须是正整数", file=sys.stderr)
        return 2

    lock = base_sync.acquire_lock("feishu_transcript_docs_sync")
    if lock is None:
        message = "另一个 sync_feishu_transcript_docs_to_local 进程正在运行，拒绝重叠执行"
        emit_durable(message, log_path=log_path)
        print(
            json.dumps(
                {"kind": "feishu_transcript_mirror", "status": "locked", "error": message},
                ensure_ascii=False,
            )
        )
        return 3
    try:
        try:
            code = run(
                db_path=db_path,
                max_records=args.max_records,
                force=args.force,
                request_interval=args.request_interval,
                timeout=args.timeout,
                manifest_path=manifest_path,
                log_path=log_path,
            )
        except (TranscriptSetupError, FileNotFoundError) as exc:
            emit_durable(f"环境预检失败：{exc}", log_path=log_path)
            print(REMEDIATION_TRANSCRIPT, file=sys.stderr)
            manifest = {
                "kind": "feishu_transcript_mirror",
                "task": "sync_feishu_transcript_docs_to_local",
                "status": "failed",
                "started_at": base_sync.now_text(),
                "ended_at": base_sync.now_text(),
                "db_path": relative_posix(db_path),
                "content_scope": CONTENT_SCOPE,
                "extractor_version": EXTRACTOR_VERSION,
                "summary": {
                    "total": 0,
                    "created": 0,
                    "updated": 0,
                    "skipped_existing": 0,
                    "skipped": 0,
                    "orphaned": 0,
                    "error": 0,
                    "failed": 1,
                },
                "failures": [],
                "notes": ["未访问飞书，未改动任何本地语料"],
                "error": str(exc),
                "remediation": REMEDIATION_TRANSCRIPT,
            }
            manifest_path.parent.mkdir(parents=True, exist_ok=True)
            fc.write_manifest(manifest_path, manifest)
            print(
                json.dumps(
                    dict(manifest, manifest_path=relative_posix(manifest_path)), ensure_ascii=False
                )[:8000]
            )
            return 1
        try:
            payload = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            payload = {
                "kind": "feishu_transcript_mirror",
                "status": "failed",
                "error": f"manifest 读取失败：{exc}",
            }
        payload["manifest_path"] = relative_posix(manifest_path)
        payload["exit_code"] = code
        print(json.dumps(payload, ensure_ascii=False)[:20000])
        return code
    finally:
        lock.release()


if __name__ == "__main__":  # pragma: no cover
    for _stream_name in ("stdout", "stderr"):
        _stream = getattr(sys, _stream_name)
        if hasattr(_stream, "reconfigure"):
            try:
                _stream.reconfigure(encoding="utf-8", errors="replace")
            except Exception:  # pragma: no cover
                pass
    sys.exit(main())
