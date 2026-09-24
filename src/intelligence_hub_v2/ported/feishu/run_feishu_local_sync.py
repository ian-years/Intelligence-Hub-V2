#!/usr/bin/env python3
# ruff: noqa
# TODO(v2-adapt): V1 的每日管线编排器，未适配。它本身就是另一套 TaskRunner：以子进程方式调
# `<ROOT>/sync_feishu_*.py`（cwd=ROOT）、自带 O_CREAT|O_EXCL 的 PipelineLock 与陈旧锁自愈、把结果
# 写成 <ROOT>/downloads/manifests/<YYYYMMDD-HHMMSS>-feishu-local-pipeline.json，而文件名前 8 位是
# 下游 V1 报告脚本 pick_data_cutoff 的解析协议。V2 的清单/超时/取消/事件在 core/task_runner.py +
# core/manifest.py，这一整块是平行的重复实现。适配要做的那件事：删掉编排，只留两个阶段里真正拉
# 飞书的那段，交给 feishu_sync handler 与 TaskRunner 的锁、清单收尾。
# 形状说明：ruff 的文件级豁免写在第 2 行，第 1 行留给 shebang —— 否则 `./x.py` 这个入口就没了。
"""飞书本地同步每日管线 · 唯一入口（阶段一 Base 五张表 → 阶段二 纯净口播稿）。

契约来源：``FEISHU_LOCAL_DATABASE.md``（唯一规格说明书），承重条款是 :160
「``run_feishu_local_sync.py`` 还会检查两个子程序的退出码和本次新 manifest 的 ``status``。
任一阶段失败都不会运行下一阶段，并生成状态为 ``failed`` 的 ``*-feishu-local-pipeline.json``。
整条管线有跨平台进程锁，拒绝重叠执行」，以及 :146 / :211（每日固定入口，
**Codex 自动化唯一允许调用的命令**，不带任何参数）与 :229（排错从本 manifest 的 ``stages[]`` 出发）。

    python .\\run_feishu_local_sync.py

两个阶段
--------
1. ``sync_feishu_base_to_local.py`` —— 飞书 Base 五张表增量镜像 + ``*_readable`` 视图重建。
2. ``sync_feishu_transcript_docs_to_local.py`` —— 只把 `口播稿（可读版）` 镜像为纯净 Markdown。

本文件**不重新实现任何同步逻辑**，只做编排：以子进程方式调用上面两个脚本（固定
``cwd=ROOT``、``PYTHONUTF8=1`` / ``PYTHONIOENCODING=utf-8`` / ``PYTHONUNBUFFERED=1``），
把它们的 stdout / stderr 逐行加前缀（``[base] `` / ``[transcript] ``）实时透传，并各自传入
``--manifest`` 与 ``--db-path``。

阶段是否成功要同时满足：退出码为 0 **且** 它拿到的那份 manifest 存在、能解析为 JSON、
``status`` 是成功态、``summary.failed`` 为 0。任一条件不满足即判失败——**阶段一失败时
阶段二根本不启动**，绝不伪造成功：本机没有 ``lark-cli`` / ``feishu-base-config.json`` 时
阶段一必然失败，管线如实报 ``failed`` 并非零退出。

进程锁
------
`downloads/launcher-state/locks/feishu_local_pipeline.lock`，用 ``O_CREAT | O_EXCL`` 原子抢锁，
锁内写 ``pid`` 与 ``started_at``。持锁进程已消失（Windows 走 ``kernel32.OpenProcess``，
POSIX 走 ``os.kill(pid, 0)``）或锁龄超过 ``--lock-stale-seconds`` 时，判定为上次崩溃遗留，
打印告警后抢占，避免永久阻塞；否则直接拒绝重叠执行（退出码 2）。锁在 ``finally`` 里释放，
且**只删自己创建的那把**（释放前复核锁内容），不会误删别人的锁。

数据截止点契约
--------------
产物 ``downloads/manifests/<YYYYMMDD-HHMMSS>-feishu-local-pipeline.json`` 会被
``prepare_creator_analysis.py`` 的 ``pick_data_cutoff`` 消费：文件名前 8 位必须是运行日期
（它按 ``p.name[:8] == 今天`` 过滤再按名字倒序），顶层需要 ``status`` /
``summary.failed`` / ``failures`` / ``kind`` / ``ended_at``（``YYYY-MM-DD HH:MM:SS``），
并按 ``stages[]`` 定位失败阶段——所以 ``stages[]`` 必须真实存在。

最后一行输出
------------
所有人类可读文字都在前面，**stdout 最后一行是单行 JSON**（本管线 payload），
供 ``launcher/launcher_server.py`` 的 ``extract_trailing_json`` 直接取用。

退出码
------
- ``0`` 两个阶段都成功，manifest 状态 ``success``
- ``1`` 任一阶段失败或未达成功态（含被中断 / 超时），manifest 状态 ``failed``
- ``2`` 拒绝重叠执行：已有本管线在跑（不写 manifest，因为没有任何阶段运行过）
"""

