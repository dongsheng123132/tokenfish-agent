"""Never double-charge (SPEC.md "附：防重复扣费").

Principle under test: anything that may already have been charged is never
reported as retryable. Every test asserts how many *submit* requests the fake
backend actually received -- that number is the thing that costs money.

T1  incident replay: state not writable AFTER the paid submit
T2  no writable state dir anywhere -> nothing submitted
T3  home unwritable -> record lands in <cwd>/.tokenfish/tasks.json
T4  Suno response lost -> outcome_unknown, no retry, no MiniMax fallback
T5  Suno 503 in auto mode -> MiniMax fallback, record says minimax
T6  Suno 2xx with a non-JSON body -> outcome_unknown
T7  connection refused -> transient, retryable, "submitting" record removed
T8  MiniMax maybe-sent timeout -> retryable, same idempotency_key on retry
T9  images: output dir unwritable -> result + warning, not an error
T10 safety net: unexpected error after a paid submit -> outcome_unknown

Follow-up round:
A   record lookup does not depend on cwd (tmp before cwd, union read of all candidate dirs)
B   PermissionError / ENETUNREACH / EHOSTUNREACH / ECONNREFUSED / cert-verify inside
    URLError.reason are "definitely not sent"
C   MiniMax 2xx with a bad body stays retryable (idempotency_key), record kept as minimax
D   tasks.get accepts `provider` when there is no local record
"""

import base64
import errno
import json
import os
import shutil
import socket
import ssl
import sys
import tempfile
import time
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
from fake_server import FakeBackend  # noqa: E402
from testutil import SYSTEM_TMP, base_env, import_client_module, make_isolated_home, run_cli  # noqa: E402

SUNO_SUBMIT = ("POST", "/suno/submit/music")
MM_SUBMIT = ("POST", "/v1/music/generations")
IMG_SUBMIT = ("POST", "/v1/images/generations")
MUSIC_TOOL = "tokenfish.music.generate"
IMAGES_TOOL = "tokenfish.images.generate"
KEY = "sk-nodoublechargekey01"
TINY_B64 = base64.b64encode(b"\x89PNG\r\n\x1a\nfake-image-bytes").decode("ascii")


class _Base(unittest.TestCase):
    def setUp(self):
        self.home = make_isolated_home()
        self.cwd = Path(tempfile.mkdtemp(prefix="tokenfish-test-cwd-", dir=SYSTEM_TMP))
        old_cwd = os.getcwd()
        self.addCleanup(shutil.rmtree, self.cwd, True)
        self.addCleanup(shutil.rmtree, self.home, True)
        self.addCleanup(os.chdir, old_cwd)
        os.chdir(self.cwd)

        self.backend = FakeBackend()
        self.base_url = self.backend.start()
        self.addCleanup(self.backend.stop)
        os.environ["TOKENFISH_API_BASE"] = self.base_url

        cred_dir = self.home / ".tokenfish"
        cred_dir.mkdir(parents=True)
        (cred_dir / "credentials.json").write_text(json.dumps({"key": KEY}), encoding="utf-8")
        self.tf = import_client_module()
        # make_isolated_home() redirected the temp dir, so this is NOT the real one
        self.fake_tmp = Path(tempfile.gettempdir())
        self.tmp_state = self.tf._tmp_state_dir()

    # -- helpers ----------------------------------------------------------

    def call(self, tool, args):
        return self.tf.cmd_call(tool, json.dumps(args))

    def music(self, crid, **extra):
        args = {"prompt": "city pop", "client_request_id": crid}
        args.update(extra)
        return self.call(MUSIC_TOOL, args)

    def images(self, crid, **extra):
        args = {"prompt": "a red fish", "client_request_id": crid}
        args.update(extra)
        return self.call(IMAGES_TOOL, args)

    def n(self, route):
        return self.backend.count(*route)

    def store(self):
        base = self.tf.state_dir()
        path = base / "tasks.json"
        return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}

    def suno_ok(self, task_id="task-abc"):
        self.backend.set_response(*SUNO_SUBMIT, 200, {"code": "success", "data": task_id})

    def minimax_ok(self, task_id="mm-1"):
        self.backend.set_response(
            *MM_SUBMIT, 200, {"id": task_id, "object": "music", "status": "running"}
        )

    def stub_unwritable(self, predicate):
        """Make _dir_is_writable() say False for paths matching predicate."""
        real = self.tf._dir_is_writable

        def stub(path):
            return False if predicate(Path(path)) else real(path)

        self.tf._dir_is_writable = stub
        self.tf.reset_state_dir_cache()

    def stub_home_and_tmp_unwritable(self):
        home_dir = self.tf.HOME_STATE_DIR
        tmp_dir = self.tf._tmp_state_dir()
        self.stub_unwritable(lambda p: p == home_dir or p == tmp_dir)

    def new_cwd(self, name):
        path = self.cwd / name
        path.mkdir()
        os.chdir(path)
        return path

    def assertErrorPayload(self, payload, code, exit_code, actual_exit, retryable):
        self.assertIn("error", payload, msg=f"expected an error payload, got {payload!r}")
        self.assertEqual(payload["error"]["code"], code)
        self.assertEqual(payload["error"]["retryable"], retryable)
        self.assertEqual(actual_exit, exit_code)


class TestIncidentReplay(_Base):
    def test_T1_update_after_submit_fails_returns_warning_and_never_resubmits(self):
        """Real incident: Suno accepted+charged, then writing the local record
        raised PermissionError. Must NOT surface as a retryable error."""
        self.suno_ok("task-incident")
        real_save = self.tf._save_task_store

        def flaky_save(store):
            # the write-ahead save must work; the post-submit update must fail
            if any(r.get("status") == "submitted" for r in store.values()):
                raise PermissionError("read-only home")
            return real_save(store)

        self.tf._save_task_store = flaky_save

        payload, code = self.music("crid-incident", model="suno")
        self.assertEqual(code, 0, msg=payload)
        self.assertNotIn("error", payload)
        self.assertEqual(payload["task_id"], "task-incident")
        self.assertIn("warning", payload)
        self.assertIn("请勿重试", payload["warning"])
        self.assertEqual(self.n(SUNO_SUBMIT), 1)

        # The AI retries with the same client_request_id: must NOT submit again.
        payload2, code2 = self.music("crid-incident", model="suno")
        self.assertErrorPayload(payload2, "outcome_unknown", 3, code2, retryable=False)
        self.assertEqual(self.n(SUNO_SUBMIT), 1)

    def test_T1b_update_after_submit_fails_in_auto_mode_too(self):
        self.suno_ok("task-incident-auto")
        real_save = self.tf._save_task_store

        def flaky_save(store):
            if any(r.get("status") == "submitted" for r in store.values()):
                raise PermissionError("read-only home")
            return real_save(store)

        self.tf._save_task_store = flaky_save
        payload, code = self.music("crid-incident-auto")  # model defaults to auto
        self.assertEqual(code, 0, msg=payload)
        self.assertIn("warning", payload)
        self.assertEqual(self.n(SUNO_SUBMIT), 1)
        self.assertEqual(self.n(MM_SUBMIT), 0)


