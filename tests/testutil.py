"""Shared test utilities: isolate HOME/USERPROFILE per test and import the
client module both in-process (for unit tests of internal functions) and via
subprocess (for true end-to-end / stdout-stderr leak checks).
"""

from __future__ import annotations

import importlib
import os
import subprocess
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
CLIENT_PATH = REPO_ROOT / "client" / "tokenfish_client.py"
CLIENT_DIR = str(REPO_ROOT / "client")


def import_client_module():
    """(Re-)import tokenfish_client with a fresh module object so module-level
    constants like CREDENTIALS_PATH (derived from Path.home()) are recomputed
    against the current HOME env var."""
    if CLIENT_DIR not in sys.path:
        sys.path.insert(0, CLIENT_DIR)
    if "tokenfish_client" in sys.modules:
        del sys.modules["tokenfish_client"]
    return importlib.import_module("tokenfish_client")


def make_isolated_home() -> Path:
    home = Path(tempfile.mkdtemp(prefix="tokenfish-test-home-"))
    os.environ["HOME"] = str(home)
    os.environ["USERPROFILE"] = str(home)
    return home


def run_cli(args: list, env: dict, input_text: str = "") -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, str(CLIENT_PATH)] + args,
        input=input_text,
        capture_output=True,
        text=True,
        env=env,
        timeout=30,
    )


def base_env(home: Path, api_base: str = "") -> dict:
    env = dict(os.environ)
    env["HOME"] = str(home)
    env["USERPROFILE"] = str(home)
    if api_base:
        env["TOKENFISH_API_BASE"] = api_base
    # Make sure attribution probes deployed on the test machine's real
    # username/paths don't accidentally match DoubaoWork/WorkBuddy substrings.
    env.pop("CLAUDECODE", None)
    env.pop("CLAUDE_CODE", None)
    for k in list(env.keys()):
        if k.startswith("CODEX_"):
            env.pop(k, None)
    return env
