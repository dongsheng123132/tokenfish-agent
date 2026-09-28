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
    (not logged in / insufficient balance), 4 transient (safe to retry).

This module is designed to be copied into `scripts/tokenfish_client.py`
inside a Skill package by build/build.py. When run standalone (e.g. from
client/ for testing) it still works; package_slug attribution falls back to
"tokenfish-client".
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import platform as platform_mod
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Optional

CLIENT_VERSION = "0.1.0"

DEFAULT_API_BASE = "https://api.u-claw.org.cn"
TOPUP_URL = "https://api.u-claw.org/recharge"

QUOTA_PER_CNY = 500000

DEFAULT_TIMEOUT = 30
MINIMAX_TIMEOUT = 240

CREDENTIALS_PATH = Path.home() / ".tokenfish" / "credentials.json"
UKING_DEVICE_PATH = Path.home() / ".uking" / "device.json"
TASKS_PATH = Path.home() / ".tokenfish" / "tasks.json"

OUTPUT_DIR = Path("tokenfish-outputs")

IDEMPOTENCY_KEY_RE = re.compile(r"^[A-Za-z0-9\-_.]{12,128}$")

MUSIC_MODELS = ("auto", "suno", "minimax")
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


def err_unknown_task(task_id: str) -> ToolError:
    return ToolError(
        "invalid_arguments",
        f"未找到本地记录的任务 `{task_id}`。",
        False,
        "确认 task_id 来自本客户端此前的返回值",
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


def save_credentials(key: str) -> None:
    CREDENTIALS_PATH.parent.mkdir(parents=True, exist_ok=True)
    try:
        if os.name != "nt":
            os.chmod(CREDENTIALS_PATH.parent, 0o700)
    except OSError:
        pass
    fd, tmp_name = tempfile.mkstemp(
        prefix=".credentials-", suffix=".tmp", dir=str(CREDENTIALS_PATH.parent)
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump({"key": key}, fh)
        os.replace(tmp_name, CREDENTIALS_PATH)
    finally:
        if os.path.exists(tmp_name):
            try:
                os.remove(tmp_name)
            except OSError:
                pass
    _tighten_permissions(CREDENTIALS_PATH)


def load_credentials() -> Optional[str]:
    """Returns the API key, or None. Never writes ~/.uking/device.json."""
    if CREDENTIALS_PATH.exists():
        try:
            data = json.loads(CREDENTIALS_PATH.read_text(encoding="utf-8"))
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
    def __init__(self, status: int, body: Any):
        super().__init__(f"HTTP {status}")
        self.status = status
        self.body = body


class ApiTransientError(Exception):
    def __init__(self, detail: str):
        super().__init__(detail)
        self.detail = detail


def api_base() -> str:
    return os.environ.get("TOKENFISH_API_BASE", DEFAULT_API_BASE).rstrip("/")


def http_request(
    method: str,
    path: str,
    key: str,
    payload: Optional[dict] = None,
    timeout: int = DEFAULT_TIMEOUT,
) -> dict:
    """Raises ApiHttpError for non-2xx responses, ApiTransientError for
    network/timeout problems. Never includes the key in any raised message."""
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
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as resp:
            raw = resp.read()
            if not raw:
                return {}
            try:
                return json.loads(raw.decode("utf-8"))
            except json.JSONDecodeError as exc:
                raise ApiTransientError(f"响应不是合法 JSON：{exc}") from None
    except urllib.error.HTTPError as exc:
        raw = exc.read()
        try:
            body = json.loads(raw.decode("utf-8")) if raw else {}
        except json.JSONDecodeError:
            body = {"raw": raw.decode("utf-8", errors="replace")}
        raise ApiHttpError(exc.code, body) from None
    except urllib.error.URLError as exc:
        raise ApiTransientError(str(exc.reason)) from None
    except TimeoutError as exc:
        raise ApiTransientError("请求超时") from None


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
    if isinstance(exc, ApiTransientError):
        return err_transient(exc.detail)
    return err_transient(str(exc))


# --------------------------------------------------------------------------
# Local task store (client-side idempotency, §2.4)
# --------------------------------------------------------------------------


def _load_task_store() -> dict:
    if not TASKS_PATH.exists():
        return {}
    try:
        return json.loads(TASKS_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _save_task_store(store: dict) -> None:
    TASKS_PATH.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=".tasks-", suffix=".tmp", dir=str(TASKS_PATH.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(store, fh, ensure_ascii=False, indent=2)
        os.replace(tmp_name, TASKS_PATH)
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


def record_task(client_request_id: str, provider: str, task_id: str, ahash: str, task_object: dict) -> None:
    store = _load_task_store()
    store[client_request_id] = {
        "client_request_id": client_request_id,
        "args_hash": ahash,
        "task_id": task_id,
        "provider": provider,
        "created_at": time.time(),
        "last_task_object": task_object,
    }
    _save_task_store(store)


def update_task_snapshot(client_request_id: str, task_object: dict) -> None:
    store = _load_task_store()
    if client_request_id in store:
        store[client_request_id]["last_task_object"] = task_object
        _save_task_store(store)


def find_record_by_client_request_id(client_request_id: str) -> Optional[dict]:
    store = _load_task_store()
    return store.get(client_request_id)


def find_record_by_task_id(task_id: str) -> Optional[dict]:
    store = _load_task_store()
    for record in store.values():
        if record.get("task_id") == task_id:
            return record
    return None


def list_local_tasks(limit: int = 20) -> list:
    store = _load_task_store()
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
    except (ApiHttpError, ApiTransientError) as exc:
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
    if not task_id:
        raise ApiTransientError("Suno 未返回 task_id")
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
    return build_minimax_task_object(resp, clean["client_request_id"])


def tool_music_generate(args: dict, key: str) -> dict:
    clean = validate_music_args(args)
    crid = clean["client_request_id"]
    ahash = args_hash(clean)

    existing = find_record_by_client_request_id(crid)
    if existing is not None:
        if existing.get("args_hash") != ahash:
            raise err_idempotency_conflict(crid)
        return _refresh_task(existing, key)

    model = clean["model"]
    if model == "minimax":
        try:
            task_object = _submit_minimax(clean, key)
        except (ApiHttpError, ApiTransientError) as exc:
            raise normalize_api_error(exc, key) from None
        provider = "minimax"
    elif model == "suno":
        try:
            task_object = _submit_suno(clean, key)
        except (ApiHttpError, ApiTransientError) as exc:
            raise normalize_api_error(exc, key) from None
        provider = "suno"
    else:  # auto: suno first, fall back to minimax unless it's a balance issue
        try:
            task_object = _submit_suno(clean, key)
            provider = "suno"
        except (ApiHttpError, ApiTransientError) as exc:
            normalized = normalize_api_error(exc, key)
            if normalized.code == "insufficient_balance":
                raise normalized from None
            try:
                task_object = _submit_minimax(clean, key)
            except (ApiHttpError, ApiTransientError) as exc2:
                raise normalize_api_error(exc2, key) from None
            provider = "minimax"

    record_task(crid, provider, task_object["task_id"], ahash, task_object)
    return task_object


def tool_images_generate(args: dict, key: str) -> dict:
    clean = validate_images_args(args)
    crid = clean["client_request_id"]
    ahash = args_hash(clean)

    existing = find_record_by_client_request_id(crid)
    if existing is not None:
        if existing.get("args_hash") != ahash:
            raise err_idempotency_conflict(crid)
        return existing.get("last_task_object", {})

    payload = {
        "model": "gpt-image-2",
        "prompt": clean["prompt"],
        "n": clean["n"],
        "size": clean["size"],
        "quality": clean["quality"],
    }
    try:
        resp = http_request("POST", "/v1/images/generations", key, payload=payload, timeout=DEFAULT_TIMEOUT)
    except (ApiHttpError, ApiTransientError) as exc:
        raise normalize_api_error(exc, key) from None

    images = []
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    for idx, item in enumerate(resp.get("data") or []):
        entry = {"url": item.get("url"), "path": None}
        b64 = item.get("b64_json")
        if b64:
            out_path = OUTPUT_DIR / f"{crid}-{idx}.png"
            out_path.write_bytes(base64.b64decode(b64))
            entry["path"] = str(out_path)
        images.append(entry)

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
    record_task(crid, "image", task_id, ahash, task_object)
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
        except (ApiHttpError, ApiTransientError) as exc:
            raise normalize_api_error(exc, key) from None
        task_object = build_suno_fetch_task_object(resp, task_id, crid)
    elif provider == "minimax":
        try:
            resp = http_request("GET", f"/v1/music/generations/{task_id}", key, timeout=DEFAULT_TIMEOUT)
        except (ApiHttpError, ApiTransientError) as exc:
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
    record = find_record_by_task_id(task_id)
    if record is None:
        raise err_unknown_task(task_id)
    return _refresh_task(record, key)


def tool_tasks_list(args: dict, key: str) -> dict:
    records = list_local_tasks(limit=20)
    return {
        "tasks": [
            {
                "client_request_id": r.get("client_request_id"),
                "provider": r.get("provider"),
                "task_id": r.get("task_id"),
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
            "properties": {"task_id": {"type": "string"}},
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
    except (ApiHttpError, ApiTransientError) as exc:
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
        e = err_transient(f"未预期的内部错误：{type(exc).__name__}")
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
    save_credentials(content)
    return {"ok": True, "message": "凭据已保存", "key_masked": mask_key(content)}, 0


def cmd_whoami() -> tuple[dict, int]:
    key = load_credentials()
    if not key:
        e = err_not_logged_in()
        return e.to_payload(), e.exit_code
    try:
        wallet = fetch_wallet_raw(key)
    except (ApiHttpError, ApiTransientError) as exc:
        e = normalize_api_error(exc, key)
        return e.to_payload(), e.exit_code
    total_available = wallet.get("total_available", 0) or 0
    return (
        {
            "key_masked": mask_key(key),
            "balance_cny": round(total_available / QUOTA_PER_CNY, 2),
            "total_available_quota": total_available,
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