class TestStateDir(_Base):
    def test_T2_no_writable_dir_anywhere_nothing_is_submitted(self):
        self.suno_ok()
        self.stub_unwritable(lambda p: True)

        payload, code = self.music("crid-nodir", model="suno")
        self.assertErrorPayload(payload, "local_state_unwritable", 3, code, retryable=False)
        self.assertIn("TOKENFISH_HOME", payload["error"]["message"])
        self.assertIn("未扣费", payload["error"]["message"])
        self.assertEqual(self.n(SUNO_SUBMIT), 0)
        self.assertEqual(self.n(MM_SUBMIT), 0)

    def test_T2b_images_no_writable_dir_anywhere_nothing_is_submitted(self):
        self.backend.set_response(*IMG_SUBMIT, 200, {"data": [{"url": "https://x/a.png"}]})
        self.stub_unwritable(lambda p: True)

        payload, code = self.images("crid-nodir-img")
        self.assertErrorPayload(payload, "local_state_unwritable", 3, code, retryable=False)
        self.assertEqual(self.n(IMG_SUBMIT), 0)

    def test_T3_home_and_tmp_unwritable_falls_back_to_cwd_dotdir(self):
        self.suno_ok("task-cwd")
        self.stub_home_and_tmp_unwritable()

        payload, code = self.music("crid-cwd", model="suno")
        self.assertEqual(code, 0, msg=payload)
        self.assertNotIn("warning", payload)
        tasks_file = Path.cwd() / ".tokenfish" / "tasks.json"
        self.assertTrue(tasks_file.is_file(), msg="record must land in <cwd>/.tokenfish/tasks.json")
        record = json.loads(tasks_file.read_text(encoding="utf-8"))["crid-cwd"]
        self.assertEqual(record["status"], "submitted")
        self.assertEqual(record["task_id"], "task-cwd")
        self.assertEqual(record["provider"], "suno")
        self.assertTrue(Path(self.tf.state_dir()).samefile(Path.cwd() / ".tokenfish"))
        # nothing was written under the (pretend read-only) home
        self.assertFalse((self.home / ".tokenfish" / "tasks.json").exists())

    def test_T3b_home_unwritable_uses_tmp_user_dir_before_cwd(self):
        self.suno_ok("task-tmp")
        home_dir = self.tf.HOME_STATE_DIR
        self.stub_unwritable(lambda p: p == home_dir)

        payload, code = self.music("crid-tmp", model="suno")
        self.assertEqual(code, 0, msg=payload)
        tasks_file = self.tmp_state / "tasks.json"
        self.assertTrue(tasks_file.is_file(), msg="record must land in <tmp>/tokenfish-<user>/tasks.json")
        self.assertEqual(json.loads(tasks_file.read_text(encoding="utf-8"))["crid-tmp"]["status"], "submitted")
        self.assertFalse((Path.cwd() / ".tokenfish").exists(), msg="cwd must not be used while tmp is writable")

    def test_state_dir_order_env_then_home_then_tmp_then_cwd_then_none(self):
        env_dir = self.cwd / "custom-home"
        with mock.patch.dict(os.environ, {"TOKENFISH_HOME": str(env_dir)}):
            self.tf.reset_state_dir_cache()
            self.assertEqual(self.tf.state_dir(), env_dir)
        self.tf.reset_state_dir_cache()
        self.assertEqual(self.tf.state_dir(), self.tf.HOME_STATE_DIR)

        home_dir = self.tf.HOME_STATE_DIR
        tmp_dir = self.tf._tmp_state_dir()
        cwd_dir = Path.cwd() / ".tokenfish"
        self.assertTrue(tmp_dir.name.startswith("tokenfish-"))
        self.assertEqual(tmp_dir.parent, self.fake_tmp)

        self.stub_unwritable(lambda p: p == home_dir)
        self.assertEqual(self.tf.state_dir(), tmp_dir)
        self.stub_unwritable(lambda p: p in (home_dir, tmp_dir))
        self.assertEqual(self.tf.state_dir(), cwd_dir)
        self.stub_unwritable(lambda p: True)
        self.assertIsNone(self.tf.state_dir())

    def test_login_saves_to_tmp_when_home_unwritable_and_credentials_are_found_there(self):
        shutil.rmtree(self.home / ".tokenfish")  # fresh machine: no credentials anywhere
        home_dir = self.tf.HOME_STATE_DIR
        self.stub_unwritable(lambda p: p == home_dir)
        key_file = self.cwd / "key.txt"
        key_file.write_text("sk-loginfallback0001", encoding="utf-8")

        payload, code = self.tf.cmd_login(str(key_file))
        self.assertEqual(code, 0, msg=payload)
        self.assertTrue(Path(payload["saved_to"]).samefile(self.tmp_state / "credentials.json"))
        self.assertNotIn("sk-loginfallback0001", json.dumps(payload))
        self.assertEqual(self.tf.load_credentials(), "sk-loginfallback0001")

    def test_login_saves_to_cwd_when_home_and_tmp_unwritable(self):
        shutil.rmtree(self.home / ".tokenfish")
        self.stub_home_and_tmp_unwritable()
        key_file = self.cwd / "key.txt"
        key_file.write_text("sk-loginfallback0002", encoding="utf-8")

        payload, code = self.tf.cmd_login(str(key_file))
        self.assertEqual(code, 0, msg=payload)
        self.assertTrue(Path(payload["saved_to"]).samefile(Path.cwd() / ".tokenfish" / "credentials.json"))
        self.assertEqual(self.tf.load_credentials(), "sk-loginfallback0002")

    def test_login_with_no_writable_dir_is_a_clean_error(self):
        key_file = self.cwd / "key.txt"
        key_file.write_text("sk-loginnowhere0001", encoding="utf-8")
        self.stub_unwritable(lambda p: True)
        payload, code = self.tf.cmd_login(str(key_file))
        self.assertEqual(payload["error"]["code"], "local_state_unwritable")
        self.assertEqual(code, 3)

    def _cli_incident_flow(self, env, expected_dir, task_id):
        """login + the same music call from two different cwds, real CLI."""
        key_file = self.cwd / "k.txt"
        key_file.write_text(KEY, encoding="utf-8")
        login = run_cli(["login", "--key-file", str(key_file)], env)
        self.assertEqual(login.returncode, 0, msg=login.stdout + login.stderr)
        saved = Path(json.loads(login.stdout)["saved_to"])
        self.assertTrue(saved.samefile(expected_dir / "credentials.json"))

        self.suno_ok(task_id)
        self.backend.set_response(
            "GET", f"/suno/fetch/{task_id}", 200, {"data": {"status": "IN_PROGRESS", "data": []}}
        )
        args = {"prompt": "city pop", "model": "suno", "client_request_id": "crid-real-fs"}
        first = run_cli(["call", MUSIC_TOOL], env, input_text=json.dumps(args))
        self.assertEqual(first.returncode, 0, msg=first.stdout + first.stderr)
        self.assertEqual(json.loads(first.stdout)["task_id"], task_id)
        self.assertNotIn("warning", json.loads(first.stdout))
        self.assertTrue((expected_dir / "tasks.json").is_file())
        return login, first, args

    def test_real_unwritable_home_cli_flow_no_mocks_and_cwd_may_change(self):
        """Real CLI, HOME is a regular file (cannot be created) -> state lives in
        <tmp>/tokenfish-<user>. The AI tool then changes cwd: still no resubmit."""
        bad_home = self.cwd / "home-is-a-file"
        bad_home.write_text("not a directory", encoding="utf-8")
        env = base_env(bad_home, api_base=self.base_url)

        login, first, args = self._cli_incident_flow(env, self.tmp_state, "task-real")
        self.new_cwd("elsewhere")  # different cwd for the "retry"
        second = run_cli(["call", MUSIC_TOOL], env, input_text=json.dumps(args))
        self.assertEqual(second.returncode, 0, msg=second.stdout + second.stderr)
        self.assertEqual(json.loads(second.stdout)["task_id"], "task-real")
        self.assertEqual(self.n(SUNO_SUBMIT), 1)
        everything = first.stdout + first.stderr + second.stdout + second.stderr + login.stdout + login.stderr
        self.assertNotIn(KEY, everything)

    def test_real_cwd_fallback_cli_flow_when_home_and_tmp_are_both_unusable(self):
        """tmp candidate made unusable for real (a regular file sits where the
        dir would be): the cwd is the last resort."""
        bad_home = self.cwd / "home-is-a-file"
        bad_home.write_text("not a directory", encoding="utf-8")
        self.tmp_state.write_text("not a directory either", encoding="utf-8")
        env = base_env(bad_home, api_base=self.base_url)

        login, first, args = self._cli_incident_flow(env, self.cwd / ".tokenfish", "task-real-cwd")
        second = run_cli(["call", MUSIC_TOOL], env, input_text=json.dumps(args))
        self.assertEqual(second.returncode, 0, msg=second.stdout + second.stderr)
        self.assertEqual(self.n(SUNO_SUBMIT), 1)

    def test_whoami_exposes_state_dir(self):
        self.backend.set_response(
            "GET",
            "/api/usage/token/",
            200,
            {"code": True, "data": {"total_available": 500000}},
        )
        payload, code = self.tf.cmd_whoami()
        self.assertEqual(code, 0, msg=payload)
        self.assertEqual(payload["state_dir"], str(self.tf.state_dir()))
        self.assertNotIn(KEY, json.dumps(payload))


