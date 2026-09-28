"""The API key must never appear in stdout or stderr, under any command or
error path. These tests run the real CLI as a subprocess (not in-process
function calls) so they observe exactly what a caller would see.

Deliberately NOT exercised here: `topup` with valid credentials, because that
branch calls webbrowser.open() and would pop a real browser window on the
test machine. The not-logged-in branch of `topup` (which returns before
touching the browser) is covered instead.
"""

import json
import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from fake_server import FakeBackend  # noqa: E402
from testutil import base_env, make_isolated_home, run_cli  # noqa: E402

SECRET_KEY = "sk-SUPERSECRETFAKEKEY9999"
SECRET_FRAGMENT = "SUPERSECRETFAKEKEY"


def _assert_no_leak(testcase: unittest.TestCase, proc):
    combined = (proc.stdout or "") + (proc.stderr or "")
    testcase.assertNotIn(SECRET_KEY, combined, msg=f"full key leaked: {combined!r}")
    testcase.assertNotIn(SECRET_FRAGMENT, combined, msg=f"key fragment leaked: {combined!r}")


class TestKeyNeverLeaked(unittest.TestCase):
    def setUp(self):
        self.home = make_isolated_home()
        tf_dir = self.home / ".tokenfish"
        tf_dir.mkdir(parents=True, exist_ok=True)
        (tf_dir / "credentials.json").write_text(json.dumps({"key": SECRET_KEY}), encoding="utf-8")

        self.backend = FakeBackend()
        self.base_url = self.backend.start()
        self.backend.set_response(
            "GET",
            "/api/usage/token/",
            200,
            {"code": True, "data": {"name": "u", "total_available": 1000000, "total_used": 0, "total_granted": 1000000}},
        )
        self.backend.set_response(
            "POST",
            "/v1/music/generations",
            200,
            {"id": "mm-leak-1", "status": "succeeded", "output_url": "https://x/a.mp3"},
        )
        self.env = base_env(self.home, api_base=self.base_url)

    def tearDown(self):
        self.backend.stop()

    def test_whoami_does_not_leak_key(self):
        proc = run_cli(["whoami"], self.env)
        _assert_no_leak(self, proc)
        payload = json.loads(proc.stdout)
        self.assertTrue(payload["key_masked"].endswith("9999"))
        self.assertNotIn(SECRET_FRAGMENT, payload["key_masked"])

    def test_verify_does_not_leak_key(self):
        proc = run_cli(["verify"], self.env)
        _assert_no_leak(self, proc)

    def test_call_wallet_get_does_not_leak_key(self):
        proc = run_cli(["call", "tokenfish.wallet.get"], self.env, input_text="{}")
        _assert_no_leak(self, proc)

    def test_call_music_generate_success_does_not_leak_key(self):
        args = {"prompt": "synthwave", "model": "minimax", "client_request_id": "leak-check-1"}
        proc = run_cli(["call", "tokenfish.music.generate"], self.env, input_text=json.dumps(args))
        _assert_no_leak(self, proc)

    def test_call_music_generate_error_path_does_not_leak_key(self):
        self.backend.set_response(
            "POST",
            "/v1/music/generations",
            402,
            {"error": {"type": "insufficient_quota", "message": "no quota"}},
        )
        args = {"prompt": "synthwave", "model": "minimax", "client_request_id": "leak-check-2"}
        proc = run_cli(["call", "tokenfish.music.generate"], self.env, input_text=json.dumps(args))
        _assert_no_leak(self, proc)
        payload = json.loads(proc.stdout)
        self.assertEqual(payload["error"]["code"], "insufficient_balance")

    def test_call_with_401_and_secondary_wallet_check_does_not_leak_key(self):
        self.backend.set_response("POST", "/v1/music/generations", 401, {"error": "Invalid token"})
        args = {"prompt": "synthwave", "model": "minimax", "client_request_id": "leak-check-3"}
        proc = run_cli(["call", "tokenfish.music.generate"], self.env, input_text=json.dumps(args))
        _assert_no_leak(self, proc)

    def test_login_does_not_leak_key(self):
        key_file = self.home / "keyfile.txt"
        key_file.write_text(SECRET_KEY, encoding="utf-8")
        proc = run_cli(["login", "--key-file", str(key_file)], self.env)
        _assert_no_leak(self, proc)
        payload = json.loads(proc.stdout)
        self.assertTrue(payload["key_masked"].endswith("9999"))

    def test_topup_not_logged_in_does_not_leak_key_and_does_not_open_browser(self):
        # Fresh home with no credentials at all -> must exit before touching
        # webbrowser.open or the clipboard.
        empty_home = make_isolated_home()
        env = base_env(empty_home, api_base=self.base_url)
        proc = run_cli(["topup"], env)
        _assert_no_leak(self, proc)
        payload = json.loads(proc.stdout)
        self.assertEqual(payload["error"]["code"], "not_logged_in")
        self.assertEqual(proc.returncode, 3)


if __name__ == "__main__":
    unittest.main()
