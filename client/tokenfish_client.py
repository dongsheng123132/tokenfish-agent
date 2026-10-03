#!/usr/bin/env python3
"""TokenFish agent-native client.

Single-file, Python 3.10+ standard-library-only client that lets an AI agent
call TokenFish (backed by 虾盘云 / u-claw) capabilities: wallet balance,
model listing, music generation (Suno / MiniMax), image generation, and task
polling.

Design constraints (see SPEC.md, authoritative):
  - stdout carries ONLY JSON. All diagnostics go to stderr.
  - The API key must never appear in stdout, stderr, logs, exceptions,
    command-line arguments, or environment variables.
  - No third-party SDKs; stdlib only (urllib, json, hashlib, ...).
  - Exit codes: 0 ok, 1 other, 2 bad arguments, 3 user action required
    (not logged in / insufficient balance / `outcome_unknown` = the request
    may already have been charged, do NOT retry / `local_state_unwritable` =
    nothing was submitted, fix TOKENFISH_HOME), 4 transient (safe to retry).
  - Never double-charge: anything that may already have been charged is never
    reported as retryable. A paid submit (music / images) is preceded by a
    write-ahead record in the local state dir, failures are classified as
    rejected / not-sent / maybe-sent, and a maybe-sent request surfaces as
    `outcome_unknown` (see SPEC.md "附：防重复扣费"). The state dir is
    $TOKENFISH_HOME -> ~/.tokenfish -> <tmp>/tokenfish-<user> -> ./.tokenfish,
    first writable (writes); lookups read the union of every candidate's
    tasks.json so a record is found whatever the cwd; image output dir is $TOKENFISH_OUTPUT_DIR ->
    ./tokenfish-outputs -> <state_dir>/outputs, first writable.

This module is designed to be copied into `scripts/tokenfish_client.py`
inside a Skill package by build/build.py. When run standalone (e.g. from
client/ for testing) it still works; package_slug attribution falls back to
"tokenfish-client".
"""

from __future__ import annotations

import argparse
import base64
import errno
import getpass
import hashlib
import http.client
import json
import os
import platform as platform_mod
import re
import shutil
import socket
import ssl
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Optional

CLIENT_VERSION = "0.1.1"

DEFAULT_API_BASE = "https://api.u-claw.org.cn"
TOPUP_URL = "https://api.u-claw.org.cn/recharge"

QUOTA_PER_CNY = 500000

DEFAULT_TIMEOUT = 30
MINIMAX_TIMEOUT = 240

HOME_STATE_DIR = Path.home() / ".tokenfish"
CREDENTIALS_PATH = HOME_STATE_DIR / "credentials.json"
UKING_DEVICE_PATH = Path.home() / ".uking" / "device.json"

# Default image output dir (relative to cwd); see output_dir() for fallbacks.
OUTPUT_DIR = Path("tokenfish-outputs")

IDEMPOTENCY_KEY_RE = re.compile(r"^[A-Za-z0-9\-_.]{12,128}$")

MUSIC_MODELS = ("auto", "suno", "minimax")
TASK_PROVIDERS = ("suno", "minimax")
IMAGE_QUALITIES = ("low", "medium", "high", "auto")
CAPABILITIES = ("text_to_music", "text_to_image")


# --------------------------------------------------------------------------
# Errors
# --------------------------------------------------------------------------


class ToolError(Exception):
    """Carries a normalized error payload + process exit code."""

    def __init__(self, code: str, message: str, retryable: bool, next_action: str, exit_code: int):
        super().__init__(message)
        self.code = code
        self.message = message
        self.retryable = retryable
        self.next_action = next_action
        self.exit_code = exit_code

    def to_payload(self) -> dict:
        return {
            "error": {
                "code": self.code,
                "message": self.message,
                "retryable": self.retryable,
                "next_action": self.next_action,
            }
        }


def err_not_logged_in() -> ToolError:
    return ToolError(
        "not_logged_in",
        "未找到凭据。请先运行 `login --key-file <path>` 完成登录。",
        False,
        "运行 `python tokenfish_client.py login --key-file <path>` 完成登录",
        3,
    )


def err_insufficient_balance() -> ToolError:
    return ToolError(
        "insufficient_balance",
        "余额不足，本次未扣费。请运行 topup 充值，充值后可用同一 client_request_id 原样重试。",
        False,
        "运行 `python tokenfish_client.py topup` 充值后，用同一 client_request_id 重试",
        3,
    )


def err_auth_invalid() -> ToolError:
    return ToolError(
        "auth_invalid",
        "凭据无效，请重新 login。",
        False,
        "运行 `python tokenfish_client.py login --key-file <path>` 重新登录",
        3,
    )


def err_rate_limited() -> ToolError:
    return ToolError(
        "rate_limited",
        "请求过于频繁，请稍后重试。",
        True,
        "稍等后用同一 client_request_id 重试",
        4,
    )


def err_transient(detail: str = "") -> ToolError:
    msg = "服务暂时不可用" + (f"：{detail}" if detail else "") + "。用同一 client_request_id 原样重试。"
    return ToolError("transient", msg, True, "用同一 client_request_id 原样重试", 4)


def err_invalid_arguments(field: str, message: str) -> ToolError:
    return ToolError(
        "invalid_arguments",
        f"参数 `{field}` 不合法：{message}",
        False,
        "修正参数后重新调用",
        2,
    )


def err_idempotency_conflict(client_request_id: str) -> ToolError:
    return ToolError(
        "idempotency_conflict",
        f"client_request_id `{client_request_id}` 此前已用于一次不同参数的请求，本次未提交。",
        False,
        "换一个新的 client_request_id，或使用与首次提交完全一致的参数",
        2,
    )


def err_outcome_unknown(detail: str = "") -> ToolError:
    """The request may already have been submitted and charged. NEVER retryable."""
    msg = (
        "无法确认本次请求的结果：请求可能已经提交并已扣费。"
        "请不要用同一个或新的 client_request_id 重试，先让用户核对钱包余额和流水。"
    )
    if detail:
        msg += f"（{detail}）"
    return ToolError(
        "outcome_unknown",
        msg,
        False,
        "先运行 `python tokenfish_client.py call tokenfish.wallet.get` 查看余额，"
        "并运行 `call tokenfish.tasks.list` 查看本地任务记录，对照钱包流水；"
        "确认确实没有扣费之前不要重试，也不要换新的 client_request_id",
        3,
    )


def err_local_state_unwritable(for_login: bool = False) -> ToolError:
    if for_login:
        msg = "本地状态目录不可写，凭据未保存。请把环境变量 TOKENFISH_HOME 设为一个可写目录后重新运行 login。"
        action = "设置环境变量 TOKENFISH_HOME 为一个可写目录后，重新运行 `login --key-file <path>`"
    else:
        msg = (
            "本地状态目录不可写，本次未提交、未扣费。"
            "请把环境变量 TOKENFISH_HOME 设为一个可写目录后再试。"
        )
        action = "设置环境变量 TOKENFISH_HOME 为一个可写目录后，用同一个 client_request_id 重新调用"
    return ToolError("local_state_unwritable", msg, False, action, 3)