class TestSubmitClassification(_Base):
    def test_T4_suno_response_lost_is_outcome_unknown_and_never_falls_back(self):
        self.backend.set_drop(*SUNO_SUBMIT)
        self.minimax_ok()

        payload, code = self.music("crid-lost")  # auto mode
        self.assertErrorPayload(payload, "outcome_unknown", 3, code, retryable=False)
        self.assertIn("可能已经提交并已扣费", payload["error"]["message"])
        self.assertEqual(self.n(SUNO_SUBMIT), 1)
        self.assertEqual(self.n(MM_SUBMIT), 0)
        record = self.store()["crid-lost"]
        self.assertEqual(record["status"], "unknown")
        self.assertEqual(record["provider"], "suno")

        # retry with the same id: still no resubmit, still no fallback
        payload2, code2 = self.music("crid-lost")
        self.assertErrorPayload(payload2, "outcome_unknown", 3, code2, retryable=False)
        self.assertEqual(self.n(SUNO_SUBMIT), 1)
        self.assertEqual(self.n(MM_SUBMIT), 0)

    def test_T4b_suno_timeout_is_outcome_unknown_and_never_falls_back(self):
        self.backend.set_slow(*SUNO_SUBMIT, 1.5, 200, {"code": "success", "data": "late-task"})
        self.minimax_ok()
        self.tf.DEFAULT_TIMEOUT = 0.3

        payload, code = self.music("crid-timeout")  # auto mode
        self.assertErrorPayload(payload, "outcome_unknown", 3, code, retryable=False)
        self.assertEqual(self.n(SUNO_SUBMIT), 1)
        self.assertEqual(self.n(MM_SUBMIT), 0)

        payload2, code2 = self.music("crid-timeout")
        self.assertErrorPayload(payload2, "outcome_unknown", 3, code2, retryable=False)
        self.assertEqual(self.n(SUNO_SUBMIT), 1)
        self.assertEqual(self.n(MM_SUBMIT), 0)

    def test_unknown_record_with_different_args_is_idempotency_conflict(self):
        self.backend.set_drop(*SUNO_SUBMIT)
        self.music("crid-conflict", model="suno")
        payload, code = self.music("crid-conflict", model="suno", prompt="heavy metal")
        self.assertEqual(payload["error"]["code"], "idempotency_conflict")
        self.assertEqual(self.n(SUNO_SUBMIT), 1)

    def test_T5_suno_503_in_auto_mode_falls_back_to_minimax(self):
        self.backend.set_response(*SUNO_SUBMIT, 503, {"error": {"message": "down"}})
        self.minimax_ok("mm-fallback")

        payload, code = self.music("crid-fallback")
        self.assertEqual(code, 0, msg=payload)
        self.assertEqual(payload["provider"], "minimax")
        self.assertEqual(payload["task_id"], "mm-fallback")
        self.assertEqual(self.n(SUNO_SUBMIT), 1)
        self.assertEqual(self.n(MM_SUBMIT), 1)
        record = self.store()["crid-fallback"]
        self.assertEqual(record["provider"], "minimax")
        self.assertEqual(record["status"], "submitted")

    def test_suno_402_in_auto_mode_does_not_fall_back_and_clears_record(self):
        self.backend.set_response(
            *SUNO_SUBMIT, 402, {"error": {"type": "insufficient_quota", "message": "no quota"}}
        )
        self.minimax_ok()
        payload, code = self.music("crid-402")
        self.assertErrorPayload(payload, "insufficient_balance", 3, code, retryable=False)
        self.assertEqual(self.n(MM_SUBMIT), 0)
        self.assertNotIn("crid-402", self.store())

    def test_both_providers_rejected_is_retryable_and_clears_record(self):
        self.backend.set_response(*SUNO_SUBMIT, 503, {"error": {"message": "down"}})
        self.backend.set_response(*MM_SUBMIT, 503, {"error": {"message": "down"}})
        payload, code = self.music("crid-both-down")
        self.assertErrorPayload(payload, "transient", 4, code, retryable=True)
        self.assertNotIn("crid-both-down", self.store())
        self.assertEqual(self.n(SUNO_SUBMIT), 1)
        self.assertEqual(self.n(MM_SUBMIT), 1)

    def test_T6_suno_2xx_with_non_json_body_is_outcome_unknown(self):
        self.backend.set_raw(*SUNO_SUBMIT, 200, b"<html>gateway says ok</html>")
        self.minimax_ok()

        payload, code = self.music("crid-badbody")  # auto mode
        self.assertErrorPayload(payload, "outcome_unknown", 3, code, retryable=False)
        self.assertEqual(self.n(SUNO_SUBMIT), 1)
        self.assertEqual(self.n(MM_SUBMIT), 0)
        self.assertEqual(self.store()["crid-badbody"]["status"], "unknown")

        payload2, code2 = self.music("crid-badbody")
        self.assertErrorPayload(payload2, "outcome_unknown", 3, code2, retryable=False)
        self.assertEqual(self.n(SUNO_SUBMIT), 1)

    def test_suno_2xx_json_without_task_id_is_outcome_unknown(self):
        self.backend.set_response(*SUNO_SUBMIT, 200, {"code": "success"})
        payload, code = self.music("crid-notask", model="suno")
        self.assertErrorPayload(payload, "outcome_unknown", 3, code, retryable=False)
        self.assertEqual(self.n(SUNO_SUBMIT), 1)

    def test_T7_connection_refused_is_transient_and_record_is_removed(self):
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        closed_port = s.getsockname()[1]
        s.close()
        os.environ["TOKENFISH_API_BASE"] = f"http://127.0.0.1:{closed_port}"

        payload, code = self.music("crid-refused", model="suno")
        self.assertErrorPayload(payload, "transient", 4, code, retryable=True)
        self.assertNotIn("crid-refused", self.store(), msg="a retry must be able to submit")
        self.assertEqual(self.n(SUNO_SUBMIT), 0)

        # network back: the same client_request_id now submits exactly once
        os.environ["TOKENFISH_API_BASE"] = self.base_url
        self.suno_ok("task-after-refused")
        payload2, code2 = self.music("crid-refused", model="suno")
        self.assertEqual(code2, 0, msg=payload2)
        self.assertEqual(payload2["task_id"], "task-after-refused")
        self.assertEqual(self.n(SUNO_SUBMIT), 1)

    def test_connection_refused_in_auto_mode_falls_back_then_stays_retryable(self):
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        closed_port = s.getsockname()[1]
        s.close()
        os.environ["TOKENFISH_API_BASE"] = f"http://127.0.0.1:{closed_port}"
        payload, code = self.music("crid-refused-auto")
        self.assertErrorPayload(payload, "transient", 4, code, retryable=True)
        self.assertNotIn("crid-refused-auto", self.store())

    def test_T8_minimax_maybe_sent_timeout_stays_retryable_with_same_idempotency_key(self):
        calls = {"n": 0}

        def handler(method, path, headers, body):
            calls["n"] += 1
            if calls["n"] == 1:
                time.sleep(1.5)  # longer than the client timeout below
            return 200, {"id": "mm-late", "object": "music", "status": "running"}

        self.backend.set_handler(*MM_SUBMIT, handler)
        self.tf.MINIMAX_TIMEOUT = 0.3

        payload, code = self.music("crid-mm-timeout", model="minimax")
        self.assertErrorPayload(payload, "transient", 4, code, retryable=True)
        record = self.store()["crid-mm-timeout"]
        self.assertEqual(record["provider"], "minimax")
        self.assertEqual(record["status"], "unknown")

        self.tf.MINIMAX_TIMEOUT = 5
        payload2, code2 = self.music("crid-mm-timeout", model="minimax")
        self.assertEqual(code2, 0, msg=payload2)
        self.assertEqual(payload2["provider"], "minimax")
        self.assertEqual(self.n(MM_SUBMIT), 2)
        keys = [r["body"]["idempotency_key"] for r in self.backend.requests if r["path"] == MM_SUBMIT[1]]
        self.assertEqual(len(set(keys)), 1, msg=f"retry must reuse the idempotency_key: {keys}")
        self.assertEqual(keys[0], self.tf.make_idempotency_key("crid-mm-timeout"))
        self.assertEqual(self.store()["crid-mm-timeout"]["status"], "submitted")

    def test_auto_minimax_maybe_sent_retry_goes_straight_to_minimax_not_suno(self):
        """Suno 503 -> MiniMax fallback times out (maybe sent). The retry must
        not try Suno first, or both providers could charge."""
        self.backend.set_response(*SUNO_SUBMIT, 503, {"error": {"message": "down"}})
        self.backend.set_slow(*MM_SUBMIT, 1.5, 200, {"id": "mm-slow", "status": "running"})
        self.tf.MINIMAX_TIMEOUT = 0.3

        payload, code = self.music("crid-auto-mm-timeout")
        self.assertErrorPayload(payload, "transient", 4, code, retryable=True)
        self.assertEqual(self.store()["crid-auto-mm-timeout"]["provider"], "minimax")
        self.assertEqual(self.n(SUNO_SUBMIT), 1)

        self.tf.MINIMAX_TIMEOUT = 5
        self.backend.set_response(*SUNO_SUBMIT, 200, {"code": "success", "data": "should-not-happen"})
        self.minimax_ok("mm-replayed")
        payload2, code2 = self.music("crid-auto-mm-timeout")
        self.assertEqual(code2, 0, msg=payload2)
        self.assertEqual(payload2["provider"], "minimax")
        self.assertEqual(self.n(SUNO_SUBMIT), 1, msg="retry must not go through Suno again")

    def test_legacy_record_without_status_is_treated_as_submitted(self):
        base = self.tf.state_dir()
        (base / "tasks.json").write_text(
            json.dumps(
                {
                    "crid-legacy": {
                        "client_request_id": "crid-legacy",
                        "args_hash": self.tf.args_hash(
                            self.tf.validate_music_args(
                                {"prompt": "city pop", "model": "suno", "client_request_id": "crid-legacy"}
                            )
                        ),
                        "task_id": "task-legacy",
                        "provider": "suno",
                        "created_at": 1.0,
                    }
                }
            ),
            encoding="utf-8",
        )
        self.backend.set_response(
            "GET", "/suno/fetch/task-legacy", 200, {"data": {"status": "IN_PROGRESS", "data": []}}
        )
        payload, code = self.music("crid-legacy", model="suno")
        self.assertEqual(code, 0, msg=payload)
        self.assertEqual(payload["task_id"], "task-legacy")
        self.assertEqual(self.n(SUNO_SUBMIT), 0)

    def test_tasks_list_shows_record_status_of_unknown_submission(self):
        self.backend.set_drop(*SUNO_SUBMIT)
        self.music("crid-listed", model="suno")
        payload, code = self.call("tokenfish.tasks.list", {})
        self.assertEqual(code, 0)
        row = [t for t in payload["tasks"] if t["client_request_id"] == "crid-listed"][0]
        self.assertEqual(row["record_status"], "unknown")