from __future__ import annotations

import argparse
import ctypes
import json
import os
import re
import signal
import subprocess
import sys
import threading
import time
from collections import deque
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Sequence

ROOT: Path = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from intelligence_hub_v2.ported.v1_shared.utils import now_str, ts_slug, write_json  # noqa: E402

TS_FORMAT = "%Y-%m-%d %H:%M:%S"

DEFAULT_DB_PATH: Path = ROOT / "downloads" / "feishu-base" / "feishu-base.sqlite3"
DEFAULT_MANIFEST_DIR: Path = ROOT / "downloads" / "manifests"
LOCK_DIR: Path = ROOT / "downloads" / "launcher-state" / "locks"
LOCK_NAME = "feishu_local_pipeline"  # 与两个子程序各自的锁名互不冲突
LOCK_STALE_SECONDS = 6 * 3600

#: 与 ``prepare_creator_analysis.pick_data_cutoff`` 的成功集合保持一致，不多也不少。
SUCCESS_STATUSES = frozenset({"success", "ok", "finished", "succeeded"})

#: 单一阶段的最长等待时间（秒）；超时即判该阶段失败并终止子进程。
DEFAULT_STAGE_TIMEOUT = 3600

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_LOCKED = 2

#: 两个阶段的静态定义：顺序即执行顺序，前一步失败则后一步标记为 ``not_run``。
STAGES: tuple[dict[str, str], ...] = (
    {
        "name": "base",
        "label": "阶段一 · 飞书 Base 五张表增量镜像",
        "script": "sync_feishu_base_to_local.py",
        "manifest_suffix": "feishu-local-sync.json",
        "prefix": "[base] ",
    },
    {
        "name": "transcript",
        "label": "阶段二 · 飞书口播稿纯净版镜像",
        "script": "sync_feishu_transcript_docs_to_local.py",
        "manifest_suffix": "feishu-transcript-mirror.json",
        "prefix": "[transcript] ",
    },
)

#: 子进程输出与人类可读文字共用一把锁，保证行不粘连、最后一行 JSON 不被插队。
_PRINT_LOCK = threading.Lock()


# --------------------------------------------------------------------------- #
# 路径与时间小工具
# --------------------------------------------------------------------------- #


def relative_posix(path: Path | str) -> str:
    """仓库内路径写成相对 posix 形式（manifest 里只存相对路径，换机器不失效）。"""
    p = Path(path).resolve()
    try:
        return p.relative_to(ROOT).as_posix()
    except ValueError:
        return p.as_posix()