def err_unknown_task(task_id: str) -> ToolError:
    return ToolError(
        "invalid_arguments",
        f"未找到本地记录的任务 `{task_id}`。",
        False,
        "确认 task_id 来自本客户端此前的返回值；如果 task_id 确实有效但本地没有记录，"
        "请同时传 `provider`（取返回结果里的 provider 值）直接查询",
        2,
    )


def err_unknown_tool(name: str) -> ToolError:
    return ToolError(
        "invalid_arguments",
        f"未知工具 `{name}`。运行 `tools` 命令查看可用工具。",
        False,
        "运行 `tools` 查看可用工具名单",
        2,
    )


# --------------------------------------------------------------------------
# Local state dir / output dir (a read-only home must never break idempotency)
# --------------------------------------------------------------------------

_STATE_DIR_CACHE: dict = {}


def _dir_is_writable(path: Path) -> bool:
    """True if `path` can be created AND written (probe: create + delete a
    temp file). Called through the module global so tests can stub it."""
    try:
        path.mkdir(parents=True, exist_ok=True, mode=0o700)
        fd, name = tempfile.mkstemp(prefix=".probe-", suffix=".tmp", dir=str(path))
    except OSError:
        return False
    try:
        os.close(fd)
    except OSError:
        pass
    try:
        os.remove(name)
    except OSError:
        pass
    return True


def _username() -> str:
    try:
        name = getpass.getuser()
    except Exception:
        name = os.environ.get("USERNAME") or os.environ.get("USER") or "user"
    return re.sub(r'[\\/:*?"<>|\s]', "_", name) or "user"


def _owned_by_me(path: Path) -> bool:
    """POSIX only: a shared tmp dir pre-created by someone else is not ours."""
    if not hasattr(os, "getuid"):
        return True
    try:
        return path.stat().st_uid == os.getuid()
    except OSError:
        return False


def _tmp_state_dir() -> Path:
    return Path(tempfile.gettempdir()) / f"tokenfish-{_username()}"


def state_dir_candidates() -> list:
    """Priority order. tmp comes BEFORE cwd on purpose: the tmp dir is the same
    from every working directory, so a record written there is found again
    even if the AI tool changes cwd between calls."""
    cands = []
    env = os.environ.get("TOKENFISH_HOME")
    if env:
        cands.append(Path(env))
    cands.append(HOME_STATE_DIR)
    cands.append(_tmp_state_dir())
    try:
        cands.append(Path.cwd() / ".tokenfish")
    except OSError:  # cwd was deleted
        pass
    return cands


def state_dir() -> Optional[Path]:
    """First writable of: $TOKENFISH_HOME, ~/.tokenfish,
    <tmp>/tokenfish-<user>, <cwd>/.tokenfish. Cached per process; None if none
    is writable. (Reads don't use this alone: see _load_merged_task_store.)"""
    if "path" in _STATE_DIR_CACHE:
        return _STATE_DIR_CACHE["path"]
    chosen = None
    tmp_dir = _tmp_state_dir()
    for cand in state_dir_candidates():
        if not _dir_is_writable(cand):
            continue
        if cand == tmp_dir and not _owned_by_me(cand):
            continue  # shared tmp: only trust a dir we own (POSIX)
        chosen = cand
        break
    _STATE_DIR_CACHE["path"] = chosen
    return chosen


def reset_state_dir_cache() -> None:
    _STATE_DIR_CACHE.clear()


def output_dir() -> Optional[Path]:
    """First writable of: $TOKENFISH_OUTPUT_DIR, ./tokenfish-outputs,
    <state_dir>/outputs. None if none is writable."""
    cands = []
    env = os.environ.get("TOKENFISH_OUTPUT_DIR")
    if env:
        cands.append(Path(env))
    cands.append(OUTPUT_DIR)
    base = state_dir()
    if base is not None:
        cands.append(base / "outputs")
    for cand in cands:
        if _dir_is_writable(cand):
            return cand
    return None


# --------------------------------------------------------------------------
# Credentials
# --------------------------------------------------------------------------


def _tighten_permissions(path: Path) -> None:
    """Best-effort: restrict a credentials file to the current user only."""
    try:
        if os.name == "nt":
            username = os.environ.get("USERNAME") or os.environ.get("USER")
            icacls = shutil.which("icacls")
            if icacls and username:
                subprocess.run(
                    [icacls, str(path), "/inheritance:r", "/grant:r", f"{username}:F"],
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    check=False,
                )
            else:
                print("警告：未能通过 icacls 收紧凭据文件权限（icacls 或用户名不可用）。", file=sys.stderr)
        else:
            os.chmod(path, 0o600)
    except OSError as exc:
        print(f"警告：收紧凭据文件权限失败：{exc}", file=sys.stderr)


def save_credentials(key: str) -> Path:
    """Saves the key into the resolved writable state dir (normally
    ~/.tokenfish). Returns the file path; raises OSError if no state dir is
    writable."""
    base = state_dir()
    if base is None:
        raise OSError("no writable state dir")
    path = base / "credentials.json"
    try:
        if os.name != "nt":
            os.chmod(base, 0o700)
    except OSError:
        pass
    fd, tmp_name = tempfile.mkstemp(prefix=".credentials-", suffix=".tmp", dir=str(base))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump({"key": key}, fh)
        os.replace(tmp_name, path)
    finally:
        if os.path.exists(tmp_name):
            try:
                os.remove(tmp_name)
            except OSError:
                pass
    _tighten_permissions(path)
    return path


def _credential_paths():
    """Lazily yields where to look for credentials.json, highest priority
    first: $TOKENFISH_HOME, ~/.tokenfish, then the resolved state dir (where
    `login` saves when ~/.tokenfish isn't writable). Lazy so the common case
    (file in ~/.tokenfish) never probes/creates any directory."""
    seen = set()
    if os.environ.get("TOKENFISH_HOME"):
        base = state_dir()
        if base is not None:
            seen.add(base / "credentials.json")
            yield base / "credentials.json"
    if CREDENTIALS_PATH not in seen:
        seen.add(CREDENTIALS_PATH)
        yield CREDENTIALS_PATH
    base = state_dir()
    if base is not None and (base / "credentials.json") not in seen:
        yield base / "credentials.json"


def load_credentials() -> Optional[str]:
    """Returns the API key, or None. Never writes ~/.uking/device.json."""
    for path in _credential_paths():
        if path.exists():
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                key = data.get("key")
                if isinstance(key, str) and key:
                    return key
            except (OSError, json.JSONDecodeError):
                pass
            return None
    if UKING_DEVICE_PATH.exists():
        try:
            data = json.loads(UKING_DEVICE_PATH.read_text(encoding="utf-8"))
            key = data.get("key")
            if isinstance(key, str) and key:
                return key
        except (OSError, json.JSONDecodeError):
            pass
    return None