class TestHttpClassification(_Base):
    """http_request() maps failures onto the four classes."""

    def req(self, **kw):
        return self.tf.http_request("POST", "/x", KEY, payload={"a": 1}, **kw)

    def test_refused_is_not_sent(self):
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.close()
        os.environ["TOKENFISH_API_BASE"] = f"http://127.0.0.1:{port}"
        with self.assertRaises(self.tf.ApiTransientError) as cm:
            self.req()
        self.assertFalse(cm.exception.maybe_sent)

    def test_dns_failure_is_not_sent(self):
        err = urllib.error.URLError(socket.gaierror(11001, "getaddrinfo failed"))
        with mock.patch.object(self.tf.urllib.request, "urlopen", side_effect=err):
            with self.assertRaises(self.tf.ApiTransientError) as cm:
                self.req()
        self.assertFalse(cm.exception.maybe_sent)

    NOT_SENT_REASONS = {
        "PermissionError": PermissionError(errno.EACCES, "sandbox blocks connect"),
        "ENETUNREACH": OSError(errno.ENETUNREACH, "network unreachable"),
        "EHOSTUNREACH": OSError(errno.EHOSTUNREACH, "host unreachable"),
        "ECONNREFUSED": OSError(errno.ECONNREFUSED, "connection refused"),
        "SSLCertVerificationError": ssl.SSLCertVerificationError(1, "certificate verify failed"),
    }

    def test_B_not_sent_reasons_inside_urlerror(self):
        for name, reason in self.NOT_SENT_REASONS.items():
            with self.subTest(reason=name):
                err = urllib.error.URLError(reason)
                with mock.patch.object(self.tf.urllib.request, "urlopen", side_effect=err):
                    with self.assertRaises(self.tf.ApiTransientError) as cm:
                        self.req()
                self.assertFalse(cm.exception.maybe_sent)

    def test_maybe_sent_cases(self):
        cases = [
            urllib.error.URLError(TimeoutError("timed out")),
            urllib.error.URLError(ConnectionResetError()),
            urllib.error.URLError(BrokenPipeError()),
            ConnectionResetError(),
            ConnectionAbortedError(),
            TimeoutError(),
            self.tf.http.client.RemoteDisconnected("Remote end closed connection without response"),
            self.tf.http.client.IncompleteRead(b""),
            OSError("some other network error"),
            # only inside URLError.reason do these prove "not sent"; raised bare
            # they could come from anywhere (e.g. mid-exchange) -> maybe sent
            PermissionError(errno.EACCES, "denied"),
            ssl.SSLCertVerificationError(1, "certificate verify failed"),
        ]
        for exc in cases:
            with self.subTest(exc=type(exc).__name__):
                with mock.patch.object(self.tf.urllib.request, "urlopen", side_effect=exc):
                    with self.assertRaises(self.tf.ApiTransientError) as cm:
                        self.req()
                self.assertTrue(cm.exception.maybe_sent)

    def test_remote_disconnect_end_to_end_is_maybe_sent(self):
        self.backend.set_drop("POST", "/x")
        with self.assertRaises(self.tf.ApiTransientError) as cm:
            self.req()
        self.assertTrue(cm.exception.maybe_sent)

    def test_http_error_status_is_api_http_error(self):
        self.backend.set_response("POST", "/x", 503, {"error": "down"})
        with self.assertRaises(self.tf.ApiHttpError) as cm:
            self.req()
        self.assertEqual(cm.exception.status, 503)

    def test_2xx_non_json_is_bad_response(self):
        self.backend.set_raw("POST", "/x", 200, b"not json")
        with self.assertRaises(self.tf.ApiBadResponse):
            self.req()

    def test_2xx_json_non_object_is_bad_response(self):
        self.backend.set_raw("POST", "/x", 200, b"[1, 2]")
        with self.assertRaises(self.tf.ApiBadResponse):
            self.req()

    def test_read_only_query_with_bad_body_stays_plain_transient(self):
        self.backend.set_raw("GET", "/api/usage/token/", 200, b"<html>")
        with self.assertRaises(self.tf.ToolError) as cm:
            self.tf.tool_wallet_get({}, KEY)
        self.assertEqual(cm.exception.code, "transient")


