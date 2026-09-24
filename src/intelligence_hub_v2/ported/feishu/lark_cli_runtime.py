# ruff: noqa
# TODO(v2-adapt): V1 自造的 lark-cli 子进程协议层，未适配。它的判断全绑在 V1 的假设上：用
# shutil.which 现读进程 PATH 去够 npm 全局的 lark-cli.exe（V2 的对应物是
# core/runtime_env.sync_path_from_registry，AGENTS.md §5 的 §7.19 就是它）、按 lark-cli 的**错误
# 文案**关键字（token_missing / keychain / dpapi）决定重试节奏、日志落在 <ROOT>/downloads/logs。
# 适配要做的那件事：改走 infra/subprocess.run_subprocess 并把失败原文交给 errors.PlatformError，
# 日志目录取 storage/files.FileStorage.logs_dir，PATH 解析交给 core/runtime_env 而不是自己 which。
import ctypes
import getpass
import json
import os
import shutil
import subprocess
import sys
import time
from datetime import datetime
from functools import lru_cache
from pathlib import Path
from typing import Callable, Sequence


ROOT = Path(__file__).resolve().parent
DEFAULT_LOG_DIR = ROOT / "downloads" / "logs"
DEFAULT_CREDENTIAL_RETRY_DELAYS = (2.0, 5.0, 10.0, 20.0)

_LARK_CLI_NAMES = {"lark-cli", "lark-cli.exe", "lark-cli.cmd", "lark-cli.ps1", "lark-cli.bat"}
_TRANSIENT_CREDENTIAL_SUBTYPES = {
    "token_missing",
    "missing_client_secret",
    "credential_unavailable",
    "credential_store_unavailable",
    "keychain_error",
}
_TRANSIENT_CREDENTIAL_MARKERS = (
    "token_missing",
    "missing client_secret",
    "missing_client_secret",
    "client secret is missing",
    "keychain access",
    "failed to access keychain",
    "failed to read keychain",
    "dpapi",
)
# 这些词太泛（API 错误正文也可能提到），单独命中不足以判定为本地凭证读取失败
_GENERIC_CREDENTIAL_MARKERS = (
    "credential store",
    "secure storage",
    "secure store",
)


def command_env(base: dict[str, str] | None = None) -> dict[str, str]:
    env = dict(os.environ if base is None else base)
    env.pop("HERMES_HOME", None)
    env.pop("HERMES_GIT_BASH_PATH", None)
    env["LARK_CLI_NO_PROXY"] = "1"
    env["LARKSUITE_CLI_NO_UPDATE_NOTIFIER"] = "1"
    env["LARKSUITE_CLI_NO_SKILLS_NOTIFIER"] = "1"
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    return env


def is_lark_cli_command(args: Sequence[str | os.PathLike[str]]) -> bool:
    if not args:
        return False
    return Path(os.fspath(args[0])).name.lower() in _LARK_CLI_NAMES


def resolve_lark_cli_binary(env: dict[str, str] | None = None) -> Path:
    effective_env = os.environ if env is None else env
    candidates: list[Path] = []
    configured = effective_env.get("LARK_CLI_BINARY")
    if configured:
        candidates.append(Path(configured))

    search_path = effective_env.get("PATH")
    for name in ("lark-cli.exe", "lark-cli.cmd", "lark-cli.ps1", "lark-cli"):
        found = shutil.which(name, path=search_path)
        if not found:
            continue
        path = Path(found)
        candidates.append(path)
        if path.suffix.lower() == ".exe":
            candidates.append(
                path.parent / "node_modules" / "@larksuite" / "cli" / "bin" / "lark-cli.exe"
            )

    seen: set[str] = set()
    for candidate in candidates:
        key = os.path.normcase(os.path.abspath(candidate))
        if key in seen:
            continue
        seen.add(key)
        if candidate.is_file() and (
            candidate.suffix.lower() == ".exe" or os.access(candidate, os.X_OK)
        ):
            return candidate.resolve()
    raise RuntimeError(
        "Cannot locate the native lark-cli executable. "
        "Install it with `npm install -g @larksuite/cli` or set LARK_CLI_BINARY."
    )


def normalize_lark_cli_command(
    args: Sequence[str | os.PathLike[str]],
    *,
    env: dict[str, str] | None = None,
) -> list[str]:
    if not is_lark_cli_command(args):
        raise ValueError("normalize_lark_cli_command requires a lark-cli command")
    requested = Path(os.fspath(args[0]))
    executable = (
        requested.resolve()
        if requested.is_file() and os.access(requested, os.X_OK)
        else resolve_lark_cli_binary(env)
    )
    return [str(executable), *(os.fspath(value) for value in args[1:])]


def _json_envelope(text: str) -> dict:
    stripped = str(text or "").strip()
    if not stripped:
        return {}
    try:
        payload = json.loads(stripped)
        return payload if isinstance(payload, dict) else {}
    except json.JSONDecodeError:
        start = stripped.find("{")
        if start < 0:
            return {}
        try:
            payload, _ = json.JSONDecoder().raw_decode(stripped[start:])
        except json.JSONDecodeError:
            return {}
        return payload if isinstance(payload, dict) else {}