def mask_key(key: str) -> str:
    if not key:
        return "****"
    prefix = key[:3] if len(key) >= 3 else key
    suffix = key[-4:] if len(key) >= 4 else key
    if len(key) <= 7:
        return "*" * max(len(key) - 1, 1) + (key[-1] if key else "")
    return f"{prefix}****{suffix}"


# --------------------------------------------------------------------------
# Attribution (User-Agent)
# --------------------------------------------------------------------------


def find_package_slug(script_path: Path) -> str:
    candidates = [
        script_path.parent / "manifest.json",
        script_path.parent.parent / "manifest.json",
        script_path.parent.parent.parent / "manifest.json",
    ]
    for candidate in candidates:
        try:
            if candidate.is_file():
                data = json.loads(candidate.read_text(encoding="utf-8"))
                name = data.get("name")
                if isinstance(name, str) and name:
                    return name
        except (OSError, json.JSONDecodeError):
            continue
    return "tokenfish-client"


def detect_platform() -> str:
    if os.environ.get("CLAUDECODE") or os.environ.get("CLAUDE_CODE"):
        return "claude-code"
    if any(k.startswith("CODEX_") for k in os.environ):
        return "codex"
    probe = " ".join(
        filter(
            None,
            [
                os.environ.get("HOME", ""),
                os.environ.get("USERPROFILE", ""),
                os.getcwd(),
                str(Path(__file__).resolve()),
            ],
        )
    )
    if "DoubaoWork" in probe:
        return "doubao-work"
    if "WorkBuddy" in probe or ".workbuddy" in probe:
        return "workbuddy"
    return "unknown"


def build_user_agent() -> str:
    slug = find_package_slug(Path(__file__).resolve())
    plat = detect_platform()
    return f"tokenfish-client/{CLIENT_VERSION} ({slug}; {plat})"


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------


class ApiHttpError(Exception):
    """The server responded with a non-2xx status."""

    def __init__(self, status: int, body: Any):
        super().__init__(f"HTTP {status}")
        self.status = status
        self.body = body


class ApiTransientError(Exception):
    """No (usable) response. `maybe_sent` says whether the request may have
    reached the server: False only for failures that happen before any bytes
    are sent (connection refused, DNS); True for everything else (timeout,
    reset, remote disconnect, broken pipe, ...). Default is the safe True."""

    def __init__(self, detail: str, maybe_sent: bool = True):
        super().__init__(detail)
        self.detail = detail
        self.maybe_sent = maybe_sent


class ApiBadResponse(Exception):
    """A 2xx response whose body is unreadable or unusable: the server
    ACCEPTED the request (a paid submit may have been charged)."""

    def __init__(self, detail: str):
        super().__init__(detail)
        self.detail = detail


API_ERRORS = (ApiHttpError, ApiTransientError, ApiBadResponse)


def api_base() -> str:
    return os.environ.get("TOKENFISH_API_BASE", DEFAULT_API_BASE).rstrip("/")


def _is_not_sent_error(exc: BaseException) -> bool:
    """Failures that can only happen before any request bytes were sent."""
    return isinstance(exc, (ConnectionRefusedError, socket.gaierror))


_NOT_SENT_ERRNOS = (errno.ENETUNREACH, errno.EHOSTUNREACH, errno.ECONNREFUSED)


def _is_not_sent_connect_error(reason: BaseException) -> bool:
    """For `URLError.reason` ONLY: urllib wraps errors of the connect/send
    phase (before any response) in URLError. Besides refused / DNS, these also
    prove nothing was sent: a sandbox blocking connect (PermissionError),
    network/host unreachable, and a TLS certificate failure (handshake).
    Timeout / reset / broken pipe stay maybe-sent: the send may have partly
    happened."""
    if _is_not_sent_error(reason) or isinstance(reason, PermissionError):
        return True
    if isinstance(reason, ssl.SSLCertVerificationError):
        return True
    return isinstance(reason, OSError) and getattr(reason, "errno", None) in _NOT_SENT_ERRNOS


def http_request(
    method: str,
    path: str,
    key: str,
    payload: Optional[dict] = None,
    timeout: int = DEFAULT_TIMEOUT,
) -> dict:
    """Raises ApiHttpError (server answered with a non-2xx status),
    ApiTransientError (no response; see `maybe_sent`) or ApiBadResponse (2xx
    but unreadable body). Never includes the key in any raised message."""
    url = api_base() + path
    data = None
    headers = {
        "Authorization": f"Bearer {key}",
        "User-Agent": build_user_agent(),
        "Accept": "application/json",
    }
    if payload is not None:
        data = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    try:
        request = urllib.request.Request(url, data=data, headers=headers, method=method)
    except ValueError as exc:
        raise ApiTransientError(f"请求地址不合法：{exc}", maybe_sent=False) from None
    try:
        resp = urllib.request.urlopen(request, timeout=timeout)
    except urllib.error.HTTPError as exc:
        try:
            raw = exc.read()
        except Exception:
            raw = b""
        try:
            body = json.loads(raw.decode("utf-8")) if raw else {}
        except ValueError:
            body = {"raw": raw.decode("utf-8", errors="replace")}
        raise ApiHttpError(exc.code, body) from None
    except urllib.error.URLError as exc:
        raise ApiTransientError(str(exc.reason), maybe_sent=not _is_not_sent_connect_error(exc.reason)) from None
    except http.client.InvalidURL as exc:
        raise ApiTransientError(f"请求地址不合法：{exc}", maybe_sent=False) from None
    except (http.client.HTTPException, OSError) as exc:
        # RemoteDisconnected, ConnectionResetError, BrokenPipeError, timeouts, ...
        detail = "请求超时" if isinstance(exc, TimeoutError) else f"{type(exc).__name__}: {exc}"
        raise ApiTransientError(detail, maybe_sent=not _is_not_sent_error(exc)) from None
    # From here on the server answered 2xx: the request was accepted.
    try:
        with resp:
            raw = resp.read()
    except Exception as exc:
        raise ApiBadResponse(f"响应读取中断：{type(exc).__name__}") from None
    if not raw:
        return {}
    try:
        parsed = json.loads(raw.decode("utf-8"))
    except ValueError as exc:  # JSONDecodeError and UnicodeDecodeError
        raise ApiBadResponse(f"响应不是合法 JSON：{exc}") from None
    if not isinstance(parsed, dict):
        raise ApiBadResponse("响应不是 JSON 对象")
    return parsed


def fetch_wallet_raw(key: str) -> dict:
    resp = http_request("GET", "/api/usage/token/", key)
    return resp.get("data", {}) or {}


def normalize_api_error(exc: Exception, key: str) -> ToolError:
    if isinstance(exc, ApiHttpError):
        status = exc.status
        body = exc.body if isinstance(exc.body, dict) else {}
        error_type = ""
        if isinstance(body.get("error"), dict):
            error_type = body["error"].get("type", "") or ""
        if status == 402 or error_type == "insufficient_quota":
            return err_insufficient_balance()
        if status == 401:
            try:
                wallet = fetch_wallet_raw(key)
                total_available = wallet.get("total_available", 0)
                if isinstance(total_available, (int, float)) and total_available <= 0:
                    return err_insufficient_balance()
            except Exception:
                pass
            return err_auth_invalid()
        if status == 429:
            return err_rate_limited()
        if 500 <= status < 600:
            return err_transient(f"HTTP {status}")
        return err_transient(f"HTTP {status}")
    if isinstance(exc, (ApiTransientError, ApiBadResponse)):
        return err_transient(exc.detail)
    return err_transient(str(exc))


