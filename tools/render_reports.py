"""T5.4 报告生成的**薄壳**：把一次调用转交给 `ported/reports/` 里的 V1 脚本，并把结果如实记下。

**跑法**：

```bash
# 输入包（V1 的镜像库 + pipeline 清单都得显式给）
uv run python -X utf8 tools/render_reports.py prepare --window daily \
    --db <镜像库> --manifest-dir <清单目录>
# HTML 报告（out-dir 下要有 <window>-report.json，那份由分析方手写）
uv run python -X utf8 tools/render_reports.py render --window daily --out-dir <目录>
# 博主洞察看板
uv run python -X utf8 tools/render_reports.py dashboard --days 7 --db <镜像库>
# 只看计划与缺项，不起子进程、不写文件
uv run python -X utf8 tools/render_reports.py prepare --window weekly \
    --db <镜像库> --cutoff "2026-09-01 20:00:00" --dry-run
```

**它只有一件职责**（ADR-0018 决定 2 的例外清单里那一类"转交型薄壳"）：拼 argv、跑子进程、
验产物、把结论打成一行 JSON。它**不 import** `ported/` —— 走子进程，理由有两条：
V1 那三份脚本用 `sys.exit` / 模块级全局状态，import 进服务进程等于把这两样带进来；
而且它们的第一行是 shebang、按计划还能照 V1 的老办法直接跑。

三条不臆造成功的规矩：

1. **退出码不是判据**。这三份脚本生成的是 HTML/JSON 产物，"跑成功"只能是产物成立：
   文件在、不是空壳（≥ `MIN_ARTIFACT_BYTES`）、能解析出里面那份内嵌 JSON（报告的
   `#report-data` / 看板的 `#dashboard-data` / 输入包的 `pack_sha256`）。退出码 0 而产物
   不存在 = 红。反过来 0 字节的 HTML 比"没生成"更坏，所以尺寸这一条单独判。
2. **路径一律绝对、且必须落在源码树之外**。V1 脚本里的 `ROOT = Path(__file__).parent`
   搬进包里之后指向 `src/…/ported/reports/`：`generate_creator_insight_dashboard.py` 的
   `--db/--output` 相对路径是拼到 ROOT 上的（另外两份拼到 cwd）。所以本壳把每个路径
   `resolve()` 成绝对再传，并且**跑之前**检查 `cwd` 与产物路径没有一个落在 `src/` 下，
   落上了就拒跑（退出码 2），而不是让子进程去把源码树写脏。
3. **缺输入就如实失败**，并给出照着能做的修复动作；`--dry-run` 只打印计划与缺项，
   永远 `ok=false`（dry-run 的产物不是产物）。

产物默认落 `data/outputs/` 下（`FileStorage` 今天没有 `outputs` 这一档，见 `outputs_dir()`
的注释）。V1 的文件名协议（`<window>-input-pack.json` / `<window>-report.html`）在
`ported/` 里原样保留，本壳照它算产物路径 —— 这是搬运代码的既有约定，不是新发明。
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Final, Literal

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from intelligence_hub_v2.core.config import load_app_config
from intelligence_hub_v2.infra.subprocess import SubprocessTimeoutError, run_subprocess
from intelligence_hub_v2.storage.files import FileStorage

__all__ = ["Plan", "build_plan", "main", "plan_problems", "verify_artifacts"]

KINDS: Final = ("prepare", "render", "dashboard")
WINDOWS: Final = ("daily", "three-day", "weekly")

#: kind → 搬运区里那份 V1 脚本的模块路径。薄壳只认这三个名字，不接受任意模块。
MODULE_BY_KIND: Final[dict[str, str]] = {
    "prepare": "intelligence_hub_v2.ported.reports.prepare_creator_analysis",
    "render": "intelligence_hub_v2.ported.reports.render_creator_analysis_report",
    "dashboard": "intelligence_hub_v2.ported.reports.generate_creator_insight_dashboard",
}

#: 产物"不是空壳"的下限。挡的是 0 字节与只写了模板头的文件，不挡"内容不够好" ——
#: 一份真看板是几百 KB，一份真输入包是几十 KB，512 B 只在"连模板都没写完"这一侧生效。
MIN_ARTIFACT_BYTES: Final = 512

#: 内嵌 JSON 的 `<script>` 元素 id（V1 自己写死的两个名字）。
EMBED_IDS: Final = {"report": "report-data", "dashboard": "dashboard-data"}

DEFAULT_TIMEOUT_SECONDS: Final = 900.0

OUTPUTS_SUBDIR: Final = "outputs"
ANALYSIS_SUBDIR: Final = "creator-analysis"
DASHBOARD_FILENAME: Final = "creator-insight-dashboard.html"
PACK_SUFFIX: Final = "-input-pack.json"
REPORT_JSON_SUFFIX: Final = "-report.json"
REPORT_HTML_SUFFIX: Final = "-report.html"

#: 判据失败时附给调用方的修复动作（AGENTS.md §1.3：失败要带能照着做的下一步）。
FIX_BY_KIND: Final[dict[str, str]] = {
    "prepare": (
        "先备好 V1 那两份输入：--db 指向 feishu-base.sqlite3（飞书镜像库，本工具只读它、不建它），"
        "--manifest-dir 指向放 *-feishu-local-pipeline.json 的目录；只是离线演练就改用 --cutoff。"
    ),
    "render": (
        "--out-dir 下要同时有 <window>-input-pack.json（先跑本工具的 prepare 子命令）与 "
        "<window>-report.json（报告 JSON 由分析方手写，V2 今天没有它）。"
    ),
    "dashboard": "--db 要指向一份可读的 V1 飞书镜像库（含 *_readable 视图），且 --days 为正整数。",
}

#: 产物怎么验：json=顶层对象+必备键；html=能解析出指定 id 的内嵌 JSON。
ArtifactCheck = Literal["pack", "report-html", "dashboard-html"]
CHECK_BY_KIND: Final[dict[str, ArtifactCheck]] = {
    "prepare": "pack",
    "render": "report-html",
    "dashboard": "dashboard-html",
}

EXIT_OK: Final = 0
EXIT_FAILED: Final = 1
EXIT_REFUSED: Final = 2
EXIT_DRY_RUN: Final = 3


@dataclass(frozen=True)
class Plan:
    """一次转交的全部事实：argv、cwd、产物与前置输入。可单独打印（`--dry-run`）。"""

    kind: str
    argv: tuple[str, ...]
    cwd: Path
    artifacts: tuple[Path, ...]
    required_inputs: tuple[Path, ...]
    fix: str


def outputs_dir(storage: FileStorage) -> Path:
    """`<data>/outputs/`。

    `FileStorage` 今天**没有** outputs 这一档（它只算 media/manifests/cookies/logs/tmp，
    见 `storage/files.py`），而本任务不许改 `storage/**`，所以在壳里拼这一层：
    根仍然只有一个真源（`storage.root`），换实现时这里跟着改一行。
    """
    return storage.root / OUTPUTS_SUBDIR


def source_tree() -> Path:
    """源码树根：产物或 cwd 落进这里就是事故，不是失败。"""
    return _REPO_ROOT / "src"


def resolve_data_dir(args: argparse.Namespace) -> FileStorage:
    """`--data-dir` 覆盖配置，否则从配置算（与 `tools/refresh_bridge_cookies.py` 同一形状）。"""
    raw = str(args.data_dir or "").strip()
    if raw:
        return FileStorage(Path(raw).expanduser().resolve())
    config = load_app_config()
    return FileStorage.from_config(config, root=_REPO_ROOT)


def build_plan(args: argparse.Namespace, storage: FileStorage) -> Plan:
    """把一次子命令拼成 argv。**所有路径先 resolve() 成绝对**，不给孩子留相对路径的余地。

    产物文件名按 V1 的协议算（`<window>-input-pack.json` 等），但**由本壳算好显式传下去**
    （`--out` / `--output`）：判据要检查的那个路径必须和子进程真正写的那个是同一个，
    靠"孩子自己会拼对名字"就是把成功押在别人的默认值上。
    """
    kind: str = args.kind
    out_base = outputs_dir(storage)
    out_dir = Path(str(args.out_dir or (out_base / ANALYSIS_SUBDIR))).expanduser().resolve()
    window = str(args.window)
    # `--db` 由 parse_args 保证只在需要它的 kind 里出现，这里统一算成绝对路径。
    db = Path(str(args.db)).expanduser().resolve()
    inputs: list[Path] = [db] if kind in {"prepare", "dashboard"} else []
    tail: list[str] = []

    if kind == "prepare":
        artifact = (out_dir / f"{window}{PACK_SUFFIX}").resolve()
        tail = [
            "prepare",
            "--window",
            window,
            "--db",
            str(db),
            "--out-dir",
            str(out_dir),
            "--out",
            str(artifact),
        ]
        cutoff = str(args.cutoff or "").strip()
        if cutoff:
            tail += ["--cutoff", cutoff]
        else:
            manifest_dir = Path(str(args.manifest_dir)).expanduser().resolve()
            inputs.append(manifest_dir)
            tail += ["--manifest-dir", str(manifest_dir)]
    elif kind == "render":
        pack = out_dir / f"{window}{PACK_SUFFIX}"
        report_json = out_dir / f"{window}{REPORT_JSON_SUFFIX}"
        artifact = (out_dir / f"{window}{REPORT_HTML_SUFFIX}").resolve()
        inputs += [pack, report_json]
        tail = [
            "--window",
            window,
            "--out-dir",
            str(out_dir),
            "--pack",
            str(pack),
            "--input",
            str(report_json),
            "--output",
            str(artifact),
        ]
    else:
        artifact = Path(str(args.output or (out_base / DASHBOARD_FILENAME))).resolve()
        tail = ["--days", str(args.days), "--db", str(db), "--output", str(artifact)]

    return Plan(
        kind=kind,
        argv=(sys.executable, "-X", "utf8", "-m", MODULE_BY_KIND[kind], *tail),
        cwd=Path(str(args.cwd or storage.root)).expanduser().resolve(),
        artifacts=(artifact,),
        required_inputs=tuple(inputs),
        fix=FIX_BY_KIND[kind],
    )


def _is_under(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root)
    except ValueError:
        return False
    return True


def plan_problems(plan: Plan) -> list[str]:
    """跑之前的闸：路径不许落在源码树里，输入必须在场。返回空 = 可以跑。"""
    src = source_tree()
    problems: list[str] = []
    if _is_under(plan.cwd, src):
        problems.append(
            f"cwd 落在源码树里：{plan.cwd} —— V1 脚本的相对路径会拼到它下面，产物会写进 src/"
        )
    for artifact in plan.artifacts:
        if _is_under(artifact, src):
            problems.append(f"产物路径落在源码树里：{artifact}（ported/ 的 ROOT 指向包目录）")
    for item in plan.required_inputs:
        if not item.exists():
            problems.append(f"输入不存在：{item}")
    return problems


def parse_json_object(text: str) -> dict[str, Any] | None:
    """能解析成 JSON 对象就返回它，否则 None。"""
    try:
        loaded: object = json.loads(text)
    except ValueError:
        return None
    return loaded if isinstance(loaded, dict) else None


def extract_embedded_json(html: str, element_id: str) -> dict[str, Any] | None:
    """从 `<script id=… type="application/json">…</script>` 里取出内嵌 JSON。

    属性顺序两份脚本不一样（report 是 type 在前、dashboard 是 id 在前），所以匹配只认
    `id="…"` 这一个必要属性。V1 会把 `< > &` 转义成 \\u003c 之类，那是合法的 JSON 转义，
    `json.loads` 直接吃得下。
    """
    match = re.search(
        r'<script[^>]*\bid="' + re.escape(element_id) + r'"[^>]*>(.*?)</script>',
        html,
        flags=re.S,
    )
    if match is None:
        return None
    return parse_json_object(match.group(1))


def verify_artifacts(plan: Plan) -> list[str]:
    """跑之后的闸：**退出码不在这里**，只看产物。返回空 = 产物成立。"""
    check = CHECK_BY_KIND[plan.kind]
    problems: list[str] = []
    for artifact in plan.artifacts:
        if not artifact.is_file():
            problems.append(f"产物不存在：{artifact}")
            continue
        size = artifact.stat().st_size
        if size < MIN_ARTIFACT_BYTES:
            problems.append(f"产物只有 {size} 字节（<{MIN_ARTIFACT_BYTES} 即视为空壳）：{artifact}")
            continue
        try:
            text = artifact.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            problems.append(f"产物读不出来：{artifact} · {exc}")
            continue
        if check == "pack":
            pack = parse_json_object(text)
            if pack is None:
                problems.append(f"输入包不是 JSON 对象：{artifact}")
            elif not str(pack.get("pack_sha256") or ""):
                problems.append(f"输入包缺 pack_sha256（prepare 未走完确定性校验）：{artifact}")
        else:
            element_id = EMBED_IDS["report" if check == "report-html" else "dashboard"]
            if extract_embedded_json(text, element_id) is None:
                problems.append(f"产物里解析不出内嵌 JSON #{element_id}：{artifact}")
    return problems


async def _execute(plan: Plan, timeout: float) -> tuple[int, str, str]:
    """跑子进程，V1 的原始输出逐行透传（人不该只看到一句"失败"）。"""
    result = await run_subprocess(
        plan.argv,
        timeout=timeout,
        cwd=plan.cwd,
        env={**os.environ, "PYTHONUTF8": "1", "PYTHONIOENCODING": "utf-8"},
        on_stdout_line=lambda line: print(line, flush=True),
        on_stderr_line=lambda line: print(line, file=sys.stderr, flush=True),
    )
    return result.returncode, result.stdout, result.stderr


async def run_plan(plan: Plan, timeout: float) -> dict[str, Any]:
    try:
        code, _out, err = await _execute(plan, timeout)
    except SubprocessTimeoutError as exc:
        return {
            "ok": False,
            "exit_code": None,
            "timed_out_after_seconds": exc.timeout,
            "reasons": [f"子进程超时（>{timeout} 秒），已终止并收尸"],
            "stderr_tail": exc.stderr.strip(),
        }
    except LookupError as exc:
        return {
            "ok": False,
            "exit_code": None,
            "reasons": [str(exc)],
            "stderr_tail": "",
        }
    reasons = verify_artifacts(plan)
    if code != 0:
        reasons.insert(0, f"子进程退出码 {code}（V1 脚本自报的失败，原文见 stderr_tail）")
    return {
        "ok": not reasons,
        "exit_code": code,
        "reasons": reasons,
        "stderr_tail": err.strip(),
    }


def report_lines(plan: Plan, verdict: dict[str, Any]) -> list[str]:
    lines = [
        f"kind={plan.kind} module={MODULE_BY_KIND[plan.kind].rsplit('.', 1)[-1]}",
        f"argv={' '.join(plan.argv)}",
        f"cwd={plan.cwd}",
    ]
    for artifact in plan.artifacts:
        size = artifact.stat().st_size if artifact.is_file() else None
        lines.append(f"artifact={artifact} bytes={size}")
    for key, value in verdict.items():
        if key == "reasons":
            continue
        lines.append(f"{key}={value}")
    for reason in verdict.get("reasons", []):
        lines.append(f"[判据不通过] {reason}")
    if not verdict["ok"]:
        lines.append(f"修复：{plan.fix}")
    return lines


def parse_args(argv: Sequence[str] | None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="把一次报告生成转交给 ported/reports/ 里的 V1 脚本，并按产物判成败",
        epilog=(
            "退出码：0 产物成立 / 1 失败（缺输入、子进程非零、产物不合格）/ "
            "2 拒跑（cwd 或产物会落在源码树里）/ 3 --dry-run（只打印计划）"
        ),
    )
    parser.add_argument("kind", choices=KINDS, help="转交给哪一份 V1 脚本")
    parser.add_argument("--window", choices=WINDOWS, default="daily", help="分析窗口")
    parser.add_argument(
        "--db",
        default="",
        help="V1 飞书镜像库 SQLite（prepare/dashboard 必填）",
    )
    parser.add_argument(
        "--manifest-dir",
        default="",
        help="V1 pipeline manifest 目录（prepare 必填）",
    )
    parser.add_argument(
        "--cutoff",
        default="",
        help="手工数据截止点，离线演练用",
    )
    parser.add_argument(
        "--out-dir",
        default="",
        help="分析产物目录，默认 <data>/outputs/creator-analysis",
    )
    parser.add_argument(
        "--output",
        default="",
        help=f"看板 HTML 路径，默认 <data>/{OUTPUTS_SUBDIR}/{DASHBOARD_FILENAME}",
    )
    parser.add_argument("--days", type=int, default=7, help="看板回溯天数（默认 7）")
    parser.add_argument("--data-dir", default="", help="产物根目录，默认取 config 的 data.dir")
    parser.add_argument(
        "--cwd",
        default="",
        help="子进程工作目录，默认 <data>；落进 src/ 即拒跑",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=DEFAULT_TIMEOUT_SECONDS,
        help="子进程超时（秒）",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="只打印计划与缺项，不起子进程、不写文件",
    )
    args = parser.parse_args(argv)

    if args.kind in {"prepare", "dashboard"} and not str(args.db).strip():
        parser.error(f"{args.kind} 需要 --db（V1 镜像库路径，本工具不替你猜）")
    if args.kind == "prepare" and not (str(args.cutoff).strip() or str(args.manifest_dir).strip()):
        parser.error("prepare 需要 --manifest-dir 或 --cutoff 之一（数据截止点的两个来源）")
    return args


def _emit(plan: Plan | None, verdict: dict[str, Any]) -> None:
    payload: dict[str, Any] = {"kind": plan.kind if plan else "", **verdict}
    if plan is not None:
        payload["argv"] = list(plan.argv)
        payload["cwd"] = str(plan.cwd)
        payload["artifacts"] = [str(p) for p in plan.artifacts]
        payload["fix"] = plan.fix
    print(json.dumps(payload, ensure_ascii=False), flush=True)
    if plan is not None:
        for line in report_lines(plan, verdict):
            print(line, file=sys.stderr)


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        storage = resolve_data_dir(args)
    except (OSError, ValueError) as exc:
        print(f"[错误] 配置读不出来：{type(exc).__name__}: {exc}", file=sys.stderr)
        print(f"修复：{FIX_BY_KIND[args.kind]}", file=sys.stderr)
        return EXIT_FAILED
    plan = build_plan(args, storage)

    if args.dry_run:
        verdict = {
            "ok": False,
            "dry_run": True,
            "reasons": plan_problems(plan) or ["计划已打印；dry-run 不产生任何产物，不算成功"],
        }
        _emit(plan, verdict)
        return EXIT_DRY_RUN

    problems = plan_problems(plan)
    if problems:
        verdict = {"ok": False, "refused": True, "reasons": problems}
        _emit(plan, verdict)
        return EXIT_REFUSED

    verdict = asyncio.run(run_plan(plan, args.timeout))
    _emit(plan, verdict)
    return EXIT_OK if verdict["ok"] else EXIT_FAILED


if __name__ == "__main__":
    raise SystemExit(main())