class TestImages(_Base):
    def image_response(self):
        return {
            "data": [{"b64_json": TINY_B64}, {"url": "https://example.invalid/b.png"}],
            "quota_charged": 100000,
        }

    def test_images_saved_to_output_dir_when_writable(self):
        self.backend.set_response(*IMG_SUBMIT, 200, self.image_response())
        payload, code = self.images("crid-img-ok", n=2)
        self.assertEqual(code, 0, msg=payload)
        self.assertNotIn("warning", payload)
        saved = Path(payload["images"][0]["path"])
        self.assertTrue(saved.is_file())
        self.assertEqual(saved.read_bytes(), base64.b64decode(TINY_B64))
        self.assertEqual(saved.parent.name, "tokenfish-outputs")
        self.assertEqual(self.store()["crid-img-ok"]["status"], "submitted")

    def test_T9_output_dir_unwritable_returns_result_with_warning(self):
        self.backend.set_response(*IMG_SUBMIT, 200, self.image_response())
        self.stub_unwritable(lambda p: p.name in ("tokenfish-outputs", "outputs"))

        payload, code = self.images("crid-img-nowrite", n=2)
        self.assertEqual(code, 0, msg=payload)
        self.assertNotIn("error", payload)
        self.assertIn("warning", payload)
        self.assertIn("请勿重试", payload["warning"])
        self.assertIsNone(payload["images"][0]["path"])
        self.assertEqual(payload["images"][1]["url"], "https://example.invalid/b.png")
        self.assertEqual(self.n(IMG_SUBMIT), 1)

        # same id again: the stored result comes back, nothing is resubmitted
        payload2, code2 = self.images("crid-img-nowrite", n=2)
        self.assertEqual(code2, 0, msg=payload2)
        self.assertEqual(self.n(IMG_SUBMIT), 1)

    def test_output_dir_falls_back_to_state_dir_outputs(self):
        self.backend.set_response(*IMG_SUBMIT, 200, self.image_response())
        self.stub_unwritable(lambda p: p.name == "tokenfish-outputs")
        payload, code = self.images("crid-img-fallback", n=2)
        self.assertEqual(code, 0, msg=payload)
        self.assertNotIn("warning", payload)
        saved = Path(payload["images"][0]["path"])
        self.assertTrue(saved.is_file())
        self.assertEqual(saved.parent, self.tf.state_dir() / "outputs")

    def test_images_record_update_fails_after_submit_returns_warning(self):
        self.backend.set_response(*IMG_SUBMIT, 200, self.image_response())
        real_save = self.tf._save_task_store

        def flaky_save(store):
            if any(r.get("status") == "submitted" for r in store.values()):
                raise PermissionError("read-only")
            return real_save(store)

        self.tf._save_task_store = flaky_save
        payload, code = self.images("crid-img-norecord", n=2)
        self.assertEqual(code, 0, msg=payload)
        self.assertIn("warning", payload)
        payload2, code2 = self.images("crid-img-norecord", n=2)
        self.assertErrorPayload(payload2, "outcome_unknown", 3, code2, retryable=False)
        self.assertEqual(self.n(IMG_SUBMIT), 1)

    def test_images_response_lost_is_outcome_unknown_and_not_resubmitted(self):
        self.backend.set_drop(*IMG_SUBMIT)
        payload, code = self.images("crid-img-lost")
        self.assertErrorPayload(payload, "outcome_unknown", 3, code, retryable=False)
        payload2, code2 = self.images("crid-img-lost")
        self.assertErrorPayload(payload2, "outcome_unknown", 3, code2, retryable=False)
        self.assertEqual(self.n(IMG_SUBMIT), 1)

    def test_images_server_error_status_is_retryable_and_clears_record(self):
        self.backend.set_response(*IMG_SUBMIT, 500, {"error": {"message": "boom"}})
        payload, code = self.images("crid-img-500")
        self.assertErrorPayload(payload, "transient", 4, code, retryable=True)
        self.assertNotIn("crid-img-500", self.store())

    def test_images_odd_crid_cannot_escape_output_dir(self):
        self.backend.set_response(*IMG_SUBMIT, 200, self.image_response())
        payload, code = self.images("../evil/id", n=2)
        self.assertEqual(code, 0, msg=payload)
        saved = Path(payload["images"][0]["path"])
        self.assertEqual(saved.parent.name, "tokenfish-outputs")


