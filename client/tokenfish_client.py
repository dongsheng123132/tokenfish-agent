#!/usr/bin/env python3
"""TokenFish agent-native client.

Single-file, Python 3.10+ standard-library-only client that lets an AI agent
call TokenFish (backed by 虾盘云 / u-claw) capabilities: wallet balance,
model listing, music generation (Suno / MiniMax), image generation,
text-to-speech, text-to-video (async submit + bounded poll), and task polling.

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

v0.2: `setup` opens a new, empty wallet when no credentials exist anywhere
(POST /device/bind, no registration); the tools `tokenfish.wallet.topup` /
`tokenfish.wallet.topup_status` create / check a recharge order (the key only
ever travels in the POST body of /recharge/create, never in a URL or in any
output); and every successful paid result is decorated by the client itself
with `play_url`s (a static page on SITE_ORIGIN, data in the URL fragment),
`links`, `balance_after_cny` and `topup_hint`. The decoration happens after the
result is final and swallows every failure, so it can never change a charge
decision.

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
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Optional

CLIENT_VERSION = "0.3.0"

DEFAULT_API_BASE = "https://api.u-claw.org.cn"
TOPUP_URL = "https://api.u-claw.org.cn/recharge"

# Origin of the static site (play.html / pay.html). Data for those pages rides
# in the URL fragment, which browsers never send to the server. Keep in sync with
# the SPEC.md "站点域名决定" appendix.
SITE_ORIGIN = "https://tokenfish.u-claw.org.cn"
OFFICIAL_SITE_URL = "https://www.u-claw.org.cn"

QUOTA_PER_CNY = 500000

DEFAULT_TIMEOUT = 30
MINIMAX_TIMEOUT = 240
WALLET_FETCH_TIMEOUT = 8  # best-effort balance lookups after a result is final

HOME_STATE_DIR = Path.home() / ".tokenfish"
CREDENTIALS_PATH = HOME_STATE_DIR / "credentials.json"
UKING_DEVICE_PATH = Path.home() / ".uking" / "device.json"

# Default image output dir (relative to cwd); see output_dir() for fallbacks.
OUTPUT_DIR = Path("tokenfish-outputs")

IDEMPOTENCY_KEY_RE = re.compile(r"^[A-Za-z0-9\-_.]{12,128}$")

MUSIC_MODELS = ("auto", "suno", "minimax")
TASK_PROVIDERS = ("suno", "minimax", "video")
IMAGE_QUALITIES = ("low", "medium", "high", "auto")
CAPABILITIES = ("text_to_music", "text_to_image", "text_to_speech", "text_to_video")
TTS_MODEL = "minimax-speech-2.8-turbo"
TTS_VOICES = ("female-tianmei", "male-qn-qingse")
TTS_DEFAULT_VOICE = "female-tianmei"
TTS_MAX_BILLED_CHARS = 5000

# Text-to-video (POST /v1/video/generations + GET /v1/video/generations/{id}).
# Models come from the live /v1/models list; the first one is the cheapest and
# therefore the default. Prices are only a hint for the craft card - the wallet
# ledger is authoritative (see estimate_video_cost_cny).
VIDEO_MODELS = (
    "doubao-seedance-2-0-mini-260615",
    "doubao-seedance-2-0-fast-260128",
    "doubao-seedance-2-0-260128",
    "wanx2.1-t2v-turbo",
    "hy-video-1.5",
)
VIDEO_DEFAULT_MODEL = "doubao-seedance-2-0-mini-260615"
VIDEO_RESOLUTIONS = ("480p", "720p", "1080p")
VIDEO_DURATION_MIN = 5
VIDEO_DURATION_MAX = 15
VIDEO_POLL_INTERVAL = 5  # seconds between status polls (tests stub this)
VIDEO_POLL_TIMEOUT = 180  # max seconds one call blocks before returning "running"
VIDEO_DOWNLOAD_TIMEOUT = 120
VIDEO_MIN_BYTES = 1024  # anything smaller is not a real MP4
VIDEO_BASE_PRICE_CNY = {  # reference price for 5s / 480p, CNY
    "doubao-seedance-2-0-mini-260615": 2.9,
    "doubao-seedance-2-0-fast-260128": 4.9,
    "doubao-seedance-2-0-260128": 6.9,
}
VIDEO_RESOLUTION_MULTIPLIER = {"480p": 1.0, "720p": 1.5, "1080p": 2.5}
VIDEO_COST_NOTE = "估算，实扣以钱包流水为准"

TOPUP_CHANNELS = ("alipay", "wechat")
TOPUP_AMOUNT_MIN = 1
TOPUP_AMOUNT_MAX = 500


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
        "未找到凭据。可以免注册开一个新的 TokenFish 钱包（`setup`），或用 `login --key-file <path>` 导入已有的 key。",
        False,
        "先告诉用户：可以免注册开一个新钱包（新钱包余额为 0，需要先充值），征得同意后运行 "
        "`python tokenfish_client.py setup`；如果用户已有 key，改为让用户把 key 存到文件后运行 "
        "`python tokenfish_client.py login --key-file <path>` 导入",
        3,
    )


def err_insufficient_balance() -> ToolError:
    return ToolError(
        "insufficient_balance",
        "余额不足，本次未扣费。请告诉用户余额不足，问清要充多少元（1 到 500 的整数）、用支付宝还是微信；"
        "然后调用 tokenfish.wallet.topup 拿付款链接给用户，用户付款后调用 tokenfish.wallet.topup_status 确认到账，"
        "再用同一个 client_request_id 原样重试。",
        False,
        "问用户充多少元（1-500 的整数）和支付宝/微信 → 调用 tokenfish.wallet.topup → 把 pay_link 原样给用户 → "
        "用户付款后调用 tokenfish.wallet.topup_status → 到账后用同一个 client_request_id 重试",
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


def err_transient_free(detail: str = "") -> ToolError:
    """Transient failure of a call that never charges (setup / top-up tools):
    retrying is always safe, and there is no client_request_id to reuse."""
    msg = "服务暂时不可用" + (f"：{detail}" if detail else "") + "。本次没有产生任何扣费，可以稍后重试。"
    return ToolError("transient", msg, True, "稍后重新调用本工具", 4)


def err_rate_limited_free() -> ToolError:
    return ToolError("rate_limited", "请求过于频繁，请稍后重试。", True, "稍等片刻后重新调用本工具", 4)


def err_setup_rate_limited() -> ToolError:
    return ToolError(
        "rate_limited",
        "开钱包的请求过于频繁（同一网络出口的开通次数有上限），本次没有创建任何钱包。请稍后再试。",
        True,
        "稍等几分钟后重新运行 `python tokenfish_client.py setup`",
        4,
    )


def err_setup_transient(detail: str = "") -> ToolError:
    msg = (
        "开钱包没有成功" + (f"（{detail}）" if detail else "") + "。重试是安全的："
        "即使服务端已经建好了一个新钱包，它也只是余额为 0 的空钱包，不涉及任何资金。"
    )
    return ToolError("transient", msg, True, "重新运行 `python tokenfish_client.py setup`", 4)


def err_setup_unwritable(wallet_created: bool) -> ToolError:
    if wallet_created:
        msg = (
            "钱包可能已经创建，但它的 key 没能保存到本机（状态目录不可写），这个钱包已无法找回。"
            "它是余额为 0 的空钱包，没有资金损失。"
            "请把环境变量 TOKENFISH_HOME 设为一个可写目录后重新运行 `setup`。"
        )
    else:
        msg = (
            "本地状态目录不可写，没有创建钱包（未向服务端发请求）。"
            "请把环境变量 TOKENFISH_HOME 设为一个可写目录后重新运行 `setup`。"
        )
    return ToolError(
        "local_state_unwritable",
        msg,
        False,
        "设置环境变量 TOKENFISH_HOME 为一个可写目录后，重新运行 `python tokenfish_client.py setup`",
        3,
    )


def err_channel_unavailable(channel: str) -> ToolError:
    names = {"alipay": "支付宝", "wechat": "微信"}
    other = "wechat" if channel == "alipay" else "alipay"
    return ToolError(
        "channel_unavailable",
        f"{names.get(channel, channel)}充值通道暂时不可用，没有创建订单。可以改用{names[other]}（channel={other}）。",
        False,
        f"换成 channel={other} 重新调用 tokenfish.wallet.topup；"
        f"如果两个通道都不可用，请用户到虾盘云官网 {OFFICIAL_SITE_URL} 充值",
        3,
    )


def err_order_not_found(order_id: str) -> ToolError:
    return ToolError(
        "order_not_found",
        f"没有找到订单 `{order_id}`。",
        False,
        "确认 order_id 来自 tokenfish.wallet.topup 的返回值（原样使用，不要改动）",
        2,
    )


def err_unknown_task(task_id: str) -> ToolError:
    return ToolError(
        "invalid_arguments",
        f"未找到本地记录的任务 `{task_id}`。",
        False,
        "确认 task_id 来自本客户端此前的返回值；如果 task_id 确实有效但本地没有记录，"
        "请同时传 `provider`（取返回结果里的 provider 值）直接查询",
        2,
    )


def err_video_download_failed(task_id: Any, detail: str) -> ToolError:
    """The video task itself succeeded (and may already be charged) but the
    MP4 could not be downloaded / validated. Never resubmit: the caller can
    call `tokenfish.tasks.get` with the same task_id to download it again."""
    return ToolError(
        "download_failed",
        f"视频任务 `{task_id}` 已生成成功（可能已扣费），但下载校验未通过：{detail}。"
        "本地残留文件已删除，没有重新提交任务。",
        False,
        "不要重新提交（会再次扣费）。稍后用 `tokenfish.tasks.get` 传这次的 `task_id`"
        "（和 `provider`: \"video\"）重新下载；仍失败就用返回里的 `video_url` 在浏览器下载，"
        "并以钱包流水核对。",
        3,
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


def save_credentials(key: str, wallet_id: Optional[str] = None) -> Path:
    """Saves the key into the resolved writable state dir (normally
    ~/.tokenfish), plus the wallet id when `setup` created the wallet
    ({"key": ..., "wallet_id": ...}; readers only need "key", so files without
    wallet_id stay valid). Returns the file path; raises OSError if no state dir
    is writable."""
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
            record = {"key": key}
            if wallet_id:
                record["wallet_id"] = wallet_id
            json.dump(record, fh)
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
    key: Optional[str],
    payload: Optional[dict] = None,
    timeout: int = DEFAULT_TIMEOUT,
    as_bytes: bool = False,
    accept: Optional[str] = None,
) -> Any:
    """Raises ApiHttpError (server answered with a non-2xx status),
    ApiTransientError (no response; see `maybe_sent`) or ApiBadResponse (2xx
    but unreadable body). Never includes the key in any raised message.
    `key=None` sends no Authorization header (public endpoints: /device/bind,
    /recharge/*). With `as_bytes=True` the raw 2xx body is returned as bytes
    (for binary endpoints like TTS) instead of being parsed as JSON."""
    url = api_base() + path
    data = None
    headers = {
        "User-Agent": build_user_agent(),
        "Accept": accept if accept else "application/json",
    }
    if key:
        headers["Authorization"] = f"Bearer {key}"
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
    if as_bytes:
        return raw
    if not raw:
        return {}
    try:
        parsed = json.loads(raw.decode("utf-8"))
    except ValueError as exc:  # JSONDecodeError and UnicodeDecodeError
        raise ApiBadResponse(f"响应不是合法 JSON：{exc}") from None
    if not isinstance(parsed, dict):
        raise ApiBadResponse("响应不是 JSON 对象")
    return parsed


def fetch_wallet_raw(key: str, timeout: int = DEFAULT_TIMEOUT) -> dict:
    resp = http_request("GET", "/api/usage/token/", key, timeout=timeout)
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


_CJK_RE = re.compile(r"[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]")


def _tts_billed_chars(text: str) -> int:
    """TTS 计费字符数：汉字（CJK 统一表意文字及扩展/兼容表意文字）按 2，其余按 1。"""
    total = 0
    for ch in text:
        total += 2 if _CJK_RE.search(ch) else 1
    return total


def validate_tts_args(args: dict) -> dict:
    if not isinstance(args, dict):
        raise err_invalid_arguments("args", "必须是 JSON 对象")

    text = _require_str(args, "input", required=True)

    voice = args.get("voice", TTS_DEFAULT_VOICE)
    if not isinstance(voice, str) or voice not in TTS_VOICES:
        raise err_invalid_arguments("voice", f"必须是以下之一：{', '.join(TTS_VOICES)}（默认 {TTS_DEFAULT_VOICE}）")

    client_request_id = _require_str(args, "client_request_id", max_len=200, required=True)

    billed = _tts_billed_chars(text)
    if billed > TTS_MAX_BILLED_CHARS:
        raise err_invalid_arguments(
            "input",
            f"计费字符数超过 {TTS_MAX_BILLED_CHARS}（汉字按 2、其他按 1 计，当前约 {billed}），请缩短文本后再试",
        )

    return {"input": text, "voice": voice, "client_request_id": client_request_id}


def validate_video_args(args: dict) -> dict:
    if not isinstance(args, dict):
        raise err_invalid_arguments("args", "必须是 JSON 对象")

    prompt = _require_str(args, "prompt", max_len=2000, required=True)

    duration = args.get("duration", VIDEO_DURATION_MIN)
    if isinstance(duration, bool) or not isinstance(duration, int):
        raise err_invalid_arguments("duration", "必须是整数（单位：秒）")
    if not (VIDEO_DURATION_MIN <= duration <= VIDEO_DURATION_MAX):
        raise err_invalid_arguments(
            "duration", f"取值范围必须是 {VIDEO_DURATION_MIN}..{VIDEO_DURATION_MAX}（单位：秒，默认 {VIDEO_DURATION_MIN}）"
        )

    resolution = args.get("resolution", "480p")
    if not isinstance(resolution, str) or resolution not in VIDEO_RESOLUTIONS:
        raise err_invalid_arguments("resolution", f"必须是以下之一：{', '.join(VIDEO_RESOLUTIONS)}（默认 480p）")

    model = args.get("model", VIDEO_DEFAULT_MODEL)
    if not isinstance(model, str) or model not in VIDEO_MODELS:
        raise err_invalid_arguments(
            "model", f"必须是以下之一：{', '.join(VIDEO_MODELS)}（默认 {VIDEO_DEFAULT_MODEL}）"
        )

    client_request_id = _require_str(args, "client_request_id", max_len=200, required=True)

    return {
        "prompt": prompt,
        "duration": duration,
        "resolution": resolution,
        "model": model,
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


def normalize_video_status(raw_status: Any) -> str:
    """SUCCESS/SUCCEED/COMPLETE -> succeeded; FAIL/ERROR/CANCEL -> failed;
    everything else (IN_PROGRESS / QUEUED / unknown) -> running."""
    s = str(raw_status or "").upper()
    if any(token in s for token in ("SUCCESS", "SUCCEED", "COMPLETE")):
        return "succeeded"
    if any(token in s for token in ("FAIL", "ERROR", "CANCEL")):
        return "failed"
    return "running"


def estimate_video_cost_cny(model: str, duration: Any, resolution: str) -> Optional[float]:
    """Rough reference price for the craft card (5s/480p base, scaled linearly
    by duration, then by resolution). Models without a published reference
    price return None; the real charge always comes from the wallet ledger."""
    base = VIDEO_BASE_PRICE_CNY.get(model)
    if base is None:
        return None
    try:
        seconds = float(duration)
    except (TypeError, ValueError):
        return None
    multiplier = VIDEO_RESOLUTION_MULTIPLIER.get(resolution, 1.0)
    return round(base * (seconds / 5.0) * multiplier, 2)


def build_video_task_object(
    data: dict,
    task_id: str,
    client_request_id: Optional[str],
    estimated_cost_cny: Optional[float] = None,
) -> dict:
    """Unified video task object. `data` is the (unwrapped) status payload;
    a submit response with no status yet passes an empty dict."""
    data = data if isinstance(data, dict) else {}
    status = normalize_video_status(data.get("status")) if data else "running"
    result_url = data.get("result_url")
    return {
        "ok": True,
        "task_id": task_id,
        "provider": "video",
        "status": status,
        "file": None,
        "video_url": result_url if isinstance(result_url, str) and result_url else None,
        "clips": [],
        "images": [],
        "charged_cny": _round_cny(data.get("quota_charged")),
        "links": None,
        "balance_after_cny": None,
        "topup_hint": None,
        "estimated_cost_cny": estimated_cost_cny,
        "cost_note": VIDEO_COST_NOTE,
        "progress": data.get("progress") if data else None,
        "fail_reason": data.get("fail_reason") if data else None,
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
        "topup": (
            "余额不足或想充值时：问用户充多少元（1 到 500 的整数）和支付宝/微信，"
            "调用 tokenfish.wallet.topup 拿付款链接，用户付款后用 tokenfish.wallet.topup_status 查到账"
        ),
    }


def tool_models_list(args: dict, key: str) -> dict:
    capability = args.get("capability") if isinstance(args, dict) else None
    if capability not in CAPABILITIES:
        raise err_invalid_arguments(
            "capability",
            f"缺少或非法，合法取值为：{', '.join(CAPABILITIES)}。"
            "说明：分别对应生成音乐、生成图片、文字转语音与文生视频。",
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
    if capability == "text_to_image":
        return {
            "capability": capability,
            "models": [
                {
                    "id": "gpt-image-2",
                    "price": "以实时扣费为准",
                }
            ],
        }
    if capability == "text_to_video":
        models = []
        for model_id in VIDEO_MODELS:
            entry = {"id": model_id, "price": "以实时扣费为准"}
            reference = VIDEO_BASE_PRICE_CNY.get(model_id)
            if reference is not None:
                entry["reference_price_5s_480p_cny"] = reference
            models.append(entry)
        return {
            "capability": capability,
            "models": models,
            "default": VIDEO_DEFAULT_MODEL,
            "duration_seconds": f"{VIDEO_DURATION_MIN}..{VIDEO_DURATION_MAX}",
            "resolutions": list(VIDEO_RESOLUTIONS),
            "note": "价格以实时扣费为准；reference_price_5s_480p_cny 只是 5 秒 / 480p 的参考价，"
            "实际按秒计费，时长与清晰度越高越贵，实扣以钱包流水为准。",
        }
    return {
        "capability": capability,
        "models": [
            {
                "id": TTS_MODEL,
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
WARNING_TTS_RECORD_NOT_SAVED = (
    "语音已生成，可能已扣费，但本地记录写入失败（状态目录不可写）。"
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


def _music_generate(args: dict, key: str) -> dict:
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


def _images_generate(args: dict, key: str) -> dict:
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


def _save_tts_audio(mp3: bytes, client_request_id: str) -> tuple:
    """Saves the MP3 bytes; never raises (the paid call already succeeded).
    Returns (path, warning)."""
    safe_id = re.sub(r"[^A-Za-z0-9._-]", "_", client_request_id)
    try:
        out_dir = output_dir()
        if out_dir is None:
            raise OSError("no writable output dir")
        out_path = out_dir / f"{safe_id}.mp3"
        out_path.write_bytes(mp3)
        return str(out_path), None
    except Exception:
        return None, (
            "语音已生成，可能已扣费，但 MP3 无法保存到本地（输出目录不可写）。"
            "请勿重试（重试会再次扣费）；把环境变量 TOKENFISH_OUTPUT_DIR 设为可写目录可避免此问题。"
        )


def _describe_json_error(parsed: Any) -> str:
    """Human-readable failure for a 2xx body that turned out to be JSON (an
    error object) instead of MP3 bytes."""
    if isinstance(parsed, dict):
        err = parsed.get("error")
        if isinstance(err, dict):
            msg = err.get("message") or err.get("type")
            if isinstance(msg, str) and msg:
                return f"语音接口返回错误：{msg}"
        if isinstance(err, str) and err:
            return f"语音接口返回错误：{err}"
        msg = parsed.get("message")
        if isinstance(msg, str) and msg:
            return f"语音接口返回错误：{msg}"
    return "语音接口返回了非 MP3 的响应（JSON 错误体）"


def _submit_tts(clean: dict, key: str) -> bytes:
    payload = {
        "model": TTS_MODEL,
        "input": clean["input"],
        "voice": clean["voice"],
    }
    raw = http_request(
        "POST",
        "/v1/audio/speech",
        key,
        payload=payload,
        timeout=DEFAULT_TIMEOUT,
        as_bytes=True,
        accept="audio/mpeg, audio/mp3, application/json",
    )
    if not raw:
        raise ApiBadResponse("语音接口返回了空响应（非 MP3）")
    try:
        parsed = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return raw  # 不是 JSON → 视为 MP3 字节
    # 2xx 但返回的是 JSON（错误体）而不是 MP3：当作失败。
    raise ApiBadResponse(_describe_json_error(parsed))


def _tts_generate(args: dict, key: str) -> dict:
    clean = validate_tts_args(args)
    crid = clean["client_request_id"]
    ahash = args_hash(clean)

    existing = find_record_by_client_request_id(crid)
    if existing is not None:
        if existing.get("args_hash") != ahash:
            raise err_idempotency_conflict(crid)
        if _record_is_submitted(existing):
            return existing.get("last_task_object", {})
        raise err_outcome_unknown("此前用同一个 client_request_id 发起的提交结果未确认，本次未重新提交")

    try:
        mp3 = _paid_submit(
            crid,
            ahash,
            "tts",
            lambda: _submit_tts(clean, key),  # noqa: E731
            key,
        )
    except _SafeFailure as failure:
        raise failure.tool_error from None

    # The server accepted (and charged) the request: from here on nothing may raise.
    file_path, warning = _save_tts_audio(mp3, crid)
    task_id = f"tts-{crid}-{hashlib.sha256(ahash.encode()).hexdigest()[:8]}"
    warnings = [w for w in [warning] if w]
    task_object = {
        "ok": True,
        "task_id": task_id,
        "provider": "tts",
        "status": "succeeded",
        "clips": [],
        "images": [],
        "file": file_path,
        "charged_cny": None,
        "client_request_id": crid,
    }
    if warnings:
        task_object["warning"] = " ".join(warnings)
    try:
        record_task(crid, "tts", task_id, ahash, task_object)
    except Exception:
        task_object = dict(task_object)
        task_object["warning"] = " ".join(warnings + [WARNING_TTS_RECORD_NOT_SAVED])
    return task_object


WARNING_VIDEO_RECORD_NOT_SAVED = (
    "视频已提交生成，可能已扣费，但本地记录写入失败（状态目录不可写）。"
    "请勿重试，也不要换新的 client_request_id 再次提交；"
    "请用本次返回结果里的 `task_id` 和 `provider`（video）调用 `tokenfish.tasks.get`（两个参数都传）取回结果，"
    "并以钱包流水核对。设置环境变量 TOKENFISH_HOME 为可写目录可避免此问题。"
)


def _video_status_payload(resp: dict) -> dict:
    """Status payload lives under `data`; some gateways flatten it to the top
    level - accept both."""
    data = resp.get("data")
    return data if isinstance(data, dict) else resp


def _fetch_video_status(task_id: str, key: str) -> dict:
    path = "/v1/video/generations/" + urllib.parse.quote(task_id, safe="")
    resp = http_request("GET", path, key, timeout=DEFAULT_TIMEOUT)
    return _video_status_payload(resp)


def _submit_video(clean: dict, key: str, estimated_cost_cny: Optional[float]) -> dict:
    payload = {
        "model": clean["model"],
        "prompt": clean["prompt"],
        "duration": clean["duration"],
        "resolution": clean["resolution"],
    }
    resp = http_request("POST", "/v1/video/generations", key, payload=payload, timeout=DEFAULT_TIMEOUT)
    task_id = resp.get("task_id") or resp.get("id")
    if not isinstance(task_id, str) or not task_id:
        # 2xx but no task: the server accepted (and may have charged) the request.
        raise ApiBadResponse("视频接口未返回 task_id")
    return build_video_task_object({}, task_id, clean["client_request_id"], estimated_cost_cny)


def _curl_download(url: str, key: str, path: Path) -> bool:
    """Fallback downloader for TLS stacks that reject the CDN certificate
    because of a revocation lookup (Windows schannel): `curl --ssl-no-revoke`.
    The key travels through stdin (`--config -`), never on the command line."""
    curl = shutil.which("curl")
    if not curl:
        return False
    if any(ch in key for ch in '"\\\r\n') or any(ch in url for ch in '"\r\n'):
        return False
    config = (
        'silent\nshow-error\nlocation\nfail\nssl-no-revoke\n'
        f'max-time = {VIDEO_DOWNLOAD_TIMEOUT}\n'
        'header = "Authorization: Bearer ' + key + '"\n'
        'header = "Accept: video/mp4, video/*;q=0.9, */*;q=0.1"\n'
        f'url = "{url}"\n'
        f'output = "{path}"\n'
    )
    try:
        proc = subprocess.run(
            [curl, "--config", "-"],
            input=config,
            text=True,
            capture_output=True,
            timeout=VIDEO_DOWNLOAD_TIMEOUT + 10,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return proc.returncode == 0


def _download_video_to_file(url: str, key: str, path: Path, task_id: str) -> str:
    """Streams `url` into `path` (Bearer auth required). Returns the response
    Content-Type. Raises `download_failed` (never retryable, never resubmits)."""
    headers = {
        "User-Agent": build_user_agent(),
        "Authorization": f"Bearer {key}",
        "Accept": "video/mp4, video/*;q=0.9, */*;q=0.1",
    }
    request = urllib.request.Request(url, headers=headers, method="GET")
    try:
        resp = urllib.request.urlopen(request, timeout=VIDEO_DOWNLOAD_TIMEOUT)
    except urllib.error.HTTPError as exc:
        raise err_video_download_failed(task_id, f"下载返回 HTTP {exc.code}") from None
    except urllib.error.URLError as exc:
        reason = exc.reason
        if isinstance(reason, ssl.SSLCertVerificationError) and _curl_download(url, key, path):
            return ""
        detail = reason if isinstance(reason, str) and reason else type(reason).__name__
        raise err_video_download_failed(task_id, f"下载失败：{detail}") from None
    except (OSError, http.client.HTTPException) as exc:
        raise err_video_download_failed(task_id, f"下载失败：{type(exc).__name__}") from None
    content_type = ""
    try:
        with resp:
            content_type = resp.headers.get("Content-Type", "") or ""
            with open(path, "wb") as fh:
                while True:
                    chunk = resp.read(65536)
                    if not chunk:
                        break
                    fh.write(chunk)
    except (OSError, http.client.HTTPException) as exc:
        raise err_video_download_failed(task_id, f"下载中断：{type(exc).__name__}") from None
    return content_type


def _validate_video_file(path: Path, content_type: str, task_id: str) -> None:
    """MP4 sanity gate: not JSON/HTML, >= 1 KB, and an `ftyp` box in the first
    64 bytes. On failure the partial/poisoned file is removed first."""
    def fail(reason: str) -> None:
        try:
            path.unlink()
        except OSError:
            pass
        raise err_video_download_failed(task_id, reason)

    lowered = (content_type or "").lower()
    if "application/json" in lowered or "text/html" in lowered:
        fail(f"服务器返回的是 {content_type} 而不是视频")
    try:
        size = path.stat().st_size
    except OSError:
        fail("下载文件不存在")
    if size < VIDEO_MIN_BYTES:
        fail(f"文件只有 {size} 字节，不像一个视频")
    try:
        with open(path, "rb") as fh:
            head = fh.read(64)
    except OSError:
        fail("无法读取下载文件")
    if b"ftyp" not in head:
        fail("文件头里没有 MP4 的 ftyp 标记")


def _download_video(result_url: Any, key: str, client_request_id: Optional[str], task_id: str) -> str:
    """Download + validate the result MP4 into output_dir() as
    `<safe_crid>.mp4`. Returns the local path. Never resubmits the task."""
    if not _is_http_url(result_url):
        raise err_video_download_failed(task_id, "结果地址不是 http(s) 链接")
    try:
        out_dir = output_dir()
    except Exception:
        out_dir = None
    if out_dir is None:
        raise err_video_download_failed(task_id, "输出目录不可写（可把 TOKENFISH_OUTPUT_DIR 设为可写目录）")
    safe_id = re.sub(r"[^A-Za-z0-9._-]", "_", str(client_request_id or task_id)) or "video"
    out_path = out_dir / f"{safe_id}.mp4"
    part_path = out_dir / f".{safe_id}.part"
    try:
        content_type = _download_video_to_file(result_url, key, part_path, task_id)
        _validate_video_file(part_path, content_type, task_id)
        os.replace(part_path, out_path)
    except ToolError:
        for leftover in (part_path,):
            try:
                leftover.unlink()
            except OSError:
                pass
        raise
    except OSError as exc:
        try:
            part_path.unlink()
        except OSError:
            pass
        raise err_video_download_failed(task_id, f"无法写入本地文件：{type(exc).__name__}") from None
    return str(out_path)


def _poll_video_until_terminal(
    task_id: str, key: str, client_request_id: Optional[str], estimated_cost_cny: Optional[float]
) -> dict:
    """Polls every VIDEO_POLL_INTERVAL seconds for at most VIDEO_POLL_TIMEOUT
    seconds. A terminal status is returned (succeeded -> MP4 downloaded). If
    no terminal status is reached this returns `status: "running"` with the
    task_id and a warning - it never raises and NEVER resubmits."""
    deadline = time.monotonic() + VIDEO_POLL_TIMEOUT
    last = None
    poll_error = None
    while True:
        try:
            data = _fetch_video_status(task_id, key)
        except API_ERRORS as exc:
            poll_error = normalize_api_error(exc, key).message
            break
        last = build_video_task_object(data, task_id, client_request_id, estimated_cost_cny)
        if last["status"] == "succeeded":
            last["file"] = _download_video(last["video_url"], key, client_request_id, task_id)
            return last
        if last["status"] == "failed":
            return last
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        time.sleep(min(VIDEO_POLL_INTERVAL, remaining))

    result = last or build_video_task_object({}, task_id, client_request_id, estimated_cost_cny)
    result["status"] = "running"
    if poll_error:
        result["warning"] = (
            f"查询视频状态时出错（{poll_error}），本次没有拿到终态，任务可能仍在生成。"
            "不要重新提交（会再次扣费）；请稍后用 `tokenfish.tasks.get` 传这次的 `task_id` 和 `provider`（video）再查，"
            "成功后客户端会自动下载 MP4。"
        )
    else:
        result["warning"] = (
            f"视频仍在生成中（本次最多等待约 {VIDEO_POLL_TIMEOUT} 秒），任务已提交、可能已扣费。"
            "不要重新提交（会再次扣费）；请稍后用 `tokenfish.tasks.get` 传这次的 `task_id` 和 `provider`（video）再查，"
            "成功后客户端会自动下载 MP4。"
        )
    return result


def _video_generate(args: dict, key: str) -> dict:
    clean = validate_video_args(args)
    crid = clean["client_request_id"]
    ahash = args_hash(clean)

    existing = find_record_by_client_request_id(crid)
    if existing is not None:
        if existing.get("args_hash") != ahash:
            raise err_idempotency_conflict(crid)
        if _record_is_submitted(existing):
            return _refresh_task(existing, key)
        raise err_outcome_unknown("此前用同一个 client_request_id 发起的提交结果未确认，本次未重新提交")

    estimated_cost_cny = estimate_video_cost_cny(clean["model"], clean["duration"], clean["resolution"])
    try:
        submitted = _paid_submit(
            crid,
            ahash,
            "video",
            lambda: _submit_video(clean, key, estimated_cost_cny),  # noqa: E731
            key,
        )
    except _SafeFailure as failure:
        raise failure.tool_error from None

    # Submitted (and possibly charged): from here on nothing may look retryable.
    task_id = submitted["task_id"]
    try:
        record_task(crid, "video", task_id, ahash, submitted)
        record_warning = None
    except Exception:
        record_warning = WARNING_VIDEO_RECORD_NOT_SAVED

    task_object = _poll_video_until_terminal(task_id, key, crid, estimated_cost_cny)
    if record_warning:
        task_object = dict(task_object)
        task_object["warning"] = " ".join([w for w in (record_warning, task_object.get("warning")) if w])
    update_task_snapshot(crid, task_object)
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
    elif provider == "video":
        try:
            data = _fetch_video_status(task_id, key)
        except API_ERRORS as exc:
            raise normalize_api_error(exc, key) from None
        previous = record.get("last_task_object")
        previous = previous if isinstance(previous, dict) else {}
        estimate = previous.get("estimated_cost_cny")
        task_object = build_video_task_object(data, task_id, crid, estimate)
        if task_object["status"] == "succeeded":
            cached_file = previous.get("file")
            if isinstance(cached_file, str) and cached_file and Path(cached_file).is_file():
                task_object["file"] = cached_file
            else:
                task_object["file"] = _download_video(task_object["video_url"], key, crid, task_id)
    else:
        return record.get("last_task_object", {})
    update_task_snapshot(crid, task_object)
    return task_object


def _tasks_get(args: dict, key: str) -> dict:
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


# --------------------------------------------------------------------------
# v0.2: delivery links on successful paid results
#
# Printed by the client itself (not left to the AI to compose): a `play_url`
# per clip / image, top-level `links`, `balance_after_cny` and `topup_hint`.
# Everything here runs AFTER the result is final and is copied, never stored in
# the local task store; any failure is swallowed so it cannot turn a charged,
# successful result into an error (see SPEC.md "附：防重复扣费").
# --------------------------------------------------------------------------


def _is_http_url(value: Any) -> bool:
    return isinstance(value, str) and value.lower().startswith(("http://", "https://"))


def _fragment_url(page: str, params: dict) -> str:
    """URL of a static page whose data rides in the fragment (browsers never
    send it to the server). Callers must never put the API key in `params`."""
    return f"{SITE_ORIGIN}/{page}#{urllib.parse.urlencode(params)}"


def _play_url(kind: str, media_url: str, title: Any = None, duration: Any = None) -> str:
    if kind == "img":
        return _fragment_url("play.html", {"img": media_url})
    return _fragment_url("play.html", {"src": media_url, "title": title or "", "dur": duration or ""})


def _try_wallet_balance_cny(key: str) -> Optional[float]:
    """Best-effort balance in CNY; None on ANY failure (never raises)."""
    try:
        wallet = fetch_wallet_raw(key, timeout=WALLET_FETCH_TIMEOUT)
        total = wallet.get("total_available")
        if isinstance(total, bool) or not isinstance(total, (int, float)):
            return None
        return round(total / QUOTA_PER_CNY, 2)
    except Exception:
        return None


def _topup_hint(balance_cny: Optional[float]) -> str:
    how = "需要充值时，直接告诉 AI「充值 N 元」（例如「充值 10 元」），AI 会把付款链接发给你。"
    if balance_cny is None:
        return how
    return f"当前余额 ¥{balance_cny:.2f}。{how}"


def _with_delivery_links(task_object: Any, key: str) -> Any:
    """For a task object in the terminal `succeeded` state, returns a COPY with
    `play_url` on every clip / image that has an http(s) URL, plus top-level
    `links`, `balance_after_cny` (omitted if the wallet lookup fails) and
    `topup_hint`. Anything else (queued / running / failed / not a dict) is
    returned untouched. Never raises."""
    try:
        if not isinstance(task_object, dict) or task_object.get("status") != "succeeded":
            return task_object
        out = dict(task_object)
        if isinstance(task_object.get("clips"), list):
            clips = []
            for clip in task_object["clips"]:
                if isinstance(clip, dict):
                    clip = dict(clip)
                    if _is_http_url(clip.get("audio_url")):
                        clip["play_url"] = _play_url(
                            "src", clip["audio_url"], clip.get("title"), clip.get("duration_seconds")
                        )
                clips.append(clip)
            out["clips"] = clips
        if isinstance(task_object.get("images"), list):
            images = []
            for image in task_object["images"]:
                if isinstance(image, dict):
                    image = dict(image)
                    if _is_http_url(image.get("url")):
                        image["play_url"] = _play_url("img", image["url"])
                images.append(image)
            out["images"] = images
        out["links"] = {"site": OFFICIAL_SITE_URL, "tokenfish": SITE_ORIGIN}
        balance = _try_wallet_balance_cny(key)
        if balance is not None:
            out["balance_after_cny"] = balance
        out["topup_hint"] = _topup_hint(balance)
        return out
    except Exception:
        return task_object


def tool_music_generate(args: dict, key: str) -> dict:
    return _with_delivery_links(_music_generate(args, key), key)


def tool_images_generate(args: dict, key: str) -> dict:
    return _with_delivery_links(_images_generate(args, key), key)


def tool_tts_generate(args: dict, key: str) -> dict:
    return _with_delivery_links(_tts_generate(args, key), key)


def tool_video_generate(args: dict, key: str) -> dict:
    return _with_delivery_links(_video_generate(args, key), key)


def tool_tasks_get(args: dict, key: str) -> dict:
    return _with_delivery_links(_tasks_get(args, key), key)


# --------------------------------------------------------------------------
# v0.2: wallet top-up tools (never billed; the order is only paid when the
# user pays it themselves, so every failure here is safe to retry)
# --------------------------------------------------------------------------


def validate_topup_args(args: dict) -> dict:
    if not isinstance(args, dict):
        raise err_invalid_arguments("args", "必须是 JSON 对象")

    amount = args.get("amount")
    if amount is None:
        raise err_invalid_arguments("amount", "缺少必填参数")
    if isinstance(amount, bool) or not isinstance(amount, (int, float)):
        raise err_invalid_arguments("amount", "必须是数字（单位：元）")
    if not (TOPUP_AMOUNT_MIN <= amount <= TOPUP_AMOUNT_MAX):  # also rejects NaN / inf
        raise err_invalid_arguments(
            "amount", f"取值范围必须是 {TOPUP_AMOUNT_MIN}..{TOPUP_AMOUNT_MAX} 的整数（单位：元）"
        )
    # Whole yuan only: the backend turns the amount into fen with int64(amount*100),
    # which truncates fractional yuan (e.g. 1.15 -> 114 fen).
    if isinstance(amount, float):
        if not amount.is_integer():
            raise err_invalid_arguments("amount", "必须是整数元，不支持小数")
        amount = int(amount)

    channel = args.get("channel")
    if channel is None:
        channel = "alipay"
    if channel not in TOPUP_CHANNELS:
        raise err_invalid_arguments("channel", f"必须是以下之一：{', '.join(TOPUP_CHANNELS)}（默认 alipay）")

    return {"amount": amount, "channel": channel}


def _normalize_topup_error(exc: Exception, channel: str) -> ToolError:
    """Maps the real pay-server answers of POST /recharge/create (handler.go
    CreateOrder / createAlipayOrder / createWechatOrder)."""
    if isinstance(exc, ApiHttpError):
        body = exc.body if isinstance(exc.body, dict) else {}
        err = body.get("error")
        err_text = err if isinstance(err, str) else ""
        message = body.get("message") if isinstance(body.get("message"), str) else ""
        if exc.status == 429:
            return err_rate_limited_free()
        if exc.status == 400:
            if "not configured" in err_text:  # "alipay not configured" / "wechat pay not configured"
                return err_channel_unavailable(channel)
            if err_text == "token provision failed" or "Key 建档失败" in message:
                return err_transient_free("Key 建档失败，暂不能发起支付")
            if "amount" in err_text:
                return err_invalid_arguments("amount", "必须是 1..500 的整数元")
            return err_invalid_arguments("amount/channel", "服务端认为请求无效（invalid request）")
        return err_transient_free(f"HTTP {exc.status}")
    if isinstance(exc, (ApiTransientError, ApiBadResponse)):
        return err_transient_free(exc.detail)
    return err_transient_free(type(exc).__name__)


def _build_pay_link(channel: str, resp: dict, order_id: str, amount: Any, key: str) -> str:
    """alipay -> the gateway's payUrl as-is; wechat -> our static pay page (it
    draws the QR from the weixin:// code URL carried in the fragment). The key
    is never part of any link we hand out."""
    if channel == "alipay":
        link = resp.get("payUrl")
        if not _is_http_url(link):
            raise err_transient_free("下单响应缺少 payUrl")
    else:
        code_url = resp.get("codeUrl")
        if not (isinstance(code_url, str) and code_url.startswith("weixin://")):
            raise err_transient_free("下单响应缺少 codeUrl")
        link = _fragment_url("pay.html", {"wx": code_url, "amt": amount, "order": order_id})
    if key and key in link:
        raise err_transient_free("下单响应异常")
    return link


def tool_wallet_topup(args: dict, key: str) -> dict:
    clean = validate_topup_args(args)
    amount, channel = clean["amount"], clean["channel"]
    # The key travels ONLY in this POST body (no Authorization header, no URL).
    body = {"apiKey": key, "amount": amount, "channel": channel}
    try:
        resp = http_request("POST", "/recharge/create", None, payload=body, timeout=DEFAULT_TIMEOUT)
    except API_ERRORS as exc:
        raise _normalize_topup_error(exc, channel) from None
    order_id = resp.get("orderId")
    if not isinstance(order_id, str) or not order_id:
        raise err_transient_free("下单响应缺少 orderId")
    pay_link = _build_pay_link(channel, resp, order_id, amount, key)
    return {
        "order_id": order_id,
        "channel": channel,
        "amount_cny": amount,
        "pay_link": pay_link,
        "next_step": (
            "请用户打开 pay_link 完成付款"
            + ("（支付宝：在浏览器里打开）" if channel == "alipay" else "（微信：打开页面后用微信扫码）")
            + "；付完回到对话说「好了」，再调用 tokenfish.wallet.topup_status 查是否到账。"
        ),
    }


def tool_wallet_topup_status(args: dict, key: str) -> dict:
    if not isinstance(args, dict):
        raise err_invalid_arguments("args", "必须是 JSON 对象")
    order_id = args.get("order_id")
    if not isinstance(order_id, str) or not order_id:
        raise err_invalid_arguments("order_id", "缺少必填参数")
    if len(order_id) > 200:
        raise err_invalid_arguments("order_id", "长度不能超过 200 个字符")
    path = "/recharge/status?order_id=" + urllib.parse.quote(order_id, safe="")
    try:
        resp = http_request("GET", path, None, timeout=DEFAULT_TIMEOUT)
    except ApiHttpError as exc:
        if exc.status == 404:
            raise err_order_not_found(order_id) from None
        if exc.status == 400:
            raise err_invalid_arguments("order_id", "服务端认为订单号无效") from None
        if exc.status == 429:
            raise err_rate_limited_free() from None
        raise err_transient_free(f"HTTP {exc.status}") from None
    except (ApiTransientError, ApiBadResponse) as exc:
        raise err_transient_free(exc.detail) from None
    status = resp.get("status")  # pending / paid / recharged / failed (store.go Order.Status)
    if not isinstance(status, str) or not status:
        raise err_transient_free("订单状态响应缺少 status")
    credited = status == "recharged"
    echoed = resp.get("orderId")
    result = {
        "order_id": echoed if isinstance(echoed, str) and echoed else order_id,
        "status": status,
        "credited": credited,
    }
    if credited:
        balance = _try_wallet_balance_cny(key)
        if balance is not None:
            result["balance_cny"] = balance
    return result


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
        "name": "tokenfish.tts.generate",
        "description": "把文字转成语音配音（计费）：一句话生成 MP3，可选女声/男声",
        "billed": True,
        "parameters": {
            "type": "object",
            "properties": {
                "input": {
                    "type": "string",
                    "description": "要合成的文本；计费字符数不超过 5000（汉字按 2、其他按 1 计）",
                },
                "voice": {"type": "string", "enum": list(TTS_VOICES), "default": TTS_DEFAULT_VOICE},
                "client_request_id": {"type": "string", "maxLength": 200},
            },
            "required": ["input", "client_request_id"],
        },
        "handler": tool_tts_generate,
    },
    {
        "name": "tokenfish.video.generate",
        "description": "一句话做视频（计费）：给画面描述，按秒计费，异步生成 MP4；最多等待约 180 秒，没出片就返回 running 和 task_id，用 tokenfish.tasks.get 再查",
        "billed": True,
        "parameters": {
            "type": "object",
            "properties": {
                "prompt": {"type": "string", "maxLength": 2000, "description": "画面描述"},
                "duration": {
                    "type": "integer",
                    "minimum": VIDEO_DURATION_MIN,
                    "maximum": VIDEO_DURATION_MAX,
                    "default": VIDEO_DURATION_MIN,
                    "description": "时长（秒），5..15",
                },
                "resolution": {
                    "type": "string",
                    "enum": list(VIDEO_RESOLUTIONS),
                    "default": "480p",
                },
                "model": {
                    "type": "string",
                    "enum": list(VIDEO_MODELS),
                    "default": VIDEO_DEFAULT_MODEL,
                },
                "client_request_id": {"type": "string", "maxLength": 200},
            },
            "required": ["prompt", "client_request_id"],
        },
        "handler": tool_video_generate,
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
    {
        "name": "tokenfish.wallet.topup",
        "description": "给钱包充值（不扣费）：返回付款链接，用户自己付款；付完用 tokenfish.wallet.topup_status 查到账",
        "billed": False,
        "parameters": {
            "type": "object",
            "properties": {
                "amount": {
                    "type": "integer",
                    "minimum": TOPUP_AMOUNT_MIN,
                    "maximum": TOPUP_AMOUNT_MAX,
                    "description": "充值金额（整数元），1..500，不支持小数",
                },
                "channel": {"type": "string", "enum": list(TOPUP_CHANNELS), "default": "alipay"},
            },
            "required": ["amount"],
        },
        "handler": tool_wallet_topup,
    },
    {
        "name": "tokenfish.wallet.topup_status",
        "description": "查询充值订单是否到账（不扣费）；到账后附带钱包余额",
        "billed": False,
        "parameters": {
            "type": "object",
            "properties": {
                "order_id": {"type": "string", "description": "tokenfish.wallet.topup 返回的 order_id"},
            },
            "required": ["order_id"],
        },
        "handler": tool_wallet_topup_status,
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


def _already_configured_payload(key: str) -> dict:
    base = state_dir()
    return {
        "status": "already_configured",
        "key_masked": mask_key(key),
        "state_dir": str(base) if base is not None else None,
    }


def _is_durable_location(saved_to: Path) -> bool:
    """True if the credentials landed in ~/.tokenfish or $TOKENFISH_HOME (a
    place the user chose or the normal home); False for the tmp / cwd fallbacks
    a sandbox may wipe."""
    parent = saved_to.parent
    env = os.environ.get("TOKENFISH_HOME")
    return parent == HOME_STATE_DIR or (bool(env) and parent == Path(env))


# No trial credit: a new wallet is empty, so the very next thing is a top-up.
SETUP_NEXT_STEP = (
    "新钱包余额为 0，需要先充值才能生成。告诉 AI 要充多少元（例如「充值 10 元」，1 到 500 之间的整数元均可），"
    "选支付宝或微信，就能马上拿到付款链接。"
)


def _backup_notice(saved_to: Path) -> str:
    notice = (
        f"这把 key 就是你的钱包：余额存放在它上面，谁拿到 key 谁就能花掉余额，丢了无法找回。"
        f"请备份文件 {saved_to}（复制到安全的地方，不要发到聊天里）。"
        "如果当前 AI 工具的沙箱可能清空这个位置（会话结束后文件丢失），"
        "请把环境变量 TOKENFISH_HOME 设为一个持久的可写目录；"
        "想在别的机器或别的工具里用同一个钱包，把 key 存成文件后运行 `login --key-file <path>` 导入。"
    )
    if not _is_durable_location(saved_to):
        notice = "注意：本机 home 目录不可写，key 暂存在临时位置，会话结束后可能丢失，请马上备份。" + notice
    return notice


def cmd_setup() -> tuple[dict, int]:
    """Open a new, empty wallet (POST /device/bind) when NO credentials exist
    anywhere (incl. ~/.uking/device.json). Never replaces an existing key."""
    key = load_credentials()
    if key:
        return _already_configured_payload(key), 0
    if state_dir() is None:
        # Nowhere to keep a key: don't create a wallet we could never use.
        e = err_setup_unwritable(wallet_created=False)
        return e.to_payload(), e.exit_code
    # Concurrency guard: another process / AI tool may have just finished its own setup.
    key = load_credentials()
    if key:
        return _already_configured_payload(key), 0

    body = {
        "platform": detect_platform(),
        "channel": f"tokenfish-{find_package_slug(Path(__file__).resolve())}",
    }
    try:
        resp = http_request("POST", "/device/bind", None, payload=body, timeout=DEFAULT_TIMEOUT)
    except ApiHttpError as exc:
        e = err_setup_rate_limited() if exc.status == 429 else err_setup_transient(f"HTTP {exc.status}")
        return e.to_payload(), e.exit_code
    except (ApiTransientError, ApiBadResponse) as exc:
        e = err_setup_transient(exc.detail)
        return e.to_payload(), e.exit_code

    new_key = resp.get("apiKey")
    if not isinstance(new_key, str) or not new_key.strip():
        e = err_setup_transient("响应缺少 apiKey")
        return e.to_payload(), e.exit_code
    new_key = new_key.strip()
    wallet_id = resp.get("walletId") if isinstance(resp.get("walletId"), str) and resp.get("walletId") else None

    # Credentials may have appeared while the request was in flight: never overwrite
    # them (the fresh wallet is empty, an existing one may hold money).
    other = load_credentials()
    if other:
        return _already_configured_payload(other), 0

    try:
        saved_to = save_credentials(new_key, wallet_id)
    except OSError:
        e = err_setup_unwritable(wallet_created=True)
        return e.to_payload(), e.exit_code

    return {
        "status": "created",
        "wallet_id": wallet_id,
        "key_masked": mask_key(new_key),
        "saved_to": str(saved_to),
        "balance_cny": _round_cny(resp.get("balanceTokens")),
        "backup_notice": _backup_notice(saved_to),
        "next_step": SETUP_NEXT_STEP,
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
    sub.add_parser("setup", help="没有任何凭据时，免注册开一个新钱包（已有凭据则不动）")

    call_parser = sub.add_parser("call", help="调用一个工具，参数 JSON 从 stdin 读取")
    call_parser.add_argument("tool", help="工具名，如 tokenfish.wallet.get")

    login_parser = sub.add_parser("login", help="从文件读取 key 并写入凭据文件")
    login_parser.add_argument("--key-file", required=True, help="包含 key 的文件路径")

    sub.add_parser("whoami", help="显示打码后的 key 和余额")
    sub.add_parser(
        "topup",
        help="在本机浏览器打开充值页（桌面环境用）",
        description="在本机浏览器打开充值页（桌面环境用）；会把 key 复制到剪贴板方便粘贴。AI 对话里充值请用 tokenfish.wallet.topup 工具。",
    )

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
    elif ns.command == "setup":
        payload, code = cmd_setup()
    elif ns.command == "topup":
        payload, code = cmd_topup()
    else:  # pragma: no cover
        parser.print_usage(sys.stderr)
        return 2

    print(json.dumps(payload, ensure_ascii=False, indent=2))
    return code


if __name__ == "__main__":
    sys.exit(main())