def classify_lark_cli_failure(result: subprocess.CompletedProcess[str]) -> dict[str, object]:
    payload = _json_envelope(result.stderr) or _json_envelope(result.stdout)
    error = payload.get("error") if isinstance(payload.get("error"), dict) else {}
    subtype = str(error.get("subtype") or "").strip().lower()
    error_type = str(error.get("type") or "").strip().lower()
    code = error.get("code")
    combined = f"{result.stderr}\n{result.stdout}".lower()
    markers = sorted({marker for marker in _TRANSIENT_CREDENTIAL_MARKERS if marker in combined})
    generic_markers = [marker for marker in _GENERIC_CREDENTIAL_MARKERS if marker in combined]
    # 泛化词必须佐证（JSON 错误信封或命中第二个词）才算凭证类，避免 API 错误正文碰巧含
    # "credential store" 被误判为可重试的本地读取失败。
    corroborated = bool(markers) or len(generic_markers) >= 2 or (generic_markers and bool(payload))
    retryable = subtype in _TRANSIENT_CREDENTIAL_SUBTYPES or corroborated
    return {
        "category": "transient_credential_read" if retryable else "non_retryable",
        "retryable": retryable,
        "error_type": error_type or None,
        "error_subtype": subtype or None,
        "error_code": code,
        "markers": markers + generic_markers,
    }


def credential_retry_delays(env: dict[str, str] | None = None) -> tuple[float, ...]:
    effective_env = os.environ if env is None else env
    value = effective_env.get("LARK_CLI_CREDENTIAL_RETRY_DELAYS")
    if not value:
        return DEFAULT_CREDENTIAL_RETRY_DELAYS
    delays: list[float] = []
    for item in value.split(","):
        try:
            delay = float(item.strip())
        except ValueError as exc:
            raise RuntimeError(
                "LARK_CLI_CREDENTIAL_RETRY_DELAYS must be a comma-separated list of seconds"
            ) from exc
        if delay < 0:
            raise RuntimeError("LARK_CLI_CREDENTIAL_RETRY_DELAYS values must be non-negative")
        delays.append(delay)
    return tuple(delays)


def _profile_from_args(args: Sequence[str]) -> str | None:
    try:
        index = args.index("--profile")
    except ValueError:
        return None
    return args[index + 1] if index + 1 < len(args) else None


def _safe_operation(args: Sequence[str]) -> str:
    services = {"auth", "base", "docs", "drive", "wiki", "config", "doctor", "whoami"}
    for index, value in enumerate(args[1:], start=1):
        if value not in services:
            continue
        following = args[index + 1] if index + 1 < len(args) else ""
        if following and not following.startswith("--"):
            return f"{value} {following}"
        return value
    if "--version" in args:
        return "version"
    return "unknown"


@lru_cache(maxsize=1)
def _process_identity() -> dict[str, str | None]:
    username = getpass.getuser()
    sid = None
    sid_error = None
    if os.name == "nt":
        try:
            from ctypes import wintypes

            token_query = 0x0008
            token_user_class = 1
            token = wintypes.HANDLE()
            kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
            advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
            kernel32.GetCurrentProcess.restype = wintypes.HANDLE
            kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
            kernel32.CloseHandle.restype = wintypes.BOOL
            kernel32.LocalFree.argtypes = [wintypes.HLOCAL]
            kernel32.LocalFree.restype = wintypes.HLOCAL
            advapi32.OpenProcessToken.argtypes = [
                wintypes.HANDLE,
                wintypes.DWORD,
                ctypes.POINTER(wintypes.HANDLE),
            ]
            advapi32.OpenProcessToken.restype = wintypes.BOOL
            advapi32.GetTokenInformation.argtypes = [
                wintypes.HANDLE,
                wintypes.DWORD,
                wintypes.LPVOID,
                wintypes.DWORD,
                ctypes.POINTER(wintypes.DWORD),
            ]
            advapi32.GetTokenInformation.restype = wintypes.BOOL
            advapi32.ConvertSidToStringSidW.argtypes = [
                wintypes.LPVOID,
                ctypes.POINTER(wintypes.LPWSTR),
            ]
            advapi32.ConvertSidToStringSidW.restype = wintypes.BOOL
            if not advapi32.OpenProcessToken(
                kernel32.GetCurrentProcess(),
                token_query,
                ctypes.byref(token),
            ):
                raise OSError(ctypes.get_last_error(), "OpenProcessToken failed")
            try:
                required = wintypes.DWORD()
                advapi32.GetTokenInformation(
                    token,
                    token_user_class,
                    None,
                    0,
                    ctypes.byref(required),
                )
                buffer = ctypes.create_string_buffer(required.value)
                if not advapi32.GetTokenInformation(
                    token,
                    token_user_class,
                    buffer,
                    required,
                    ctypes.byref(required),
                ):
                    raise OSError(ctypes.get_last_error(), "GetTokenInformation failed")

                class SidAndAttributes(ctypes.Structure):
                    _fields_ = [("sid", ctypes.c_void_p), ("attributes", wintypes.DWORD)]

                token_user = ctypes.cast(buffer, ctypes.POINTER(SidAndAttributes)).contents
                sid_text = wintypes.LPWSTR()
                if not advapi32.ConvertSidToStringSidW(token_user.sid, ctypes.byref(sid_text)):
                    raise OSError(ctypes.get_last_error(), "ConvertSidToStringSidW failed")
                try:
                    sid = sid_text.value
                finally:
                    kernel32.LocalFree(sid_text)
            finally:
                kernel32.CloseHandle(token)
        except (AttributeError, OSError, ValueError) as exc:
            sid_error = f"{type(exc).__name__}: {exc}"
    return {"username": username, "sid": sid, "sid_error": sid_error}