class TestSafetyNet(_Base):
    def test_T10_unexpected_error_after_paid_submit_is_outcome_unknown(self):
        self.suno_ok("task-boom")

        def boom(*_a, **_k):
            raise RuntimeError("bug after the server accepted the request")

        real = self.tf.build_suno_submit_task_object
        self.tf.build_suno_submit_task_object = boom
        payload, code = self.music("crid-boom", model="suno")
        self.assertErrorPayload(payload, "outcome_unknown", 3, code, retryable=False)
        # only the exception *type* may be surfaced, never its message
        self.assertNotIn("bug after the server accepted", json.dumps(payload, ensure_ascii=False))
        self.assertEqual(self.n(SUNO_SUBMIT), 1)

        # the write-ahead record is still there, so a retry cannot resubmit
        self.tf.build_suno_submit_task_object = real
        payload2, code2 = self.music("crid-boom", model="suno")
        self.assertErrorPayload(payload2, "outcome_unknown", 3, code2, retryable=False)
        self.assertEqual(self.n(SUNO_SUBMIT), 1)

    def test_unexpected_error_in_read_only_tool_stays_transient(self):
        def boom(args, key):
            raise RuntimeError("bug")

        with mock.patch.dict(self.tf.TOOLS_BY_NAME["tokenfish.wallet.get"], {"handler": boom}):
            payload, code = self.call("tokenfish.wallet.get", {})
        self.assertErrorPayload(payload, "transient", 4, code, retryable=True)

    def test_cli_outcome_unknown_exit_code_and_no_key_leak(self):
        """End to end through the real CLI process."""
        self.backend.set_drop(*SUNO_SUBMIT)
        env = base_env(self.home, api_base=self.base_url)
        args = {"prompt": "city pop", "model": "suno", "client_request_id": "crid-cli-lost"}
        proc = run_cli(["call", MUSIC_TOOL], env, input_text=json.dumps(args))
        self.assertEqual(proc.returncode, 3, msg=proc.stdout + proc.stderr)
        out = json.loads(proc.stdout)
        self.assertEqual(out["error"]["code"], "outcome_unknown")
        self.assertFalse(out["error"]["retryable"])
        self.assertNotIn(KEY, proc.stdout + proc.stderr)
        # second process, same id: still no resubmit
        proc2 = run_cli(["call", MUSIC_TOOL], env, input_text=json.dumps(args))
        self.assertEqual(proc2.returncode, 3, msg=proc2.stdout + proc2.stderr)
        self.assertEqual(self.n(SUNO_SUBMIT), 1)


class TestNotSentEndToEnd(_Base):
    def test_B_not_sent_reasons_are_transient_retryable_and_record_removed(self):
        for name, reason in TestHttpClassification.NOT_SENT_REASONS.items():
            with self.subTest(reason=name):
                crid = f"crid-notsent-{name}"
                err = urllib.error.URLError(reason)
                with mock.patch.object(self.tf.urllib.request, "urlopen", side_effect=err):
                    payload, code = self.music(crid, model="suno")
                self.assertErrorPayload(payload, "transient", 4, code, retryable=True)
                self.assertNotIn(crid, self.store(), msg="a retry must be able to submit")
        self.assertEqual(self.n(SUNO_SUBMIT), 0)

        # and the very same client_request_id then submits exactly once
        self.suno_ok("task-after-notsent")
        payload, code = self.music("crid-notsent-PermissionError", model="suno")
        self.assertEqual(code, 0, msg=payload)
        self.assertEqual(self.n(SUNO_SUBMIT), 1)

    def test_B_bare_permission_error_is_still_maybe_sent(self):
        with mock.patch.object(
            self.tf.urllib.request, "urlopen", side_effect=PermissionError(errno.EACCES, "denied")
        ):
            payload, code = self.music("crid-bare-perm", model="suno")
        self.assertErrorPayload(payload, "outcome_unknown", 3, code, retryable=False)
        self.assertEqual(self.store()["crid-bare-perm"]["status"], "unknown")

    def test_B_not_sent_suno_in_auto_mode_falls_back_to_minimax(self):
        self.minimax_ok("mm-after-notsent")
        real = self.tf.urllib.request.urlopen
        err = urllib.error.URLError(PermissionError(errno.EACCES, "sandbox blocks connect"))

        def selective(request, timeout=None):
            if request.full_url.endswith(SUNO_SUBMIT[1]):
                raise err
            return real(request, timeout=timeout)

        with mock.patch.object(self.tf.urllib.request, "urlopen", side_effect=selective):
            payload, code = self.music("crid-auto-notsent")
        self.assertEqual(code, 0, msg=payload)
        self.assertEqual(payload["provider"], "minimax")
        self.assertEqual(self.n(SUNO_SUBMIT), 0)
        self.assertEqual(self.n(MM_SUBMIT), 1)


class TestMinimaxBadBody(_Base):
    def test_C_minimax_2xx_bad_body_is_retryable_and_retry_reuses_the_idempotency_key(self):
        self.backend.set_raw(*MM_SUBMIT, 200, b"<html>gateway ok</html>")
        payload, code = self.music("crid-mm-bad", model="minimax")
        self.assertErrorPayload(payload, "transient", 4, code, retryable=True)
        record = self.store()["crid-mm-bad"]
        self.assertEqual(record["provider"], "minimax")
        self.assertEqual(record["status"], "unknown")

        self.minimax_ok("mm-ok")
        payload2, code2 = self.music("crid-mm-bad", model="minimax")
        self.assertEqual(code2, 0, msg=payload2)
        self.assertEqual(payload2["provider"], "minimax")
        self.assertEqual(self.n(MM_SUBMIT), 2)
        keys = [r["body"]["idempotency_key"] for r in self.backend.requests if r["path"] == MM_SUBMIT[1]]
        self.assertEqual(len(set(keys)), 1, msg=f"retry must reuse the idempotency_key: {keys}")
        self.assertEqual(self.store()["crid-mm-bad"]["status"], "submitted")

    def test_C_minimax_2xx_json_without_id_is_retryable(self):
        self.backend.set_response(*MM_SUBMIT, 200, {"status": "running"})
        payload, code = self.music("crid-mm-noid", model="minimax")
        self.assertErrorPayload(payload, "transient", 4, code, retryable=True)
        self.assertEqual(self.store()["crid-mm-noid"]["status"], "unknown")

    def test_C_auto_suno_503_then_minimax_bad_body_retry_skips_suno(self):
        self.backend.set_response(*SUNO_SUBMIT, 503, {"error": {"message": "down"}})
        self.backend.set_raw(*MM_SUBMIT, 200, b"not json")
        payload, code = self.music("crid-auto-mm-bad")
        self.assertErrorPayload(payload, "transient", 4, code, retryable=True)
        self.assertEqual(self.store()["crid-auto-mm-bad"]["provider"], "minimax")

        self.backend.set_response(*SUNO_SUBMIT, 200, {"code": "success", "data": "should-not-happen"})
        self.minimax_ok("mm-replayed")
        payload2, code2 = self.music("crid-auto-mm-bad")
        self.assertEqual(code2, 0, msg=payload2)
        self.assertEqual(payload2["provider"], "minimax")
        self.assertEqual(self.n(SUNO_SUBMIT), 1, msg="retry must not go through Suno again")
        self.assertEqual(self.n(MM_SUBMIT), 2)