def parse_ts(value: Any) -> datetime | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    candidates = (text, text.replace("T", " "))
    for fmt in (TS_FORMAT, "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        for cand in candidates:
            try:
                return datetime.strptime(cand, fmt)
            except ValueError:
                continue
    return None


def child_env() -> dict[str, str]:
    """与 ``launcher/launcher_server.py`` 的 ``child_env()`` 保持一致的子进程环境。"""
    env = os.environ.copy()
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUNBUFFERED"] = "1"
    return env


def popen_kwargs() -> dict[str, Any]:
    if os.name == "nt":
        return {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
    return {"start_new_session": True}


# --------------------------------------------------------------------------- #
# 跨平台进程锁（stdlib，原子抢锁 + 陈旧锁自愈）
# --------------------------------------------------------------------------- #


@dataclass
class PipelineLock:
    """``O_CREAT | O_EXCL`` 进程锁；只释放自己创建的那把。"""

    path: Path
    handle: int | None = None
    payload: str = ""
    stale_takeover: str = ""

    @property
    def acquired(self) -> bool:
        return self.handle is not None

    def release(self) -> None:
        if self.handle is None:
            return
        try:
            os.close(self.handle)
        except OSError:
            pass
        self.handle = None
        try:
            if self.path.read_text(encoding="utf-8", errors="replace") == self.payload:
                self.path.unlink()
        except OSError:
            pass

    def __enter__(self) -> "PipelineLock":
        return self

    def __exit__(self, *_exc: Any) -> None:
        self.release()


def pid_alive(pid: int) -> bool:
    """可移植地判断 pid 是否还活着（Windows 没有 ``os.kill(pid, 0)`` 语义）。"""
    if pid <= 0:
        return False
    if os.name == "nt":
        try:
            still_active = 259
            process_query_limited = 0x1000
            kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
            handle = kernel32.OpenProcess(process_query_limited, False, pid)
            if not handle:
                return False
            try:
                exit_code = ctypes.c_ulong()
                if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
                    return False
                return exit_code.value == still_active
            finally:
                kernel32.CloseHandle(handle)
        except Exception:  # pragma: no cover - 极端环境下按“活着”处理，宁可拒绝
            return True
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def read_lock_owner(path: Path) -> tuple[int | None, str]:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None, ""
    match = re.search(r"pid=(\d+)", text)
    return (int(match.group(1)) if match else None), text.strip()[:200]


def acquire_pipeline_lock(stale_seconds: int = LOCK_STALE_SECONDS) -> PipelineLock | None:
    """拿到锁返回 ``PipelineLock``；确实被别的进程占着返回 ``None``（拒绝重叠执行）。"""
    try:
        LOCK_DIR.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        say(f"[warn] 无法创建锁目录 {relative_posix(LOCK_DIR)}：{exc}")
        return None
    path = LOCK_DIR / f"{LOCK_NAME}.lock"
    payload = (
        f"pid={os.getpid()} acquired_at={now_str()} pipeline=run_feishu_local_sync cwd={ROOT}\n"
    )
    stale_reason = ""
    for attempt in (0, 1):
        try:
            handle = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
        except FileExistsError:
            if attempt == 1:
                return None
            pid, owner = read_lock_owner(path)
            try:
                age = time.time() - path.stat().st_mtime
            except OSError:
                age = stale_seconds + 1
            dead_owner = pid is not None and not pid_alive(pid)
            too_old = age > stale_seconds
            if not (dead_owner or too_old):
                return None
            stale_reason = (
                "持锁进程已不存在" if dead_owner else f"锁龄 {int(age)}s 超过 {stale_seconds}s"
            )
            say(
                f"[warn] 发现陈旧进程锁 {relative_posix(path)}（{stale_reason}；持有者 {owner or '未知'}），"
                f"判定为上次异常退出遗留，抢占后继续"
            )
            try:
                path.unlink()
            except OSError:
                return None
            continue
        except OSError as exc:
            say(f"[warn] 无法创建进程锁 {relative_posix(path)}：{exc}")
            return None
        try:
            os.write(handle, payload.encode("utf-8"))
        except OSError:  # pragma: no cover - 写不进 owner 文本不影响互斥语义
            pass
        return PipelineLock(path=path, handle=handle, payload=payload, stale_takeover=stale_reason)
    return None


# --------------------------------------------------------------------------- #
# 输出透传
# --------------------------------------------------------------------------- #


def say(text: str) -> None:
    """人类可读文字：一律走 stdout，且始终在最后一行 JSON 之前。"""
    with _PRINT_LOCK:
        print(text, flush=True)


def pump(stream: Any, prefix: str, sink: Any, tail: deque[str]) -> None:
    """逐行透传子进程输出（实时刻码，行缓冲）。"""
    try:
        for raw in stream:
            line = str(raw).rstrip("\r\n")
            if line.strip():
                tail.append(line.strip()[:400])
            with _PRINT_LOCK:
                sink.write(f"{prefix}{line}\n")
                sink.flush()
    except (OSError, ValueError):  # 子进程被终止后管道关闭
        pass
    finally:
        try:
            stream.close()
        except (OSError, ValueError):
            pass


def terminate(proc: subprocess.Popen[str]) -> None:
    """先礼后兵：Windows 发 CTRL_BREAK（子进程是独立进程组），POSIX 发 SIGTERM。"""
    if proc.poll() is not None:
        return
    if os.name == "nt":
        ctrl_break = getattr(signal, "CTRL_BREAK_EVENT", None)
        if ctrl_break is not None:
            try:
                os.kill(proc.pid, ctrl_break)
                time.sleep(2.0)
            except OSError:
                pass
    else:
        try:
            proc.terminate()
            time.sleep(2.0)
        except OSError:
            pass
    if proc.poll() is None:
        try:
            proc.kill()
        except OSError:  # pragma: no cover
            pass


# --------------------------------------------------------------------------- #
# 阶段执行与判定
# --------------------------------------------------------------------------- #


class StageInterrupted(Exception):
    """阶段被人工打断，已带好记账信息。"""

    def __init__(self, name: str, record: dict[str, Any]) -> None:
        super().__init__(f"stage {name} interrupted")
        self.name = name
        self.record = record


def judge_manifest(path: Path) -> tuple[bool, str, dict[str, Any], str]:
    """读子程序本次生成的 manifest，返回 (是否算成功, status, summary, 失败原因)。"""
    if not path.is_file():
        return False, "", {}, f"子程序退出前没有生成 manifest：{relative_posix(path)}"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return False, "", {}, f"manifest 无法解析：{path.name} -> {exc}"
    if not isinstance(payload, dict):
        return False, "", {}, f"manifest 顶层不是 JSON 对象：{path.name}"
    status = str(payload.get("status") or "").strip().lower()
    raw_summary = payload.get("summary")
    summary: dict[str, Any] = raw_summary if isinstance(raw_summary, dict) else {}
    reasons: list[str] = []
    if status not in SUCCESS_STATUSES:
        reasons.append(f"manifest status={status or '缺失'} 不是成功态")
    try:
        failed = int(summary.get("failed") or 0)
    except (TypeError, ValueError):
        failed = 1
        reasons.append(f"summary.failed 不是整数（{summary.get('failed')!r}）")
    if failed > 0:
        reasons.append(f"summary.failed={failed}")
    own_error = str(payload.get("error") or "").strip()
    if reasons and own_error:
        reasons.append(f"子程序报错：{own_error[:600]}")
    return (not reasons), status, summary, "；".join(reasons)


def run_stage(
    stage: dict[str, str],
    *,
    manifest_path: Path,
    db_path: Path,
    timeout: int,
) -> dict[str, Any]:
    """执行一个阶段：透传输出 + 退出码与 manifest 双重判定，绝不吞错。"""
    prefix = stage["prefix"]
    argv = [
        sys.executable,
        str(ROOT / stage["script"]),
        "--manifest",
        str(manifest_path),
        "--db-path",
        str(db_path),
    ]
    recorded_argv = [
        "python",
        stage["script"],
        "--manifest",
        relative_posix(manifest_path),
        "--db-path",
        relative_posix(db_path),
    ]
    started_at = now_str()
    started_perf = time.perf_counter()
    tail: deque[str] = deque(maxlen=20)
    record: dict[str, Any] = {
        "name": stage["name"],
        "label": stage["label"],
        "script": stage["script"],
        "argv": recorded_argv,
        "manifest_path": relative_posix(manifest_path),
        "exit_code": None,
        "status": "failed",
        "manifest_status": "",
        "summary": {},
        "error": "",
        "started_at": started_at,
        "ended_at": "",
        "duration_seconds": 0.0,
        "timeout_seconds": timeout,
        "output_tail": [],
    }
    say(f":: {stage['label']} -> {stage['script']}")
    try:
        proc = subprocess.Popen(
            argv,
            cwd=str(ROOT),
            env=child_env(),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="replace",
            bufsize=1,
            **popen_kwargs(),
        )
    except OSError as exc:
        record["error"] = f"无法启动子进程 {stage['script']}：{exc}"
        record["ended_at"] = now_str()
        say(f"× {record['error']}")
        return record

    out_thread = threading.Thread(
        target=pump, args=(proc.stdout, prefix, sys.stdout, tail), daemon=True
    )
    err_thread = threading.Thread(
        target=pump, args=(proc.stderr, prefix, sys.stderr, tail), daemon=True
    )
    out_thread.start()
    err_thread.start()
    timed_out = ""
    try:
        exit_code: int | None = proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        terminate(proc)
        try:
            exit_code = proc.wait(timeout=30)
        except subprocess.TimeoutExpired:  # pragma: no cover - kill 之后应当立刻回收
            exit_code = None
        timed_out = f"阶段超过 {timeout}s 未完成，已终止子进程"
    except KeyboardInterrupt:  # 人工打断：先收拾子进程，再交给 main 如实记账
        terminate(proc)
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:  # pragma: no cover
            proc.kill()
        for thread in (out_thread, err_thread):
            thread.join(timeout=2)
        record["exit_code"] = proc.poll()
        record["ended_at"] = now_str()
        record["duration_seconds"] = round(time.perf_counter() - started_perf, 3)
        record["output_tail"] = list(tail)
        raise StageInterrupted(stage["name"], record) from None

    for thread in (out_thread, err_thread):
        thread.join(timeout=60)

    record["exit_code"] = exit_code
    record["ended_at"] = now_str()
    record["duration_seconds"] = round(time.perf_counter() - started_perf, 3)
    record["output_tail"] = list(tail)[-10:]

    ok, manifest_status, summary, reason = judge_manifest(manifest_path)
    record["manifest_status"] = manifest_status
    record["summary"] = summary
    problems: list[str] = []
    if timed_out:
        problems.append(timed_out)
    if exit_code != 0:
        problems.append(f"退出码 {exit_code}")
    if not ok:
        problems.append(reason)
    if problems:
        record["status"] = "failed"
        record["error"] = "，并且".join(problems)
        say(f"× {stage['label']} 失败：{record['error']}")
        return record
    if timed_out:  # 理论上不可达，保险起见不静默
        record["status"] = "failed"
        record["error"] = timed_out
        say(f"× {stage['label']} 失败：{timed_out}")
        return record
    record["status"] = "ok"
    say(f"√ {stage['label']} 成功（manifest status={manifest_status}）")
    return record


def not_run_record(stage: dict[str, str], manifest_path: Path, why: str) -> dict[str, Any]:
    """上游失败时，下游阶段的占位记录（保证 stages[] 完整可诊断）。"""
    return {
        "name": stage["name"],
        "label": stage["label"],
        "script": stage["script"],
        "argv": [],
        "manifest_path": relative_posix(manifest_path),
        "exit_code": None,
        "status": "not_run",
        "manifest_status": "",
        "summary": {},
        "error": why,
        "started_at": "",
        "ended_at": "",
        "duration_seconds": 0.0,
        "timeout_seconds": None,
        "output_tail": [],
    }


def aggregate_summary(stages: list[dict[str, Any]]) -> dict[str, int]:
    """把各阶段 manifest 的计数合并成管线级 summary。"""
    summary = {
        "stages_total": len(stages),
        "stages_ok": 0,
        "stages_failed": 0,
        "created": 0,
        "updated": 0,
        "skipped": 0,
        "failed": 0,
    }
    for record in stages:
        if record.get("status") == "ok":
            summary["stages_ok"] += 1
        else:
            summary["stages_failed"] += 1
        raw = record.get("summary")
        data: dict[str, Any] = raw if isinstance(raw, dict) else {}
        for key in ("created", "updated"):
            try:
                summary[key] += int(data.get(key) or 0)
            except (TypeError, ValueError):
                pass
        try:
            summary["skipped"] += int(data.get("skipped") or 0) + int(
                data.get("skipped_existing") or 0
            )
        except (TypeError, ValueError):
            pass
    summary["failed"] = summary["stages_failed"]
    return summary


def build_payload(
    *,
    run_id: str,
    started_at: str,
    stages: list[dict[str, Any]],
    db_path: Path,
    pipeline_manifest: Path,
    status: str,
    extra_error: str = "",
) -> dict[str, Any]:
    """管线 manifest / stdout 最后一行共用的 payload。"""
    summary = aggregate_summary(stages)
    failures = [
        f"{record['name']}（{record['script']}）：{record.get('error') or record.get('status')}"
        for record in stages
        if record.get("status") != "ok"
    ]
    if extra_error and not failures:
        failures.append(extra_error)
    ended_at = now_str()
    notes = [
        "入口契约见 FEISHU_LOCAL_DATABASE.md :146 :160 :211；排错沿 stages[].manifest_path 下钻（文档 :229）",
    ]
    if status != "success":
        notes.append("本管线不伪造成功：任何阶段未达标都整体判为 failed，且下游阶段不会启动")
    return {
        "kind": "feishu_local_pipeline",
        "task": "run_feishu_local_sync",
        "run_id": run_id,
        "status": status,
        "mode": "daily-incremental",
        "started_at": started_at,
        "ended_at": ended_at,
        "duration_seconds": round(
            max(
                0.0,
                (parse_ts(ended_at) - parse_ts(started_at)).total_seconds()
                if (parse_ts(ended_at) and parse_ts(started_at))
                else 0.0,
            ),
            3,
        ),
        "db_path": relative_posix(db_path),
        "manifest_dir": relative_posix(pipeline_manifest.parent),
        "manifest_path": relative_posix(pipeline_manifest),
        "stages": stages,
        "summary": summary,
        "failures": failures,
        "error": extra_error or (failures[0] if failures else None),
        "notes": notes,
    }


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #


def build_parser() -> argparse.ArgumentParser:
    epilog = (
        "示例：\n"
        "  python run_feishu_local_sync.py\n"
        "  python run_feishu_local_sync.py --db-path downloads/feishu-base/feishu-base.sqlite3\n"
        "\n退出码：0 两阶段都成功 / 1 任一阶段失败（生成 status=failed 的 "
        "*-feishu-local-pipeline.json）/ 2 已有管线在跑，拒绝重叠执行\n"
        "阶段一 sync_feishu_base_to_local.py 失败时阶段二 sync_feishu_transcript_docs_to_local.py 不会启动。\n"
        "stdout 最后一行是单行 JSON payload，人类可读文字全部在它之前。"
    )
    parser = argparse.ArgumentParser(
        prog="run_feishu_local_sync.py",
        description=(
            "每日飞书本地同步管线：先增量镜像 Base 五张表，成功后再镜像纯净口播稿；"
            "带跨平台进程锁，任一阶段失败都不运行下一阶段并写出 status=failed 的管线 manifest。"
        ),
        epilog=epilog,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=DEFAULT_STAGE_TIMEOUT,
        help=f"单个阶段的最长等待秒数（默认 {DEFAULT_STAGE_TIMEOUT}）",
    )
    parser.add_argument(
        "--manifest-dir",
        default=str(DEFAULT_MANIFEST_DIR),
        help=f"manifest 目录（默认 {DEFAULT_MANIFEST_DIR.relative_to(ROOT)}）",
    )
    parser.add_argument(
        "--db-path",
        default=str(DEFAULT_DB_PATH),
        help=f"转发给两个阶段的本地 SQLite 路径（默认 {DEFAULT_DB_PATH.relative_to(ROOT)}）",
    )
    parser.add_argument(
        "--lock-stale-seconds",
        type=int,
        default=LOCK_STALE_SECONDS,
        help=f"锁龄超过该秒数且持锁进程不在时视为遗留并抢占（默认 {LOCK_STALE_SECONDS}）",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(list(argv) if argv is not None else None)
    if args.timeout <= 0:
        say("× --timeout 必须是正整数")
        return EXIT_FAILED

    started_at = now_str()
    run_id = ts_slug()
    manifest_dir = Path(args.manifest_dir).expanduser().resolve()
    db_path = Path(args.db_path).expanduser().resolve()
    pipeline_manifest = manifest_dir / f"{run_id}-feishu-local-pipeline.json"
    stage_manifests = {
        stage["name"]: manifest_dir / f"{run_id}-{stage['manifest_suffix']}" for stage in STAGES
    }

    lock = acquire_pipeline_lock(args.lock_stale_seconds)
    if lock is None:
        pid, owner = read_lock_owner(LOCK_DIR / f"{LOCK_NAME}.lock")
        message = (
            f"已有一个 run_feishu_local_sync 正在运行"
            f"（锁 {relative_posix(LOCK_DIR / f'{LOCK_NAME}.lock')}"
            f"{f'，持有者 pid={pid}' if pid else ''}），本管线拒绝重叠执行。"
            f"请等它结束；确需强行接手，先确认那个进程真的已死，再删除该锁文件，"
            f"或用 --lock-stale-seconds 调低陈旧阈值。"
        )
        say(f"× {message}")
        payload = build_payload(
            run_id=run_id,
            started_at=started_at,
            stages=[
                not_run_record(
                    stage, stage_manifests[stage["name"]], "拒绝重叠执行，未启动任何阶段"
                )
                for stage in STAGES
            ],
            db_path=db_path,
            pipeline_manifest=pipeline_manifest,
            status="locked",
            extra_error=message,
        )
        payload["summary"]["failed"] = len(STAGES)
        payload["lock_path"] = relative_posix(LOCK_DIR / f"{LOCK_NAME}.lock")
        payload["manifest_written"] = False  # 没有任何阶段跑过，不产出 manifest
        print(json.dumps(payload, ensure_ascii=False), flush=True)
        return EXIT_LOCKED

    stages: list[dict[str, Any]] = []
    interrupted = False
    try:
        first = STAGES[0]
        try:
            base_record = run_stage(
                first,
                manifest_path=stage_manifests[first["name"]],
                db_path=db_path,
                timeout=args.timeout,
            )
        except StageInterrupted as exc:
            interrupted = True
            stages.append(exc.record)
            stages.append(
                not_run_record(
                    STAGES[1],
                    stage_manifests[STAGES[1]["name"]],
                    "上游阶段被人工打断，未启动",
                )
            )
        else:
            stages.append(base_record)
            if base_record["status"] == "ok":
                second = STAGES[1]
                try:
                    stages.append(
                        run_stage(
                            second,
                            manifest_path=stage_manifests[second["name"]],
                            db_path=db_path,
                            timeout=args.timeout,
                        )
                    )
                except StageInterrupted as exc:
                    interrupted = True
                    stages.append(exc.record)
            else:
                stages.append(
                    not_run_record(
                        STAGES[1],
                        stage_manifests[STAGES[1]["name"]],
                        f"阶段一（{first['script']}）未成功，按 FEISHU_LOCAL_DATABASE.md :160 不运行下一阶段",
                    )
                )
    finally:
        lock.release()

    success = not interrupted and all(record.get("status") == "ok" for record in stages)
    status = "success" if success else "failed"
    extra_error = "阶段被人工打断（KeyboardInterrupt）" if interrupted else ""
    payload = build_payload(
        run_id=run_id,
        started_at=started_at,
        stages=stages,
        db_path=db_path,
        pipeline_manifest=pipeline_manifest,
        status=status,
        extra_error=extra_error,
    )
    payload["lock_path"] = relative_posix(LOCK_DIR / f"{LOCK_NAME}.lock")
    if lock.stale_takeover:
        payload["notes"].append(f"抢占了上次异常退出遗留的进程锁：{lock.stale_takeover}")

    write_error = ""
    try:
        written = write_json(pipeline_manifest, payload)
        payload["manifest_path"] = relative_posix(written)
        payload["manifest_written"] = True
    except OSError as exc:  # 写不出 manifest 也是失败，必须说出来
        write_error = f"写出管线 manifest 失败：{exc}"
        say(f"× 无法写出管线 manifest {relative_posix(pipeline_manifest)}：{exc}")
        payload["manifest_written"] = False
        payload["error"] = ((payload.get("error") or "") + f"；{write_error}").lstrip("；")
        if success:  # 落不了盘就没有数据截止点，绝不允许报成功
            success = False
            payload["status"] = "failed"

    say(
        f":: 管线结束 status={payload['status']} 阶段 "
        + " / ".join(f"{r['name']}={r['status']}" for r in stages)
        + f"；manifest={payload['manifest_path']}"
    )
    # 最后一行：单行 JSON payload（extract_trailing_json 靠 summary.failed 命中它）。
    print(json.dumps(payload, ensure_ascii=False), flush=True)
    return EXIT_OK if success else EXIT_FAILED


if __name__ == "__main__":
    for _name in ("stdout", "stderr"):
        _stream = getattr(sys, _name, None)
        if hasattr(_stream, "reconfigure"):
            try:
                _stream.reconfigure(encoding="utf-8", errors="replace")
            except Exception:  # pragma: no cover
                pass
    raise SystemExit(main())