def _audit_log_path(env: dict[str, str]) -> Path:
    configured = env.get("LARK_CLI_RUNTIME_LOG")
    if configured:
        return Path(configured)
    return DEFAULT_LOG_DIR / f"lark-cli-runtime-{datetime.now():%Y-%m-%d}.jsonl"


def _write_audit_event(event: dict[str, object], env: dict[str, str]) -> None:
    path = _audit_log_path(env)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(event, ensure_ascii=False, separators=(",", ":")) + "\n")
    except OSError as exc:
        print(
            f"[warn] unable to write lark-cli runtime audit log {path}: {exc}",
            file=sys.stderr,
        )


def _redacted_error_tail(result: subprocess.CompletedProcess[str], args: Sequence[str]) -> str:
    text = (result.stderr or result.stdout or "no error details")[-2000:]
    sensitive_flags = {
        "--access-token",
        "--app-secret",
        "--base-token",
        "--client-secret",
        "--refresh-token",
        "--tenant-access-token",
    }
    secrets = {
        args[index + 1]
        for index, value in enumerate(args[:-1])
        if value in sensitive_flags and args[index + 1]
    }
    for secret in secrets:
        text = text.replace(secret, "<redacted>")
    return text


def run_lark_cli_command(
    args: Sequence[str | os.PathLike[str]],
    *,
    cwd: str | os.PathLike[str],
    env: dict[str, str] | None = None,
    timeout: float | None = None,
    check: bool = True,
    retry_delays: Sequence[float] | None = None,
    runner: Callable[..., subprocess.CompletedProcess[str]] | None = None,
    sleep_fn: Callable[[float], None] | None = None,
) -> subprocess.CompletedProcess[str]:
    effective_env = command_env(env)
    normalized = normalize_lark_cli_command(args, env=effective_env)
    delays = tuple(credential_retry_delays(effective_env) if retry_delays is None else retry_delays)
    execute = runner or subprocess.run
    wait = sleep_fn or time.sleep
    max_attempts = len(delays) + 1
    result: subprocess.CompletedProcess[str] | None = None
    attempts_used = 0
    classification: dict[str, object] = {
        "category": "not_run",
        "retryable": False,
        "error_type": None,
        "error_subtype": None,
        "error_code": None,
        "markers": [],
    }

    for attempt in range(1, max_attempts + 1):
        attempts_used = attempt
        started = time.perf_counter()
        result = execute(
            normalized,
            cwd=cwd,
            env=effective_env,
            text=True,
            encoding="utf-8",
            errors="replace",
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
        )
        classification = (
            {
                "category": "success",
                "retryable": False,
                "error_type": None,
                "error_subtype": None,
                "error_code": None,
                "markers": [],
            }
            if result.returncode == 0
            else classify_lark_cli_failure(result)
        )
        _write_audit_event(
            {
                "timestamp": datetime.now().astimezone().isoformat(timespec="milliseconds"),
                "executable": normalized[0],
                "profile": _profile_from_args(normalized),
                "operation": _safe_operation(normalized),
                "attempt": attempt,
                "max_attempts": max_attempts,
                "elapsed_seconds": round(time.perf_counter() - started, 3),
                "returncode": result.returncode,
                "classification": classification["category"],
                "error_type": classification["error_type"],
                "error_subtype": classification["error_subtype"],
                "error_code": classification["error_code"],
                "process": _process_identity(),
            },
            effective_env,
        )
        if result.returncode == 0:
            return result
        if not classification["retryable"] or attempt >= max_attempts:
            break
        delay = float(delays[attempt - 1])
        print(
            "[warn] transient lark-cli credential read failure; "
            f"starting a new process in {delay:g}s ({attempt}/{max_attempts - 1} retries)",
            file=sys.stderr,
        )
        wait(delay)

    assert result is not None
    if check:
        raise RuntimeError(
            "lark-cli failed "
            f"(classification={classification['category']}, exit={result.returncode}, "
            f"attempts={attempts_used}); "
            f"details={_redacted_error_tail(result, normalized)!r}"
        )
    return result