class TestUnionLookup(_Base):
    """Record lookup reads tasks.json from EVERY candidate dir (read-only)."""

    def write_store(self, directory, records):
        directory.mkdir(parents=True, exist_ok=True)
        (directory / "tasks.json").write_text(json.dumps(records), encoding="utf-8")

    def suno_record(self, crid, **fields):
        clean = self.tf.validate_music_args({"prompt": "city pop", "model": "suno", "client_request_id": crid})
        record = {"client_request_id": crid, "args_hash": self.tf.args_hash(clean), "provider": "suno"}
        record.update(fields)
        return record

    def test_A_record_written_under_cwdA_is_found_from_cwdB(self):
        """Home unwritable -> state goes to <tmp>/tokenfish-<user>, which does not
        depend on cwd. The AI tool then changes directory: no second submit."""
        home_dir = self.tf.HOME_STATE_DIR
        self.stub_unwritable(lambda p: p == home_dir)
        self.suno_ok("task-ab")
        self.backend.set_response(
            "GET", "/suno/fetch/task-ab", 200, {"data": {"status": "IN_PROGRESS", "data": []}}
        )

        self.new_cwd("cwdA")
        payload, code = self.music("crid-ab", model="suno")
        self.assertEqual(code, 0, msg=payload)
        self.assertEqual(self.n(SUNO_SUBMIT), 1)

        cwd_b = self.new_cwd("cwdB")
        self.tf.reset_state_dir_cache()  # a new process starts with an empty cache
        payload2, code2 = self.music("crid-ab", model="suno")
        self.assertEqual(code2, 0, msg=payload2)
        self.assertEqual(payload2["task_id"], "task-ab")
        self.assertEqual(self.n(SUNO_SUBMIT), 1)
        self.assertFalse((cwd_b / ".tokenfish").exists())

    def test_A_unknown_outcome_is_also_found_from_another_cwd(self):
        home_dir = self.tf.HOME_STATE_DIR
        self.stub_unwritable(lambda p: p == home_dir)
        self.backend.set_drop(*SUNO_SUBMIT)
        self.new_cwd("cwdA")
        payload, code = self.music("crid-ab-lost", model="suno")
        self.assertErrorPayload(payload, "outcome_unknown", 3, code, retryable=False)

        self.new_cwd("cwdB")
        self.tf.reset_state_dir_cache()
        payload2, code2 = self.music("crid-ab-lost", model="suno")
        self.assertErrorPayload(payload2, "outcome_unknown", 3, code2, retryable=False)
        self.assertEqual(self.n(SUNO_SUBMIT), 1)

    def test_A_record_left_in_home_is_still_seen_after_state_dir_moves_to_tmp(self):
        self.suno_ok("task-moved")
        self.backend.set_response(
            "GET", "/suno/fetch/task-moved", 200, {"data": {"status": "IN_PROGRESS", "data": []}}
        )
        payload, code = self.music("crid-moved", model="suno")
        self.assertEqual(code, 0, msg=payload)
        self.assertTrue((self.tf.HOME_STATE_DIR / "tasks.json").is_file())

        home_dir = self.tf.HOME_STATE_DIR
        self.stub_unwritable(lambda p: p == home_dir)  # home turns read-only
        self.assertEqual(self.tf.state_dir(), self.tmp_state)
        payload2, code2 = self.music("crid-moved", model="suno")
        self.assertEqual(code2, 0, msg=payload2)
        self.assertEqual(payload2["task_id"], "task-moved")
        self.assertEqual(self.n(SUNO_SUBMIT), 1)

    def test_A_record_in_cwd_dotdir_is_seen_when_state_dir_is_elsewhere(self):
        """A leftover from an earlier cwd-fallback session in the current cwd."""
        self.write_store(
            Path.cwd() / ".tokenfish",
            {"crid-left": self.suno_record("crid-left", status="submitting", created_at=1.0)},
        )
        self.suno_ok("must-not-be-used")
        payload, code = self.music("crid-left", model="suno")
        self.assertErrorPayload(payload, "outcome_unknown", 3, code, retryable=False)
        self.assertEqual(self.n(SUNO_SUBMIT), 0)

    def test_A_union_prefers_submitted_over_unknown_then_newest(self):
        home = self.tf.HOME_STATE_DIR
        tmp = self.tmp_state
        cwd = Path.cwd() / ".tokenfish"
        self.write_store(
            home,
            {
                "X": self.suno_record("X", status="unknown", created_at=100.0),
                "Y": self.suno_record("Y", status="submitted", task_id="y-old", created_at=1.0),
                "Z": self.suno_record("Z", status="unknown", created_at=1.0),
            },
        )
        self.write_store(
            tmp,
            {
                "X": self.suno_record("X", status="submitted", task_id="x-sub", created_at=1.0),
                "Y": self.suno_record("Y", status="submitted", task_id="y-new", created_at=5.0),
            },
        )
        self.write_store(cwd, {"Z": self.suno_record("Z", status="submitting", created_at=9.0)})

        self.assertEqual(self.tf.find_record_by_client_request_id("X")["task_id"], "x-sub")
        self.assertEqual(self.tf.find_record_by_client_request_id("Y")["task_id"], "y-new")
        self.assertEqual(self.tf.find_record_by_client_request_id("Z")["status"], "submitting")
        self.assertEqual(self.tf.find_record_by_task_id("x-sub")["client_request_id"], "X")
        listing = self.tf.list_local_tasks()
        self.assertEqual(sorted(r["client_request_id"] for r in listing), ["X", "Y", "Z"])

    def test_A_tasks_list_is_the_union_of_all_dirs(self):
        self.write_store(self.tf.HOME_STATE_DIR, {"a": self.suno_record("a", status="submitted", task_id="ta", created_at=1.0)})
        self.write_store(self.tmp_state, {"b": self.suno_record("b", status="submitted", task_id="tb", created_at=2.0)})
        payload, code = self.call("tokenfish.tasks.list", {})
        self.assertEqual(code, 0)
        self.assertEqual([t["client_request_id"] for t in payload["tasks"]], ["b", "a"])

    def test_A_reading_creates_no_directories(self):
        fresh = self.new_cwd("fresh")
        self.assertEqual(self.tf.tool_tasks_list({}, KEY), {"tasks": []})
        self.assertIsNone(self.tf.find_record_by_client_request_id("nope"))
        self.assertIsNone(self.tf.find_record_by_task_id("nope"))
        self.assertFalse((fresh / ".tokenfish").exists())
        self.assertFalse(self.tmp_state.exists())

    def test_A_corrupt_tasks_json_in_one_dir_does_not_hide_the_others(self):
        cwd = Path.cwd() / ".tokenfish"
        cwd.mkdir()
        (cwd / "tasks.json").write_text("{ not json", encoding="utf-8")
        self.write_store(
            self.tf.HOME_STATE_DIR, {"ok": self.suno_record("ok", status="submitted", task_id="t-ok", created_at=1.0)}
        )
        self.assertEqual(self.tf.find_record_by_client_request_id("ok")["task_id"], "t-ok")