# --------------------------------------------------------------------------
# Local task store (client-side idempotency, §2.4)
#
# A record is written BEFORE every paid submit (status "submitting") so that a
# crash / lost response / unwritable disk after the server charged can never be
# mistaken for "nothing happened". Statuses:
#   submitting  write-ahead record, HTTP POST in flight or process died
#   unknown     maybe-sent / unreadable 2xx: may be charged, never auto-retried
#   submitted   server accepted and returned a task (has task_id)
# Records without a status (written by 0.1.0) are treated as submitted.
# --------------------------------------------------------------------------


def _read_task_file(path: Path) -> dict:
    """Read-only; {} if missing / unreadable / corrupt. Creates nothing."""
    try:
        if not path.is_file():
            return {}
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(data, dict):
        return {}
    return {k: v for k, v in data.items() if isinstance(v, dict)}


def _load_task_store() -> dict:
    """The WRITE dir's store (read-modify-write base). Lookups use
    _load_merged_task_store instead."""
    base = state_dir()
    if base is None:
        return {}
    return _read_task_file(base / "tasks.json")


def _record_rank(record: dict) -> tuple:
    """Which of two records for the same client_request_id wins: a settled
    ("submitted", has task_id) one beats unknown/submitting, then newest."""
    settled = record.get("status") in (None, "submitted") and bool(record.get("task_id"))
    created = record.get("created_at")
    return (1 if settled else 0, created if isinstance(created, (int, float)) else 0)


def _load_merged_task_store() -> dict:
    """Union of tasks.json from EVERY candidate dir that exists (read-only,
    creates no directory). Without this a record written under one cwd / home
    would be invisible after the environment changes, and the same
    client_request_id would be submitted (and charged) again."""
    tmp_dir = _tmp_state_dir()
    merged: dict = {}
    for cand in state_dir_candidates():
        if cand == tmp_dir and not _owned_by_me(cand):
            continue
        for crid, record in _read_task_file(cand / "tasks.json").items():
            current = merged.get(crid)
            if current is None or _record_rank(record) > _record_rank(current):
                merged[crid] = record
    return merged


def _save_task_store(store: dict) -> None:
    """Atomic write into the state dir. Raises OSError on any failure."""
    base = state_dir()
    if base is None:
        raise OSError("no writable state dir")
    path = base / "tasks.json"
    base.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=".tasks-", suffix=".tmp", dir=str(base))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(store, fh, ensure_ascii=False, indent=2)
        os.replace(tmp_name, path)
    finally:
        if os.path.exists(tmp_name):
            try:
                os.remove(tmp_name)
            except OSError:
                pass


def args_hash(args: dict, exclude: tuple = ("client_request_id",)) -> str:
    filtered = {k: v for k, v in args.items() if k not in exclude}
    canonical = json.dumps(filtered, sort_keys=True, ensure_ascii=True)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def write_ahead_record(client_request_id: str, provider: str, ahash: str) -> None:
    """Persist a "submitting" record BEFORE the paid HTTP POST. If it cannot
    be persisted nothing has been submitted yet, so fail with
    `local_state_unwritable` instead of risking an untracked charge."""
    try:
        store = _load_task_store()
        store[client_request_id] = {
            "client_request_id": client_request_id,
            "args_hash": ahash,
            "status": "submitting",
            "provider": provider,
            "created_at": time.time(),
        }
        _save_task_store(store)
    except OSError:
        raise err_local_state_unwritable() from None


def discard_record(client_request_id: str) -> None:
    """Best-effort removal of a write-ahead record after a failure that
    provably did not charge (server rejected / request never sent)."""
    try:
        store = _load_task_store()
        if client_request_id in store:
            del store[client_request_id]
            _save_task_store(store)
    except OSError:
        pass


def mark_record_unknown(client_request_id: str, detail: str) -> None:
    """Best-effort: keep the record, flag it as maybe-charged. If this write
    fails the write-ahead "submitting" record is still there and is treated
    the same way."""
    try:
        store = _load_task_store()
        rec = store.get(client_request_id)
        if rec is not None:
            rec["status"] = "unknown"
            rec["detail"] = detail
            _save_task_store(store)
    except OSError:
        pass


def record_task(client_request_id: str, provider: str, task_id: str, ahash: str, task_object: dict) -> None:
    """Final record after a successful submit (status "submitted"). Raises
    OSError if the local store cannot be written."""
    store = _load_task_store()
    previous = store.get(client_request_id) or {}
    store[client_request_id] = {
        "client_request_id": client_request_id,
        "args_hash": ahash,
        "status": "submitted",
        "task_id": task_id,
        "provider": provider,
        "created_at": previous.get("created_at") or time.time(),
        "last_task_object": task_object,
    }
    _save_task_store(store)


def update_task_snapshot(client_request_id: str, task_object: dict) -> None:
    """Best-effort cache refresh: a query must not fail just because the
    local state can't be written."""
    try:
        store = _load_task_store()
        if client_request_id in store:
            store[client_request_id]["last_task_object"] = task_object
            _save_task_store(store)
    except OSError:
        pass


def find_record_by_client_request_id(client_request_id: str) -> Optional[dict]:
    store = _load_merged_task_store()
    return store.get(client_request_id)


def find_record_by_task_id(task_id: str) -> Optional[dict]:
    store = _load_merged_task_store()
    for record in store.values():
        if record.get("task_id") == task_id:
            return record
    return None


def list_local_tasks(limit: int = 20) -> list:
    store = _load_merged_task_store()
    records = list(store.values())
    records.sort(key=lambda r: r.get("created_at", 0), reverse=True)
    return records[:limit]