class TestTasksGetProvider(_Base):
    def suno_done(self, task_id):
        self.backend.set_response(
            "GET",
            f"/suno/fetch/{task_id}",
            200,
            {"data": {"status": "SUCCESS", "data": [{"audio_url": "https://x/a.mp3", "title": "A", "duration": 100}]}},
        )

    def get_count(self, path):
        return self.backend.count("GET", path)

    def test_D_provider_suno_fetches_directly_without_local_record(self):
        self.suno_done("t-no-record")
        payload, code = self.call("tokenfish.tasks.get", {"task_id": "t-no-record", "provider": "suno"})
        self.assertEqual(code, 0, msg=payload)
        self.assertEqual(payload["status"], "succeeded")
        self.assertEqual(payload["provider"], "suno")
        self.assertEqual(payload["task_id"], "t-no-record")
        self.assertEqual(payload["clips"][0]["audio_url"], "https://x/a.mp3")
        self.assertIsNone(payload["client_request_id"])
        self.assertEqual(self.get_count("/suno/fetch/t-no-record"), 1)

    def test_D_provider_minimax_fetches_directly_without_local_record(self):
        self.backend.set_response(
            "GET",
            "/v1/music/generations/mm-no-record",
            200,
            {"id": "mm-no-record", "status": "succeeded", "output_url": "https://x/mm.mp3", "quota_charged": 500000},
        )
        payload, code = self.call("tokenfish.tasks.get", {"task_id": "mm-no-record", "provider": "minimax"})
        self.assertEqual(code, 0, msg=payload)
        self.assertEqual(payload["provider"], "minimax")
        self.assertEqual(payload["status"], "succeeded")
        self.assertEqual(payload["charged_cny"], 1.0)

    def test_D_unknown_task_without_provider_still_errors_and_hints_provider(self):
        payload, code = self.call("tokenfish.tasks.get", {"task_id": "who-knows"})
        self.assertErrorPayload(payload, "invalid_arguments", 2, code, retryable=False)
        self.assertIn("provider", payload["error"]["next_action"])
        self.assertEqual(len(self.backend.requests), 0)

    def test_D_invalid_provider_is_rejected_before_any_request(self):
        for bad in ("gpt", "image", "", 5, ["suno"]):
            with self.subTest(provider=bad):
                payload, code = self.call("tokenfish.tasks.get", {"task_id": "x", "provider": bad})
                self.assertErrorPayload(payload, "invalid_arguments", 2, code, retryable=False)
                self.assertIn("provider", payload["error"]["message"])
        self.assertEqual(len(self.backend.requests), 0)

    def test_D_local_record_wins_over_the_provider_argument(self):
        self.suno_ok("task-local")
        self.music("crid-local", model="suno")
        self.suno_done("task-local")
        payload, code = self.call("tokenfish.tasks.get", {"task_id": "task-local", "provider": "minimax"})
        self.assertEqual(code, 0, msg=payload)
        self.assertEqual(payload["provider"], "suno")
        self.assertEqual(payload["client_request_id"], "crid-local")

    def test_T1c_incident_followup_fetch_with_task_id_and_provider_from_the_warning_result(self):
        """After the incident the local record has no task_id; the result the AI
        already holds (task_id + provider) is enough to fetch the song."""
        self.suno_ok("task-incident2")
        real_save = self.tf._save_task_store

        def flaky_save(store):
            if any(r.get("status") == "submitted" for r in store.values()):
                raise PermissionError("read-only home")
            return real_save(store)

        self.tf._save_task_store = flaky_save
        first, code = self.music("crid-incident2", model="suno")
        self.assertEqual(code, 0, msg=first)
        self.assertIn("warning", first)
        self.assertEqual(self.n(SUNO_SUBMIT), 1)

        self.suno_done("task-incident2")
        got, code2 = self.call(
            "tokenfish.tasks.get", {"task_id": first["task_id"], "provider": first["provider"]}
        )
        self.assertEqual(code2, 0, msg=got)
        self.assertEqual(got["status"], "succeeded")
        self.assertEqual(self.n(SUNO_SUBMIT), 1)

    def test_D_warning_and_next_action_mention_tasks_get_only_where_task_id_is_known(self):
        # music: task_id known -> warning points at tasks.get with task_id + provider
        self.suno_ok("task-known")
        real_save = self.tf._save_task_store

        def flaky_save(store):
            if any(r.get("status") == "submitted" for r in store.values()):
                raise PermissionError("read-only")
            return real_save(store)

        self.tf._save_task_store = flaky_save
        music, code = self.music("crid-warn-music", model="suno")
        self.assertIn("tokenfish.tasks.get", music["warning"])
        self.assertIn("provider", music["warning"])

        # images: result is already in the response -> no tasks.get pointer
        self.backend.set_response(*IMG_SUBMIT, 200, {"data": [{"url": "https://x/a.png"}]})
        images, code = self.images("crid-warn-img")
        self.assertIn("warning", images)
        self.assertNotIn("tasks.get", images["warning"])

        # outcome_unknown: no task_id is known -> must not suggest provider/tasks.get
        self.backend.set_drop(*SUNO_SUBMIT)
        unknown, code = self.music("crid-warn-unknown", model="suno", prompt="y")
        self.assertEqual(unknown["error"]["code"], "outcome_unknown")
        self.assertNotIn("tasks.get", unknown["error"]["next_action"])
        self.assertNotIn("provider", unknown["error"]["next_action"])

    def test_D_tools_schema_lists_provider_for_tasks_get(self):
        payload, code = self.tf.cmd_tools()
        schema = [t for t in payload if t["name"] == "tokenfish.tasks.get"][0]["parameters"]
        self.assertEqual(schema["properties"]["provider"]["enum"], ["suno", "minimax", "video"])
        self.assertEqual(schema["required"], ["task_id"])


if __name__ == "__main__":
    unittest.main()