def make_idempotency_key(client_request_id: str) -> str:
    if IDEMPOTENCY_KEY_RE.match(client_request_id):
        return client_request_id
    return hashlib.sha256(client_request_id.encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------
# Validation
# --------------------------------------------------------------------------


def _require_str(args: dict, field: str, max_len: Optional[int] = None, required: bool = True) -> Optional[str]:
    value = args.get(field)
    if value is None:
        if required:
            raise err_invalid_arguments(field, "缺少必填参数")
        return None
    if not isinstance(value, str):
        raise err_invalid_arguments(field, "必须是字符串")
    if required and len(value) == 0:
        raise err_invalid_arguments(field, "不能为空")
    if max_len is not None and len(value) > max_len:
        raise err_invalid_arguments(field, f"长度不能超过 {max_len} 个字符")
    return value


def validate_music_args(args: dict) -> dict:
    if not isinstance(args, dict):
        raise err_invalid_arguments("args", "必须是 JSON 对象")

    prompt = _require_str(args, "prompt", max_len=1000, required=True)

    lyrics = args.get("lyrics")
    if lyrics is not None:
        if not isinstance(lyrics, str):
            raise err_invalid_arguments("lyrics", "必须是字符串")
        if len(lyrics) > 5000:
            raise err_invalid_arguments("lyrics", "长度不能超过 5000 个字符")

    title = args.get("title")
    if title is not None:
        if not isinstance(title, str):
            raise err_invalid_arguments("title", "必须是字符串")
        if len(title) > 80:
            raise err_invalid_arguments("title", "长度不能超过 80 个字符")

    instrumental = args.get("instrumental", False)
    if not isinstance(instrumental, bool):
        raise err_invalid_arguments("instrumental", "必须是布尔值")

    if instrumental and lyrics:
        raise err_invalid_arguments("lyrics", "instrumental=true 时不得提供 lyrics")

    model = args.get("model", "auto")
    if model not in MUSIC_MODELS:
        raise err_invalid_arguments("model", f"必须是以下之一：{', '.join(MUSIC_MODELS)}")

    duration = args.get("duration")
    if duration is not None:
        if isinstance(duration, bool) or not isinstance(duration, int):
            raise err_invalid_arguments("duration", "必须是整数（单位：秒）")
        if not (1 <= duration <= 300):
            raise err_invalid_arguments("duration", "取值范围必须是 1..300（单位：秒）")

    client_request_id = _require_str(args, "client_request_id", max_len=200, required=True)

    return {
        "prompt": prompt,
        "lyrics": lyrics,
        "title": title,
        "instrumental": instrumental,
        "model": model,
        "duration": duration,
        "client_request_id": client_request_id,
    }


def validate_images_args(args: dict) -> dict:
    if not isinstance(args, dict):
        raise err_invalid_arguments("args", "必须是 JSON 对象")

    prompt = _require_str(args, "prompt", max_len=2000, required=True)

    n = args.get("n", 1)
    if isinstance(n, bool) or not isinstance(n, int):
        raise err_invalid_arguments("n", "必须是整数")
    if not (1 <= n <= 4):
        raise err_invalid_arguments("n", "取值范围必须是 1..4")

    size = args.get("size", "1024x1024")
    if not isinstance(size, str) or not size:
        raise err_invalid_arguments("size", "必须是非空字符串，例如 1024x1024")

    quality = args.get("quality", "auto")
    if quality not in IMAGE_QUALITIES:
        raise err_invalid_arguments("quality", f"必须是以下之一：{', '.join(IMAGE_QUALITIES)}")

    client_request_id = _require_str(args, "client_request_id", max_len=200, required=True)

    return {
        "prompt": prompt,
        "n": n,
        "size": size,
        "quality": quality,
        "client_request_id": client_request_id,
    }


# --------------------------------------------------------------------------
# Unified task object builders + status normalization
# --------------------------------------------------------------------------


def _round_cny(quota: Any) -> Optional[float]:
    if isinstance(quota, (int, float)):
        return round(quota / QUOTA_PER_CNY, 2)
    return None


def normalize_minimax_status(raw_status: Any, has_output: bool) -> str:
    if has_output:
        return "succeeded"
    s = str(raw_status or "").lower()
    if "fail" in s or "error" in s:
        return "failed"
    if "success" in s or "complete" in s or "done" in s:
        return "succeeded"
    if "process" in s or "running" in s or "progress" in s:
        return "running"
    if "queue" in s or "pending" in s or not s:
        return "queued"
    return "running"


def build_minimax_task_object(resp: dict, client_request_id: str) -> dict:
    output_url = resp.get("output_url")
    status = normalize_minimax_status(resp.get("status"), bool(output_url))
    clips = []
    if output_url:
        clips.append(
            {
                "audio_url": output_url,
                "title": resp.get("title"),
                "duration_seconds": resp.get("duration"),
            }
        )
    return {
        "task_id": resp.get("id"),
        "provider": "minimax",
        "status": status,
        "clips": clips,
        "images": [],
        "charged_cny": _round_cny(resp.get("quota_charged")),
        "client_request_id": client_request_id,
    }


def normalize_suno_status(raw_status: Any) -> str:
    s = str(raw_status or "").upper()
    if s == "SUCCESS":
        return "succeeded"
    if s == "FAILURE":
        return "failed"
    if s == "IN_PROGRESS":
        return "running"
    return "queued"


def build_suno_submit_task_object(task_id: str, client_request_id: str) -> dict:
    return {
        "task_id": task_id,
        "provider": "suno",
        "status": "queued",
        "clips": [],
        "images": [],
        "charged_cny": None,
        "client_request_id": client_request_id,
    }


def build_suno_fetch_task_object(resp: dict, task_id: str, client_request_id: Optional[str]) -> dict:
    data = resp.get("data") or {}
    status = normalize_suno_status(data.get("status"))
    clips = []
    for item in data.get("data") or []:
        clips.append(
            {
                "audio_url": item.get("audio_url"),
                "title": item.get("title"),
                "duration_seconds": item.get("duration"),
            }
        )
    return {
        "task_id": task_id,
        "provider": "suno",
        "status": status,
        "clips": clips,
        "images": [],
        "charged_cny": None,
        "client_request_id": client_request_id,
    }


# --------------------------------------------------------------------------
# Tool implementations
# --------------------------------------------------------------------------


def tool_wallet_get(args: dict, key: str) -> dict:
    try:
        wallet = fetch_wallet_raw(key)
    except API_ERRORS as exc:
        raise normalize_api_error(exc, key) from None
    total_available = wallet.get("total_available", 0) or 0
    return {
        "balance_cny": round(total_available / QUOTA_PER_CNY, 2),
        "total_available_quota": total_available,
        "topup": "运行 `python scripts/tokenfish_client.py topup` 在浏览器充值",
    }


def tool_models_list(args: dict, key: str) -> dict:
    capability = args.get("capability") if isinstance(args, dict) else None
    if capability not in CAPABILITIES:
        raise err_invalid_arguments(
            "capability",
            f"缺少或非法，合法取值为：{', '.join(CAPABILITIES)}。说明：分别对应生成音乐与生成图片。",
        )
    if capability == "text_to_music":
        return {
            "capability": capability,
            "models": [
                {
                    "id": "auto",
                    "routing_order": ["suno", "minimax"],
                    "price": "以实时扣费为准",
                }
            ],
        }
    return {
        "capability": capability,
        "models": [
            {
                "id": "gpt-image-2",
                "price": "以实时扣费为准",
            }
        ],
    }


def _submit_suno(clean: dict, key: str) -> dict:
    payload = {
        "prompt": clean["lyrics"] if clean["lyrics"] else clean["prompt"],
        "tags": clean["prompt"],
        "title": clean.get("title") or "",
        "mv": "chirp-v5-5",
    }
    if clean.get("instrumental"):
        payload["make_instrumental"] = True
    resp = http_request("POST", "/suno/submit/music", key, payload=payload, timeout=DEFAULT_TIMEOUT)
    task_id = resp.get("data")
    if not task_id or not isinstance(task_id, str):
        # 2xx but no task: the server accepted (and may have charged) the request.
        raise ApiBadResponse("Suno 未返回 task_id")
    return build_suno_submit_task_object(task_id, clean["client_request_id"])


def _submit_minimax(clean: dict, key: str) -> dict:
    payload = {
        "model": "minimax-music-v2.6",
        "prompt": clean["prompt"],
        "idempotency_key": make_idempotency_key(clean["client_request_id"]),
    }
    if clean.get("lyrics"):
        payload["lyrics"] = clean["lyrics"]
    if clean.get("instrumental") is not None:
        payload["is_instrumental"] = clean["instrumental"]
    if clean.get("duration") is not None:
        payload["duration"] = clean["duration"]
    resp = http_request("POST", "/v1/music/generations", key, payload=payload, timeout=MINIMAX_TIMEOUT)
    if not resp.get("id"):
        raise ApiBadResponse("MiniMax 未返回 id")
    return build_minimax_task_object(resp, clean["client_request_id"])


class _SafeFailure(Exception):
    """Internal: a paid submit failed in a way that provably did not charge
    (server answered with an error status, or the request was never sent).
    `fallback_ok` says whether `auto` mode may try the next provider."""

    def __init__(self, tool_error: ToolError, fallback_ok: bool):
        super().__init__(tool_error.message)
        self.tool_error = tool_error
        self.fallback_ok = fallback_ok


def _paid_submit(client_request_id: str, ahash: str, provider: str, submit_fn, key: str):
    """Write-ahead record -> run `submit_fn` (the paid HTTP POST) -> classify.

    Returns whatever `submit_fn` returned on success. Failure classes:
      - server answered with an error status -> not charged (gateway refunds):
        record removed, _SafeFailure (normalized error; retry/fallback allowed
        unless it is a balance problem)
      - request never sent (refused / DNS) -> record removed, _SafeFailure
        (transient, retryable)
      - maybe sent (timeout / reset / disconnect) or 2xx with unusable body ->
        record kept as "unknown"; ToolError outcome_unknown (NOT retryable).
        Exception: minimax carries a server-side idempotency_key, so for it
        BOTH of these stay a retryable `transient` (record kept as
        provider=minimax, status=unknown); the retry replays the same key and
        goes straight to minimax.
    Raises ToolError local_state_unwritable if the write-ahead record can't be
    persisted (nothing was submitted)."""
    write_ahead_record(client_request_id, provider, ahash)
    try:
        return submit_fn()
    except ApiHttpError as exc:
        discard_record(client_request_id)
        normalized = normalize_api_error(exc, key)
        raise _SafeFailure(normalized, fallback_ok=normalized.code != "insufficient_balance") from None
    except ApiTransientError as exc:
        if not exc.maybe_sent:
            discard_record(client_request_id)
            raise _SafeFailure(err_transient(exc.detail), fallback_ok=True) from None
        mark_record_unknown(client_request_id, exc.detail)
        if provider == "minimax":
            raise err_transient(exc.detail) from None
        raise err_outcome_unknown(exc.detail) from None
    except ApiBadResponse as exc:
        mark_record_unknown(client_request_id, exc.detail)
        if provider == "minimax":
            raise err_transient(exc.detail) from None
        raise err_outcome_unknown(exc.detail) from None


def _record_is_submitted(record: dict) -> bool:
    """True for a record the server answered for (has a task). Records written
    by 0.1.0 have no `status` and are always submitted."""
    return record.get("status") in (None, "submitted") and bool(record.get("task_id"))


WARNING_MUSIC_RECORD_NOT_SAVED = (
    "任务已提交，可能已扣费，但本地记录写入失败（状态目录不可写）。"
    "请勿重试，也不要换新的 client_request_id 再次提交；"
    "请用本次返回结果里的 `task_id` 和 `provider` 调用 `tokenfish.tasks.get`（两个参数都传）取回结果，"
    "并以钱包流水核对。设置环境变量 TOKENFISH_HOME 为可写目录可避免此问题。"
)
WARNING_IMAGES_RECORD_NOT_SAVED = (
    "图片已提交生成，可能已扣费，但本地记录写入失败（状态目录不可写）。"
    "请勿重试，也不要换新的 client_request_id 再次提交；本次结果已随本次返回给出，请直接交付并以钱包流水核对。"
    "设置环境变量 TOKENFISH_HOME 为可写目录可避免此问题。"
)


def _finish_submit(client_request_id: str, provider: str, ahash: str, task_object: dict) -> dict:
    """Update the write-ahead record to "submitted". The server has already
    accepted (and charged) the request, so a failure here is reported as a
    `warning`, never raised."""
    try:
        record_task(client_request_id, provider, task_object["task_id"], ahash, task_object)
    except Exception:
        result = dict(task_object)
        result["warning"] = WARNING_MUSIC_RECORD_NOT_SAVED
        return result
    return task_object


def tool_music_generate(args: dict, key: str) -> dict:
    clean = validate_music_args(args)
    crid = clean["client_request_id"]
    ahash = args_hash(clean)

    forced_provider = None
    existing = find_record_by_client_request_id(crid)
    if existing is not None:
        if existing.get("args_hash") != ahash:
            raise err_idempotency_conflict(crid)
        if _record_is_submitted(existing):
            return _refresh_task(existing, key)
        # A previous attempt has no known outcome (record "submitting"/"unknown").
        if existing.get("provider") != "minimax":
            raise err_outcome_unknown("此前用同一个 client_request_id 发起的提交结果未确认，本次未重新提交")
        # minimax: the same idempotency_key makes a resubmit safe; and it must go
        # straight to minimax (never Suno first) or both providers could charge.
        forced_provider = "minimax"

    model = clean["model"]
    if forced_provider:
        providers = [forced_provider]
    elif model == "auto":  # suno first; fall back to minimax only when provably not charged
        providers = ["suno", "minimax"]
    else:
        providers = [model]

    task_object = None
    provider = providers[0]
    for idx, provider in enumerate(providers):
        if provider == "suno":
            submit_fn = lambda: _submit_suno(clean, key)  # noqa: E731
        else:
            submit_fn = lambda: _submit_minimax(clean, key)  # noqa: E731
        try:
            task_object = _paid_submit(crid, ahash, provider, submit_fn, key)
        except _SafeFailure as failure:
            if idx + 1 < len(providers) and failure.fallback_ok:
                continue
            raise failure.tool_error from None
        break

    return _finish_submit(crid, provider, ahash, task_object)


def _save_images(resp: dict, client_request_id: str) -> tuple:
    """Saves b64 images; never raises (the paid call already succeeded).
    Returns (images, warnings)."""
    images = []
    warnings = []
    data = resp.get("data")
    items = [i for i in data if isinstance(i, dict)] if isinstance(data, list) else []
    if not items:
        warnings.append("服务端返回成功但没有图片数据，可能已扣费。请勿重试，请以钱包流水核对。")
    safe_id = re.sub(r"[^A-Za-z0-9._-]", "_", client_request_id)
    out_dir = None
    out_dir_resolved = False
    dropped = 0
    for idx, item in enumerate(items):
        entry = {"url": item.get("url"), "path": None}
        b64 = item.get("b64_json")
        if b64:
            try:
                if not out_dir_resolved:
                    out_dir = output_dir()
                    out_dir_resolved = True
                if out_dir is None:
                    raise OSError("no writable output dir")
                out_path = out_dir / f"{safe_id}-{idx}.png"
                out_path.write_bytes(base64.b64decode(b64))
                entry["path"] = str(out_path)
            except Exception:
                dropped += 1
        images.append(entry)
    if dropped:
        warnings.append(
            f"图片已提交生成，可能已扣费，但有 {dropped} 张图片无法保存到本地"
            "（输出目录不可写或数据无法解码），该部分图片数据已丢弃。"
            "请勿重试（重试会再次扣费）；有 url 的请用 url，否则请告知用户并以钱包流水核对。"
            "把环境变量 TOKENFISH_OUTPUT_DIR 设为可写目录可避免此问题。"
        )
    return images, warnings


def tool_images_generate(args: dict, key: str) -> dict:
    clean = validate_images_args(args)
    crid = clean["client_request_id"]
    ahash = args_hash(clean)

    existing = find_record_by_client_request_id(crid)
    if existing is not None:
        if existing.get("args_hash") != ahash:
            raise err_idempotency_conflict(crid)
        if _record_is_submitted(existing):
            return existing.get("last_task_object", {})
        raise err_outcome_unknown("此前用同一个 client_request_id 发起的提交结果未确认，本次未重新提交")

    payload = {
        "model": "gpt-image-2",
        "prompt": clean["prompt"],
        "n": clean["n"],
        "size": clean["size"],
        "quality": clean["quality"],
    }
    try:
        resp = _paid_submit(
            crid,
            ahash,
            "image",
            lambda: http_request("POST", "/v1/images/generations", key, payload=payload, timeout=DEFAULT_TIMEOUT),
            key,
        )
    except _SafeFailure as failure:
        raise failure.tool_error from None

    # The server accepted (and charged) the request: from here on nothing may raise.
    images, warnings = _save_images(resp, crid)
    task_id = f"image-{crid}-{hashlib.sha256(ahash.encode()).hexdigest()[:8]}"
    task_object = {
        "task_id": task_id,
        "provider": "image",
        "status": "succeeded",
        "clips": [],
        "images": images,
        "charged_cny": _round_cny(resp.get("quota_charged")),
        "client_request_id": crid,
    }
    if warnings:
        task_object["warning"] = " ".join(warnings)
    try:
        record_task(crid, "image", task_id, ahash, task_object)
    except Exception:
        task_object = dict(task_object)
        task_object["warning"] = " ".join(warnings + [WARNING_IMAGES_RECORD_NOT_SAVED])
    return task_object


def _refresh_task(record: dict, key: str) -> dict:
    provider = record.get("provider")
    task_id = record.get("task_id")
    crid = record.get("client_request_id")
    if provider == "image":
        return record.get("last_task_object", {})
    if provider == "suno":
        try:
            resp = http_request("GET", f"/suno/fetch/{task_id}", key, timeout=DEFAULT_TIMEOUT)
        except API_ERRORS as exc:
            raise normalize_api_error(exc, key) from None
        task_object = build_suno_fetch_task_object(resp, task_id, crid)
    elif provider == "minimax":
        try:
            resp = http_request("GET", f"/v1/music/generations/{task_id}", key, timeout=DEFAULT_TIMEOUT)
        except API_ERRORS as exc:
            raise normalize_api_error(exc, key) from None
        task_object = build_minimax_task_object(resp, crid)
    else:
        return record.get("last_task_object", {})
    update_task_snapshot(crid, task_object)
    return task_object


def tool_tasks_get(args: dict, key: str) -> dict:
    if not isinstance(args, dict):
        raise err_invalid_arguments("args", "必须是 JSON 对象")
    task_id = args.get("task_id")
    if not isinstance(task_id, str) or not task_id:
        raise err_invalid_arguments("task_id", "缺少必填参数")
    provider = args.get("provider")
    if provider is not None and provider not in TASK_PROVIDERS:
        raise err_invalid_arguments("provider", f"必须是以下之一：{', '.join(TASK_PROVIDERS)}")
    record = find_record_by_task_id(task_id)
    if record is None:
        if provider is None:
            raise err_unknown_task(task_id)
        # No local record (e.g. the post-submit record write failed): query the
        # provider's status endpoint directly with the task_id the caller holds.
        record = {"provider": provider, "task_id": task_id, "client_request_id": None}
    return _refresh_task(record, key)


def tool_tasks_list(args: dict, key: str) -> dict:
    records = list_local_tasks(limit=20)
    return {
        "tasks": [
            {
                "client_request_id": r.get("client_request_id"),
                "provider": r.get("provider"),
                "task_id": r.get("task_id"),
                "record_status": r.get("status") or "submitted",
                "created_at": r.get("created_at"),
                "task": r.get("last_task_object"),
            }
            for r in records
        ]
    }


TOOLS = [
    {
        "name": "tokenfish.wallet.get",
        "description": "查询 TokenFish 钱包余额（不扣费）",
        "billed": False,
        "parameters": {"type": "object", "properties": {}, "required": []},
        "handler": tool_wallet_get,
    },
    {
        "name": "tokenfish.models.list",
        "description": "列出某个能力下可用的模型（不扣费）",
        "billed": False,
        "parameters": {
            "type": "object",
            "properties": {
                "capability": {"type": "string", "enum": list(CAPABILITIES)},
            },
            "required": ["capability"],
        },
        "handler": tool_models_list,
    },
    {
        "name": "tokenfish.music.generate",
        "description": "生成音乐（计费）：给风格描述，可选歌词/标题/是否纯伴奏",
        "billed": True,
        "parameters": {
            "type": "object",
            "properties": {
                "prompt": {"type": "string", "maxLength": 1000, "description": "风格描述"},
                "lyrics": {"type": "string", "maxLength": 5000},
                "title": {"type": "string", "maxLength": 80},
                "instrumental": {"type": "boolean", "default": False},
                "model": {"type": "string", "enum": list(MUSIC_MODELS), "default": "auto"},
                "duration": {"type": "integer", "minimum": 1, "maximum": 300},
                "client_request_id": {"type": "string", "maxLength": 200},
            },
            "required": ["prompt", "client_request_id"],
        },
        "handler": tool_music_generate,
    },
    {
        "name": "tokenfish.images.generate",
        "description": "生成图片（计费）",
        "billed": True,
        "parameters": {
            "type": "object",
            "properties": {
                "prompt": {"type": "string", "maxLength": 2000},
                "n": {"type": "integer", "minimum": 1, "maximum": 4, "default": 1},
                "size": {"type": "string", "default": "1024x1024"},
                "quality": {"type": "string", "enum": list(IMAGE_QUALITIES), "default": "auto"},
                "client_request_id": {"type": "string", "maxLength": 200},
            },
            "required": ["prompt", "client_request_id"],
        },
        "handler": tool_images_generate,
    },
    {
        "name": "tokenfish.tasks.get",
        "description": "查询任务当前状态（不扣费）",
        "billed": False,
        "parameters": {
            "type": "object",
            "properties": {
                "task_id": {"type": "string"},
                "provider": {
                    "type": "string",
                    "enum": list(TASK_PROVIDERS),
                    "description": "可选：本地没有该任务记录时，传返回结果里的 provider 直接查询",
                },
            },
            "required": ["task_id"],
        },
        "handler": tool_tasks_get,
    },
    {
        "name": "tokenfish.tasks.list",
        "description": "列出本地最近的任务记录（不扣费，最多 20 条）",
        "billed": False,
        "parameters": {"type": "object", "properties": {}, "required": []},
        "handler": tool_tasks_list,
    },
]

TOOLS_BY_NAME = {t["name"]: t for t in TOOLS}


# --------------------------------------------------------------------------
# CLI commands
# --------------------------------------------------------------------------


def cmd_verify() -> tuple[dict, int]:
    key = load_credentials()
    if not key:
        e = err_not_logged_in()
        return e.to_payload(), e.exit_code
    try:
        wallet = fetch_wallet_raw(key)
    except API_ERRORS as exc:
        e = normalize_api_error(exc, key)
        return e.to_payload(), e.exit_code
    total_available = wallet.get("total_available", 0) or 0
    return (
        {
            "ok": True,
            "balance_cny": round(total_available / QUOTA_PER_CNY, 2),
            "total_available_quota": total_available,
        },
        0,
    )


def cmd_tools() -> tuple[Any, int]:
    listing = [
        {
            "name": t["name"],
            "description": t["description"],
            "billed": t["billed"],
            "parameters": t["parameters"],
        }
        for t in TOOLS
    ]
    return listing, 0


def cmd_call(tool_name: str, raw_stdin: str) -> tuple[dict, int]:
    tool = TOOLS_BY_NAME.get(tool_name)
    if tool is None:
        e = err_unknown_tool(tool_name)
        return e.to_payload(), e.exit_code

    try:
        args = json.loads(raw_stdin) if raw_stdin.strip() else {}
    except json.JSONDecodeError as exc:
        e = err_invalid_arguments("stdin", f"不是合法 JSON：{exc}")
        return e.to_payload(), e.exit_code

    key = load_credentials()
    if not key:
        e = err_not_logged_in()
        return e.to_payload(), e.exit_code

    try:
        result = tool["handler"](args, key)
        return result, 0
    except ToolError as e:
        return e.to_payload(), e.exit_code
    except Exception as exc:  # pragma: no cover - safety net, never leak key
        detail = f"未预期的内部错误：{type(exc).__name__}"
        if tool.get("billed"):
            # A paid tool died somewhere unexpected: it may already have been
            # charged, so this must NEVER look retryable.
            e = err_outcome_unknown(detail)
        else:
            e = err_transient(detail)
        return e.to_payload(), e.exit_code


def cmd_login(key_file: str) -> tuple[dict, int]:
    path = Path(key_file)
    if not path.is_file():
        e = err_invalid_arguments("key_file", f"文件不存在：{path}")
        return e.to_payload(), e.exit_code
    try:
        content = path.read_text(encoding="utf-8").strip()
    except OSError as exc:
        e = err_invalid_arguments("key_file", f"读取失败：{exc}")
        return e.to_payload(), e.exit_code
    if not content:
        e = err_invalid_arguments("key_file", "文件内容为空")
        return e.to_payload(), e.exit_code
    try:
        saved_to = save_credentials(content)
    except OSError:
        e = err_local_state_unwritable(for_login=True)
        return e.to_payload(), e.exit_code
    return {
        "ok": True,
        "message": "凭据已保存",
        "key_masked": mask_key(content),
        "saved_to": str(saved_to),
    }, 0


def cmd_whoami() -> tuple[dict, int]:
    key = load_credentials()
    if not key:
        e = err_not_logged_in()
        return e.to_payload(), e.exit_code
    try:
        wallet = fetch_wallet_raw(key)
    except API_ERRORS as exc:
        e = normalize_api_error(exc, key)
        return e.to_payload(), e.exit_code
    total_available = wallet.get("total_available", 0) or 0
    return (
        {
            "key_masked": mask_key(key),
            "balance_cny": round(total_available / QUOTA_PER_CNY, 2),
            "total_available_quota": total_available,
            "state_dir": str(state_dir()) if state_dir() is not None else None,
        },
        0,
    )


def _copy_to_clipboard(text: str) -> bool:
    system = platform_mod.system()
    try:
        if system == "Windows":
            subprocess.run(["clip"], input=text.encode("utf-16-le"), check=True)
            return True
        if system == "Darwin":
            subprocess.run(["pbcopy"], input=text.encode("utf-8"), check=True)
            return True
        for tool_args in (["xclip", "-selection", "clipboard"], ["wl-copy"]):
            if shutil.which(tool_args[0]):
                subprocess.run(tool_args, input=text.encode("utf-8"), check=True)
                return True
        return False
    except (OSError, subprocess.CalledProcessError):
        return False


def cmd_topup() -> tuple[dict, int]:
    key = load_credentials()
    if not key:
        e = err_not_logged_in()
        return e.to_payload(), e.exit_code
    import webbrowser

    webbrowser.open(TOPUP_URL)
    copied = _copy_to_clipboard(key)
    if not copied:
        print("剪贴板复制失败，请运行 `whoami` 查看打码后的 key。", file=sys.stderr)
    return {"opened_url": TOPUP_URL, "clipboard": copied}, 0


# --------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="tokenfish_client.py", description="TokenFish agent-native client")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("verify", help="读余额验证凭据，不扣费")
    sub.add_parser("tools", help="打印工具清单")

    call_parser = sub.add_parser("call", help="调用一个工具，参数 JSON 从 stdin 读取")
    call_parser.add_argument("tool", help="工具名，如 tokenfish.wallet.get")

    login_parser = sub.add_parser("login", help="从文件读取 key 并写入凭据文件")
    login_parser.add_argument("--key-file", required=True, help="包含 key 的文件路径")

    sub.add_parser("whoami", help="显示打码后的 key 和余额")
    sub.add_parser("topup", help="打开充值页并把 key 复制到剪贴板")

    return parser


def main(argv: Optional[list] = None) -> int:
    parser = build_arg_parser()
    ns = parser.parse_args(argv)

    if ns.command == "verify":
        payload, code = cmd_verify()
    elif ns.command == "tools":
        payload, code = cmd_tools()
    elif ns.command == "call":
        raw_stdin = sys.stdin.read() if not sys.stdin.isatty() else "{}"
        payload, code = cmd_call(ns.tool, raw_stdin)
    elif ns.command == "login":
        payload, code = cmd_login(ns.key_file)
    elif ns.command == "whoami":
        payload, code = cmd_whoami()
    elif ns.command == "topup":
        payload, code = cmd_topup()
    else:  # pragma: no cover
        parser.print_usage(sys.stderr)
        return 2

    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return code


if __name__ == "__main__":
    sys.exit(main())
