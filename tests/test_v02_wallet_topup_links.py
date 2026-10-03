"""v0.2: open a wallet in the conversation, top up in the conversation, links on results.

Fake backend only (tests/fake_server.py): nothing here ever talks to a real API.

S   `setup` (open a new empty wallet, POST /device/bind)
T   `tokenfish.wallet.topup` (POST /recharge/create)
U   `tokenfish.wallet.topup_status` (GET /recharge/status)
L   delivery links printed by the client on successful paid results
P   static pages / versions

Invariants asserted throughout: the API key never appears in stdout / stderr /
any generated link, and a failing extra (balance lookup, link building) never
turns a final result into an error.
"""

import base64
import contextlib
import io
import json
import os
import re
import shutil
import socket
import sys
import tempfile
import unittest
import urllib.parse
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent))
from fake_server import FakeBackend  # noqa: E402
from testutil import REPO_ROOT, SYSTEM_TMP, base_env, import_client_module, make_isolated_home, run_cli  # noqa: E402

BIND = ("POST", "/device/bind")
RECHARGE_CREATE = ("POST", "/recharge/create")
WALLET_GET = ("GET", "/api/usage/token/")
MM_SUBMIT = ("POST", "/v1/music/generations")
SUNO_SUBMIT = ("POST", "/suno/submit/music")
IMG_SUBMIT = ("POST", "/v1/images/generations")

NEW_KEY = "sk-NEWWALLETKEYSECRET0001"
NEW_KEY_FRAG = "NEWWALLETKEYSECRET"
KEY = "sk-V02TOPUPSECRETKEY777"
KEY_FRAG = "V02TOPUPSECRETKEY"
SITE = "https://tokenfish.u-claw.org.cn"
OFFICIAL = "https://www.u-claw.org.cn"
TINY_B64 = base64.b64encode(b"\x89PNG\r\n\x1a\nfake-image-bytes").decode("ascii")


def status_route(order_id):
    return ("GET", "/recharge/status?order_id=" + urllib.parse.quote(order_id, safe=""))


def fragment_params(link):
    return urllib.parse.parse_qs(urllib.parse.urlsplit(link).fragment, keep_blank_values=True)


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
        self.tf = import_client_module()

    # -- helpers ----------------------------------------------------------

    def n(self, route):
        return self.backend.count(*route)

    def requests_for(self, route):
        return [r for r in self.backend.requests if (r["method"], r["path"]) == route]

    def write_credentials(self, key=KEY):
        d = self.home / ".tokenfish"
        d.mkdir(parents=True, exist_ok=True)
        (d / "credentials.json").write_text(json.dumps({"key": key}), encoding="utf-8")

    def creds_file(self):
        return self.home / ".tokenfish" / "credentials.json"

    def env(self):
        return base_env(self.home, api_base=self.base_url)

    def call(self, tool, args):
        return self.tf.cmd_call(tool, json.dumps(args))

    def wallet_ok(self, total_available=4000000):
        self.backend.set_response(
            *WALLET_GET, 200, {"code": True, "data": {"name": "u", "total_available": total_available}}
        )

    def assertErrorPayload(self, payload, code, exit_code, actual_exit, retryable):
        self.assertIn("error", payload, msg=f"expected an error payload, got {payload!r}")
        self.assertEqual(payload["error"]["code"], code)
        self.assertEqual(payload["error"]["retryable"], retryable)
        self.assertEqual(actual_exit, exit_code)

    def assertNoSecret(self, obj, *secrets):
        text = obj if isinstance(obj, str) else json.dumps(obj, ensure_ascii=False)
        for secret in secrets:
            self.assertNotIn(secret, text)

    def stub_unwritable(self, predicate):
        real = self.tf._dir_is_writable

        def stub(path):
            return False if predicate(Path(path)) else real(path)

        self.tf._dir_is_writable = stub
        self.tf.reset_state_dir_cache()

    def free_port_url(self):
        s = socket.socket()
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]
        s.close()
        return f"http://127.0.0.1:{port}"


# ======================================================================
# S: setup
# ======================================================================


class TestSetup(_Base):
    def bind_ok(self, wallet_id="w_abc123", api_key=NEW_KEY, balance=1):
        self.backend.set_response(*BIND, 200, {"walletId": wallet_id, "apiKey": api_key, "balanceTokens": balance})

    def test_S1_creates_a_wallet_once_then_reports_already_configured(self):
        self.bind_ok()
        payload, code = self.tf.cmd_setup()
        self.assertEqual(code, 0, msg=payload)
        self.assertEqual(payload["status"], "created")
        self.assertEqual(payload["wallet_id"], "w_abc123")
        self.assertEqual(payload["key_masked"], self.tf.mask_key(NEW_KEY))
        self.assertEqual(payload["balance_cny"], 0.0)
        saved = Path(payload["saved_to"])
        self.assertTrue(saved.samefile(self.creds_file()))
        self.assertEqual(
            sorted(payload),
            ["backup_notice", "balance_cny", "key_masked", "next_step", "saved_to", "status", "wallet_id"],
        )
        self.assertNoSecret(payload, NEW_KEY, NEW_KEY_FRAG)
        self.assertEqual(self.n(BIND), 1)

        # the saved key is what every later call will use
        self.assertEqual(self.tf.load_credentials(), NEW_KEY)

        # second run: credentials exist -> no new wallet
        payload2, code2 = self.tf.cmd_setup()
        self.assertEqual(code2, 0)
        self.assertEqual(payload2["status"], "already_configured")
        self.assertEqual(payload2["key_masked"], self.tf.mask_key(NEW_KEY))
        self.assertEqual(sorted(payload2), ["key_masked", "state_dir", "status"])
        self.assertEqual(self.n(BIND), 1)

    def test_S2_bind_request_shape_has_no_fingerprint_and_no_auth(self):
        self.bind_ok()
        self.tf.cmd_setup()
        (req,) = self.requests_for(BIND)
        slug = self.tf.find_package_slug(Path(self.tf.__file__).resolve())
        self.assertEqual(req["body"], {"platform": self.tf.detect_platform(), "channel": f"tokenfish-{slug}"})
        self.assertNotIn("hwHint", req["body"])
        self.assertNotIn("authorization", {k.lower() for k in req["headers"]})

    def test_S3_cli_stdout_never_contains_the_new_key_and_second_run_does_not_bind(self):
        self.bind_ok()
        proc = run_cli(["setup"], self.env())
        self.assertEqual(proc.returncode, 0, msg=proc.stderr)
        self.assertNoSecret(proc.stdout + proc.stderr, NEW_KEY, NEW_KEY_FRAG)
        out = json.loads(proc.stdout)
        self.assertEqual(out["status"], "created")
        self.assertEqual(json.loads(self.creds_file().read_text(encoding="utf-8"))["key"], NEW_KEY)

        proc2 = run_cli(["setup"], self.env())
        self.assertEqual(proc2.returncode, 0, msg=proc2.stderr)
        self.assertEqual(json.loads(proc2.stdout)["status"], "already_configured")
        self.assertNoSecret(proc2.stdout + proc2.stderr, NEW_KEY, NEW_KEY_FRAG)
        self.assertEqual(self.n(BIND), 1)

    def test_S4_backup_notice_says_key_is_the_wallet_and_where_it_is(self):
        self.bind_ok()
        payload, _ = self.tf.cmd_setup()
        notice = payload["backup_notice"]
        self.assertIn(payload["saved_to"], notice)
        self.assertIn("就是你的钱包", notice)
        self.assertIn("备份", notice)
        self.assertIn("TOKENFISH_HOME", notice)
        self.assertIn("login --key-file", notice)
        self.assertNotIn("临时位置", notice)  # saved under ~/.tokenfish: the normal case

    def test_S4b_created_tells_the_ai_to_ask_for_a_top_up_right_away(self):
        """No trial credit: the new wallet is empty, so `created` carries the next step."""
        self.bind_ok()
        payload, _ = self.tf.cmd_setup()
        step = payload["next_step"]
        self.assertEqual(step, self.tf.SETUP_NEXT_STEP)
        self.assertIn("余额为 0", step)
        self.assertIn("先充值", step)
        self.assertIn("充值 10 元", step)
        self.assertIn("付款链接", step)
        self.assertNoSecret(step, NEW_KEY, NEW_KEY_FRAG)
        # only a freshly created wallet needs it
        again, _ = self.tf.cmd_setup()
        self.assertNotIn("next_step", again)

    def test_S5_skips_when_tokenfish_credentials_exist(self):
        self.write_credentials("sk-existing-tokenfish-key")
        payload, code = self.tf.cmd_setup()
        self.assertEqual((code, payload["status"]), (0, "already_configured"))
        self.assertEqual(payload["key_masked"], self.tf.mask_key("sk-existing-tokenfish-key"))
        self.assertEqual(self.n(BIND), 0)

    def test_S6_skips_when_only_the_uking_device_json_exists(self):
        d = self.home / ".uking"
        d.mkdir(parents=True)
        (d / "device.json").write_text(json.dumps({"key": "sk-fromuking-device-001"}), encoding="utf-8")
        payload, code = self.tf.cmd_setup()
        self.assertEqual((code, payload["status"]), (0, "already_configured"))
        self.assertEqual(payload["key_masked"], self.tf.mask_key("sk-fromuking-device-001"))
        self.assertEqual(self.n(BIND), 0)
        self.assertFalse(self.creds_file().exists())  # the uking file is read-only for us

    def test_S7_skips_when_credentials_live_in_tokenfish_home(self):
        custom = self.cwd / "persistent-home"
        custom.mkdir()
        (custom / "credentials.json").write_text(json.dumps({"key": "sk-in-custom-home-001"}), encoding="utf-8")
        with mock.patch.dict(os.environ, {"TOKENFISH_HOME": str(custom)}):
            self.tf.reset_state_dir_cache()
            payload, code = self.tf.cmd_setup()
        self.assertEqual((code, payload["status"]), (0, "already_configured"))
        self.assertEqual(self.n(BIND), 0)

    def test_S8_rate_limited_429_reports_it_and_never_retries(self):
        self.backend.set_response(*BIND, 429, {"error": "rate-limited"})
        payload, code = self.tf.cmd_setup()
        self.assertErrorPayload(payload, "rate_limited", 4, code, retryable=True)
        self.assertIn("没有创建", payload["error"]["message"])
        self.assertEqual(self.n(BIND), 1)
        self.assertFalse(self.creds_file().exists())

    def test_S9_server_error_is_transient_and_says_retry_is_harmless(self):
        self.backend.set_response(*BIND, 500, {"error": "provision failed"})
        payload, code = self.tf.cmd_setup()
        self.assertErrorPayload(payload, "transient", 4, code, retryable=True)
        self.assertIn("空钱包", payload["error"]["message"])
        self.assertFalse(self.creds_file().exists())

    def test_S10_not_sent_connection_refused_is_transient(self):
        os.environ["TOKENFISH_API_BASE"] = self.free_port_url()
        payload, code = self.tf.cmd_setup()
        self.assertErrorPayload(payload, "transient", 4, code, retryable=True)
        self.assertFalse(self.creds_file().exists())

    def test_S11_maybe_sent_lost_response_is_transient_and_harmless_to_retry(self):
        self.backend.set_drop(*BIND)
        payload, code = self.tf.cmd_setup()
        self.assertErrorPayload(payload, "transient", 4, code, retryable=True)
        self.assertIn("空钱包", payload["error"]["message"])
        self.assertEqual(self.n(BIND), 1)
        self.assertFalse(self.creds_file().exists())

    def test_S12_bad_body_is_transient(self):
        for label, route_setup in (
            ("not json", lambda: self.backend.set_raw(*BIND, 200, b"<html>")),
            ("no apiKey", lambda: self.backend.set_response(*BIND, 200, {"walletId": "w1", "balanceTokens": 1})),
            ("empty apiKey", lambda: self.backend.set_response(*BIND, 200, {"walletId": "w1", "apiKey": ""})),
        ):
            with self.subTest(label):
                route_setup()
                payload, code = self.tf.cmd_setup()
                self.assertErrorPayload(payload, "transient", 4, code, retryable=True)
                self.assertFalse(self.creds_file().exists())

    def test_S13_save_failure_after_bind_says_wallet_may_exist_and_shows_no_secret(self):
        self.bind_ok()

        def boom(key, wallet_id=None):
            raise PermissionError("read-only")

        self.tf.save_credentials = boom
        payload, code = self.tf.cmd_setup()
        self.assertErrorPayload(payload, "local_state_unwritable", 3, code, retryable=False)
        self.assertIn("钱包可能已经创建", payload["error"]["message"])
        self.assertIn("没能保存", payload["error"]["message"])
        self.assertIn("TOKENFISH_HOME", payload["error"]["message"])
        self.assertNoSecret(payload, NEW_KEY, NEW_KEY_FRAG)
        self.assertEqual(self.n(BIND), 1)

    def test_S14_no_writable_state_dir_does_not_even_bind(self):
        self.bind_ok()
        self.stub_unwritable(lambda p: True)
        payload, code = self.tf.cmd_setup()
        self.assertErrorPayload(payload, "local_state_unwritable", 3, code, retryable=False)
        self.assertIn("没有创建钱包", payload["error"]["message"])
        self.assertEqual(self.n(BIND), 0)

    def test_S15_credentials_that_appear_while_binding_are_never_overwritten(self):
        home_dir = self.home / ".tokenfish"

        def racing_bind(method, path, headers, body):
            home_dir.mkdir(parents=True, exist_ok=True)
            (home_dir / "credentials.json").write_text(json.dumps({"key": "sk-other-process-key-1"}), encoding="utf-8")
            return 200, {"walletId": "w_new", "apiKey": NEW_KEY, "balanceTokens": 1}

        self.backend.set_handler(*BIND, racing_bind)
        payload, code = self.tf.cmd_setup()
        self.assertEqual((code, payload["status"]), (0, "already_configured"))
        self.assertEqual(payload["key_masked"], self.tf.mask_key("sk-other-process-key-1"))
        self.assertEqual(json.loads(self.creds_file().read_text(encoding="utf-8"))["key"], "sk-other-process-key-1")

    def test_S16_wallet_id_is_persisted_next_to_the_key_and_old_files_still_load(self):
        self.bind_ok(wallet_id="w_persisted")
        self.tf.cmd_setup()
        record = json.loads(self.creds_file().read_text(encoding="utf-8"))
        self.assertEqual(record, {"key": NEW_KEY, "wallet_id": "w_persisted"})
        self.assertEqual(self.tf.load_credentials(), NEW_KEY)

        # a 0.1.x file ({"key": ...} only) keeps working
        self.creds_file().write_text(json.dumps({"key": "sk-old-format-key-01"}), encoding="utf-8")
        self.assertEqual(self.tf.load_credentials(), "sk-old-format-key-01")

        # `login` style saves (no wallet id) stay in the old format
        self.tf.save_credentials("sk-login-key-0000001")
        self.assertEqual(
            json.loads(self.creds_file().read_text(encoding="utf-8")), {"key": "sk-login-key-0000001"}
        )

    def test_S17_home_unwritable_saves_to_tmp_and_the_notice_says_to_back_up_now(self):
        self.bind_ok()
        home_dir = self.tf.HOME_STATE_DIR
        self.stub_unwritable(lambda p: p == home_dir)
        payload, code = self.tf.cmd_setup()
        self.assertEqual((code, payload["status"]), (0, "created"))
        self.assertTrue(Path(payload["saved_to"]).samefile(self.tf._tmp_state_dir() / "credentials.json"))
        self.assertIn("临时位置", payload["backup_notice"])
        self.assertEqual(self.tf.load_credentials(), NEW_KEY)  # later calls find it there

    def test_S18_not_logged_in_points_at_setup_and_login(self):
        e = self.tf.err_not_logged_in()
        self.assertEqual((e.code, e.exit_code, e.retryable), ("not_logged_in", 3, False))
        self.assertIn("setup", e.next_action)
        self.assertIn("login --key-file", e.next_action)
        self.assertIn("征得同意", e.next_action)  # tell the user first, never open silently
        payload, code = self.call("tokenfish.wallet.get", {})
        self.assertEqual(payload["error"]["code"], "not_logged_in")
        self.assertEqual(self.n(BIND), 0)  # a tool call never opens a wallet on its own

    def test_S19_setup_is_a_cli_command_not_a_tool(self):
        listing, _ = self.tf.cmd_tools()
        self.assertNotIn("setup", {t["name"].split(".")[-1] for t in listing})
        payload, code = self.tf.cmd_call("setup", "{}")
        self.assertErrorPayload(payload, "invalid_arguments", 2, code, retryable=False)


# ======================================================================
# T: tokenfish.wallet.topup
# ======================================================================

TOPUP = "tokenfish.wallet.topup"
STATUS = "tokenfish.wallet.topup_status"
ALIPAY_URL = "https://openapi.alipay.com/gateway.do?charset=utf-8&out_trade_no=uclaw_1&sign=abc"
WX_CODE = "weixin://wxpay/bizpayurl?pr=AbC123xyz"


class TestTopup(_Base):
    def setUp(self):
        super().setUp()
        self.write_credentials(KEY)

    def alipay_ok(self, order_id="uclaw_100"):
        self.backend.set_response(*RECHARGE_CREATE, 200, {"orderId": order_id, "payUrl": ALIPAY_URL, "channel": "alipay"})

    def wechat_ok(self, order_id="uclaw_200"):
        self.backend.set_response(*RECHARGE_CREATE, 200, {"orderId": order_id, "codeUrl": WX_CODE, "channel": "wechat"})

    def test_T1_alipay_returns_the_pay_url_and_sends_the_key_only_in_the_post_body(self):
        self.alipay_ok()
        payload, code = self.call(TOPUP, {"amount": 10, "channel": "alipay"})
        self.assertEqual(code, 0, msg=payload)
        self.assertEqual(sorted(payload), ["amount_cny", "channel", "next_step", "order_id", "pay_link"])
        self.assertEqual(payload["order_id"], "uclaw_100")
        self.assertEqual(payload["channel"], "alipay")
        self.assertEqual(payload["amount_cny"], 10)
        self.assertEqual(payload["pay_link"], ALIPAY_URL)
        self.assertIn("好了", payload["next_step"])
        self.assertIn("topup_status", payload["next_step"])
        self.assertNoSecret(payload, KEY, KEY_FRAG)
        (req,) = self.requests_for(RECHARGE_CREATE)
        self.assertEqual(req["body"], {"apiKey": KEY, "amount": 10, "channel": "alipay"})
        self.assertNotIn("authorization", {k.lower() for k in req["headers"]})
        self.assertNotIn(KEY_FRAG, req["path"])

    def test_T2_channel_defaults_to_alipay(self):
        self.alipay_ok()
        payload, code = self.call(TOPUP, {"amount": 5})
        self.assertEqual(code, 0, msg=payload)
        self.assertEqual(payload["channel"], "alipay")
        self.assertEqual(self.requests_for(RECHARGE_CREATE)[0]["body"]["channel"], "alipay")

    def test_T3_wechat_returns_a_pay_page_link_with_the_code_url_in_the_fragment(self):
        self.wechat_ok("uclaw_200")
        payload, code = self.call(TOPUP, {"amount": 25, "channel": "wechat"})
        self.assertEqual(code, 0, msg=payload)
        link = payload["pay_link"]
        self.assertTrue(link.startswith(SITE + "/pay.html#"), msg=link)
        self.assertEqual(
            fragment_params(link), {"wx": [WX_CODE], "amt": ["25"], "order": ["uclaw_200"]}
        )
        self.assertNotIn(KEY_FRAG, link)
        self.assertNoSecret(payload, KEY, KEY_FRAG)
        self.assertEqual(self.requests_for(RECHARGE_CREATE)[0]["body"], {"apiKey": KEY, "amount": 25, "channel": "wechat"})

    def test_T4_cli_stdout_and_stderr_never_contain_the_key_for_either_channel(self):
        for channel, setup in (("alipay", self.alipay_ok), ("wechat", self.wechat_ok)):
            with self.subTest(channel):
                setup()
                proc = run_cli(["call", TOPUP], self.env(), json.dumps({"amount": 10, "channel": channel}))
                self.assertEqual(proc.returncode, 0, msg=proc.stderr)
                self.assertNoSecret(proc.stdout + proc.stderr, KEY, KEY_FRAG)
                out = json.loads(proc.stdout)
                self.assertNotIn(KEY_FRAG, out["pay_link"])
                self.assertEqual(out["channel"], channel)

    def test_T5_invalid_amounts_are_rejected_before_any_request(self):
        bad = [0, 0.99, 501, 500.01, -5, "10", True, None, [10], {"v": 10}, 1.234, 10.001, 10.5, 1.5, 499.5, 0.5]
        for amount in bad:
            with self.subTest(amount=amount):
                payload, code = self.call(TOPUP, {"amount": amount})
                self.assertErrorPayload(payload, "invalid_arguments", 2, code, retryable=False)
                self.assertIn("amount", payload["error"]["message"])
        payload, code = self.call(TOPUP, {})
        self.assertErrorPayload(payload, "invalid_arguments", 2, code, retryable=False)
        for literal in ("NaN", "Infinity", "-Infinity"):
            with self.subTest(literal=literal):
                payload, code = self.tf.cmd_call(TOPUP, '{"amount": %s}' % literal)
                self.assertErrorPayload(payload, "invalid_arguments", 2, code, retryable=False)
        self.assertEqual(self.n(RECHARGE_CREATE), 0)

    def test_T6_invalid_channels_are_rejected_before_any_request(self):
        for channel in ("paypal", "ALIPAY", "", 5, ["wechat"], True):
            with self.subTest(channel=channel):
                payload, code = self.call(TOPUP, {"amount": 10, "channel": channel})
                self.assertErrorPayload(payload, "invalid_arguments", 2, code, retryable=False)
                self.assertIn("channel", payload["error"]["message"])
        self.assertEqual(self.n(RECHARGE_CREATE), 0)

    def test_T7_boundary_and_whole_number_floats_are_accepted(self):
        self.alipay_ok()
        for amount, expected in ((1, 1), (500, 500), (10.0, 10), (1e2, 100), (500.0, 500)):
            with self.subTest(amount=amount):
                payload, code = self.call(TOPUP, {"amount": amount})
                self.assertEqual(code, 0, msg=payload)
                self.assertEqual(payload["amount_cny"], expected)
                self.assertEqual(self.requests_for(RECHARGE_CREATE)[-1]["body"]["amount"], expected)

    def test_T8_channel_unavailable_suggests_the_other_channel(self):
        cases = (
            ("alipay", {"error": "alipay not configured"}, "wechat"),
            ("wechat", {"error": "wechat pay not configured"}, "alipay"),
        )
        for channel, body, other in cases:
            with self.subTest(channel):
                self.backend.set_response(*RECHARGE_CREATE, 400, body)
                payload, code = self.call(TOPUP, {"amount": 10, "channel": channel})
                self.assertErrorPayload(payload, "channel_unavailable", 3, code, retryable=False)
                self.assertIn(f"channel={other}", payload["error"]["next_action"])
                self.assertIn(OFFICIAL, payload["error"]["next_action"])

    def test_T9_other_server_answers_are_mapped_from_the_real_handler(self):
        cases = (
            (400, {"error": "token provision failed", "message": "Key 建档失败，暂不能发起支付，请稍后重试或联系客服"},
             "transient", 4, True),
            (500, {"error": "create order failed"}, "transient", 4, True),
            (500, {"error": "create payment failed"}, "transient", 4, True),
            (429, {"error": "rate-limited"}, "rate_limited", 4, True),
            (400, {"error": "amount must be between 1 and 500"}, "invalid_arguments", 2, False),
            (400, {"error": "invalid request"}, "invalid_arguments", 2, False),
            (503, {"error": "down"}, "transient", 4, True),
        )
        for status, body, err_code, exit_code, retryable in cases:
            with self.subTest(status=status, body=body):
                self.backend.set_response(*RECHARGE_CREATE, status, body)
                payload, code = self.call(TOPUP, {"amount": 10})
                self.assertErrorPayload(payload, err_code, exit_code, code, retryable)
                self.assertNoSecret(payload, KEY, KEY_FRAG)

    def test_T10_unusable_success_answers_and_network_failures_are_transient(self):
        bad = (
            ("alipay", {"orderId": "uclaw_1"}),  # no payUrl
            ("alipay", {"payUrl": ALIPAY_URL}),  # no orderId
            ("alipay", {"orderId": "uclaw_1", "payUrl": "javascript:alert(1)"}),
            ("wechat", {"orderId": "uclaw_1"}),  # no codeUrl
            ("wechat", {"orderId": "uclaw_1", "codeUrl": "https://evil.example/x"}),  # not weixin://
        )
        for channel, body in bad:
            with self.subTest(channel=channel, body=body):
                self.backend.set_response(*RECHARGE_CREATE, 200, body)
                payload, code = self.call(TOPUP, {"amount": 10, "channel": channel})
                self.assertErrorPayload(payload, "transient", 4, code, retryable=True)
        self.backend.set_raw(*RECHARGE_CREATE, 200, b"<html>")
        payload, code = self.call(TOPUP, {"amount": 10})
        self.assertErrorPayload(payload, "transient", 4, code, retryable=True)
        self.backend.set_drop(*RECHARGE_CREATE)
        payload, code = self.call(TOPUP, {"amount": 10})
        self.assertErrorPayload(payload, "transient", 4, code, retryable=True)
        self.assertIn("没有产生任何扣费", payload["error"]["message"])

    def test_T11_a_link_that_would_contain_the_key_is_never_handed_out(self):
        self.backend.set_response(
            *RECHARGE_CREATE, 200, {"orderId": "uclaw_1", "payUrl": "https://pay.example/?k=" + KEY}
        )
        payload, code = self.call(TOPUP, {"amount": 10})
        self.assertErrorPayload(payload, "transient", 4, code, retryable=True)
        self.assertNoSecret(payload, KEY, KEY_FRAG)

    def test_T12_without_credentials_nothing_is_sent(self):
        self.creds_file().unlink()
        payload, code = self.call(TOPUP, {"amount": 10})
        self.assertErrorPayload(payload, "not_logged_in", 3, code, retryable=False)
        self.assertEqual(self.n(RECHARGE_CREATE), 0)

    def test_T13_tools_listing_has_both_unbilled_tools_with_schemas(self):
        listing, _ = self.tf.cmd_tools()
        tools = {t["name"]: t for t in listing}
        topup = tools[TOPUP]
        self.assertFalse(topup["billed"])
        props = topup["parameters"]["properties"]
        self.assertEqual((props["amount"]["minimum"], props["amount"]["maximum"]), (1, 500))
        self.assertEqual(props["amount"]["type"], "integer")
        self.assertEqual(props["channel"]["enum"], ["alipay", "wechat"])
        self.assertEqual(topup["parameters"]["required"], ["amount"])
        status = tools[STATUS]
        self.assertFalse(status["billed"])
        self.assertEqual(status["parameters"]["required"], ["order_id"])


# ======================================================================
# U: tokenfish.wallet.topup_status
# ======================================================================


class TestTopupStatus(_Base):
    def setUp(self):
        super().setUp()
        self.write_credentials(KEY)

    def set_status(self, order_id, status, http_status=200):
        self.backend.set_response(
            *status_route(order_id), http_status, {"orderId": order_id, "status": status, "amount": 10}
        )

    def test_U1_pending_and_paid_are_not_credited_and_skip_the_balance_lookup(self):
        for status in ("pending", "paid", "failed"):
            with self.subTest(status):
                self.set_status("uclaw_1", status)
                payload, code = self.call(STATUS, {"order_id": "uclaw_1"})
                self.assertEqual(code, 0, msg=payload)
                self.assertEqual(payload, {"order_id": "uclaw_1", "status": status, "credited": False})
        self.assertEqual(self.n(WALLET_GET), 0)

    def test_U2_recharged_is_credited_and_includes_the_wallet_balance(self):
        self.set_status("uclaw_2", "recharged")
        self.wallet_ok(total_available=5000000)
        payload, code = self.call(STATUS, {"order_id": "uclaw_2"})
        self.assertEqual(code, 0, msg=payload)
        self.assertEqual(
            payload, {"order_id": "uclaw_2", "status": "recharged", "credited": True, "balance_cny": 10.0}
        )
        self.assertEqual(self.n(WALLET_GET), 1)
        self.assertNoSecret(payload, KEY, KEY_FRAG)

    def test_U3_balance_lookup_failure_does_not_fail_the_tool(self):
        self.set_status("uclaw_3", "recharged")
        for label, arrange in (
            ("http 500", lambda: self.backend.set_response(*WALLET_GET, 500, {"error": "x"})),
            ("bad body", lambda: self.backend.set_raw(*WALLET_GET, 200, b"<html>")),
            ("dropped", lambda: self.backend.set_drop(*WALLET_GET)),
            ("no quota field", lambda: self.backend.set_response(*WALLET_GET, 200, {"data": {}})),
        ):
            with self.subTest(label):
                arrange()
                payload, code = self.call(STATUS, {"order_id": "uclaw_3"})
                self.assertEqual(code, 0, msg=payload)
                self.assertEqual(payload, {"order_id": "uclaw_3", "status": "recharged", "credited": True})

    def test_U4_unknown_order_is_order_not_found(self):
        self.backend.set_response(*status_route("nope"), 404, {"error": "order not found"})
        payload, code = self.call(STATUS, {"order_id": "nope"})
        self.assertErrorPayload(payload, "order_not_found", 2, code, retryable=False)
        self.assertIn("nope", payload["error"]["message"])

    def test_U5_order_id_is_url_encoded_and_no_key_or_auth_header_is_sent(self):
        weird = "a b&c=d/é"
        self.set_status(weird, "pending")
        payload, code = self.call(STATUS, {"order_id": weird})
        self.assertEqual(code, 0, msg=payload)
        (req,) = self.requests_for(status_route(weird))
        self.assertIn("order_id=a%20b%26c%3Dd%2F%C3%A9", req["path"])
        self.assertNotIn("authorization", {k.lower() for k in req["headers"]})
        self.assertNotIn(KEY_FRAG, req["path"])

    def test_U6_invalid_order_ids_are_rejected_before_any_request(self):
        for bad in (None, "", 5, ["x"], "x" * 201):
            with self.subTest(order_id=bad):
                payload, code = self.call(STATUS, {"order_id": bad})
                self.assertErrorPayload(payload, "invalid_arguments", 2, code, retryable=False)
        payload, code = self.call(STATUS, {})
        self.assertErrorPayload(payload, "invalid_arguments", 2, code, retryable=False)
        self.assertEqual(len(self.backend.requests), 0)

    def test_U7_server_trouble_is_transient(self):
        self.backend.set_response(*status_route("uclaw_9"), 500, {"error": "x"})
        payload, code = self.call(STATUS, {"order_id": "uclaw_9"})
        self.assertErrorPayload(payload, "transient", 4, code, retryable=True)
        self.backend.set_response(*status_route("uclaw_9"), 200, {"orderId": "uclaw_9"})  # no status
        payload, code = self.call(STATUS, {"order_id": "uclaw_9"})
        self.assertErrorPayload(payload, "transient", 4, code, retryable=True)

    def test_U8_cli_end_to_end_topup_then_status(self):
        self.backend.set_response(
            *RECHARGE_CREATE, 200, {"orderId": "uclaw_77", "codeUrl": WX_CODE, "channel": "wechat"}
        )
        self.set_status("uclaw_77", "recharged")
        self.wallet_ok(total_available=2500000)
        created = run_cli(["call", TOPUP], self.env(), json.dumps({"amount": 10, "channel": "wechat"}))
        order_id = json.loads(created.stdout)["order_id"]
        proc = run_cli(["call", STATUS], self.env(), json.dumps({"order_id": order_id}))
        self.assertEqual(proc.returncode, 0, msg=proc.stderr)
        self.assertEqual(json.loads(proc.stdout)["balance_cny"], 5.0)
        self.assertNoSecret(created.stdout + created.stderr + proc.stdout + proc.stderr, KEY, KEY_FRAG)


# ======================================================================
# L: delivery links on successful paid results
# ======================================================================


class TestDeliveryLinks(_Base):
    def setUp(self):
        super().setUp()
        self.write_credentials(KEY)

    def music(self, crid, **extra):
        args = {"prompt": "city pop", "client_request_id": crid}
        args.update(extra)
        return self.call("tokenfish.music.generate", args)

    def minimax_done(self, url="https://cdn.example/a.mp3?sig=abc&x=1", **extra):
        body = {
            "id": "mm-1",
            "status": "succeeded",
            "output_url": url,
            "quota_charged": 600000,
            "title": "夜色",
            "duration": 95,
        }
        body.update(extra)
        self.backend.set_response(*MM_SUBMIT, 200, body)

    def expected_play_url(self, url, title, dur):
        return SITE + "/play.html#" + urllib.parse.urlencode({"src": url, "title": title, "dur": dur})

    def assertDeliveryExtras(self, payload, balance=8.0):
        self.assertEqual(payload["links"], {"site": OFFICIAL, "tokenfish": SITE})
        self.assertEqual(payload["balance_after_cny"], balance)
        self.assertIn("充值 N 元", payload["topup_hint"])
        self.assertIn(f"¥{balance:.2f}", payload["topup_hint"])

    def test_L1_minimax_success_has_play_url_links_balance_and_hint(self):
        self.minimax_done()
        self.wallet_ok(total_available=4000000)
        payload, code = self.music("crid-l1", model="minimax")
        self.assertEqual(code, 0, msg=payload)
        self.assertEqual(payload["status"], "succeeded")
        self.assertEqual(payload["charged_cny"], 1.2)
        (clip,) = payload["clips"]
        self.assertEqual(clip["play_url"], self.expected_play_url("https://cdn.example/a.mp3?sig=abc&x=1", "夜色", 95))
        self.assertEqual(
            fragment_params(clip["play_url"]),
            {"src": ["https://cdn.example/a.mp3?sig=abc&x=1"], "title": ["夜色"], "dur": ["95"]},
        )
        self.assertDeliveryExtras(payload)
        self.assertNoSecret(payload, KEY, KEY_FRAG)
        self.assertEqual(self.n(MM_SUBMIT), 1)
        self.assertEqual(self.n(WALLET_GET), 1)

    def test_L2_cli_output_carries_the_links_and_never_the_key(self):
        self.minimax_done()
        self.wallet_ok()
        args = {"prompt": "city pop", "model": "minimax", "client_request_id": "crid-l2"}
        proc = run_cli(["call", "tokenfish.music.generate"], self.env(), json.dumps(args))
        self.assertEqual(proc.returncode, 0, msg=proc.stderr)
        self.assertNoSecret(proc.stdout + proc.stderr, KEY, KEY_FRAG)
        out = json.loads(proc.stdout)
        self.assertTrue(out["clips"][0]["play_url"].startswith(SITE + "/play.html#src="))
        self.assertEqual(out["links"]["site"], OFFICIAL)

    def test_L3_missing_title_and_duration_become_empty_params(self):
        self.minimax_done(title=None, duration=None)
        self.wallet_ok()
        payload, _ = self.music("crid-l3", model="minimax")
        params = fragment_params(payload["clips"][0]["play_url"])
        self.assertEqual((params["title"], params["dur"]), ([""], [""]))

    def test_L4_balance_lookup_failure_still_returns_the_result_without_balance(self):
        self.minimax_done()
        for label, arrange in (
            ("no route (404)", lambda: self.backend._routes.pop(WALLET_GET, None)),
            ("http 500", lambda: self.backend.set_response(*WALLET_GET, 500, {"error": "x"})),
            ("bad body", lambda: self.backend.set_raw(*WALLET_GET, 200, b"<html>")),
            ("dropped", lambda: self.backend.set_drop(*WALLET_GET)),
        ):
            with self.subTest(label):
                arrange()
                crid = "crid-l4-" + re.sub(r"\W", "", label)
                payload, code = self.music(crid, model="minimax")
                self.assertEqual(code, 0, msg=payload)
                self.assertNotIn("error", payload)
                self.assertEqual(payload["status"], "succeeded")
                self.assertNotIn("balance_after_cny", payload)
                self.assertIn("play_url", payload["clips"][0])
                self.assertEqual(payload["links"]["site"], OFFICIAL)
                self.assertIn("充值 N 元", payload["topup_hint"])
                self.assertNotIn("当前余额", payload["topup_hint"])

    def test_L5_extras_never_break_a_final_result_even_on_unexpected_errors(self):
        self.minimax_done()

        def boom(*_a, **_k):
            raise RuntimeError("unexpected")

        self.tf.fetch_wallet_raw = boom  # not an API error: a bug in the extras
        payload, code = self.music("crid-l5a", model="minimax")
        self.assertEqual(code, 0, msg=payload)
        self.assertNotIn("balance_after_cny", payload)
        self.assertIn("play_url", payload["clips"][0])

        self.tf._play_url = boom  # the link builder itself blows up -> the plain result comes back
        payload, code = self.music("crid-l5b", model="minimax")
        self.assertEqual(code, 0, msg=payload)
        self.assertEqual(payload["status"], "succeeded")
        self.assertNotIn("play_url", payload["clips"][0])
        self.assertNotIn("links", payload)
        self.assertEqual(self.n(MM_SUBMIT), 2)  # one submit per distinct client_request_id, nothing more

    def test_L6_non_terminal_results_get_no_links_and_no_balance_lookup(self):
        self.backend.set_response(*MM_SUBMIT, 200, {"id": "mm-run", "status": "running"})
        self.wallet_ok()
        payload, code = self.music("crid-l6", model="minimax")
        self.assertEqual(code, 0, msg=payload)
        self.assertEqual(payload["status"], "running")
        for field in ("links", "balance_after_cny", "topup_hint"):
            self.assertNotIn(field, payload)
        self.assertEqual(self.n(WALLET_GET), 0)

        self.backend.set_response(*SUNO_SUBMIT, 200, {"code": "success", "data": "task-q"})
        payload, code = self.music("crid-l6b", model="suno")
        self.assertEqual(payload["status"], "queued")
        self.assertNotIn("links", payload)
        self.assertEqual(self.n(WALLET_GET), 0)

    def test_L7_links_are_not_written_into_the_local_task_store(self):
        self.minimax_done()
        self.wallet_ok()
        self.music("crid-l7", model="minimax")
        store = json.loads((self.tf.state_dir() / "tasks.json").read_text(encoding="utf-8"))
        text = json.dumps(store)
        for forbidden in ("play_url", "links", "balance_after_cny", "topup_hint"):
            self.assertNotIn(forbidden, text)
        self.assertEqual(store["crid-l7"]["status"], "submitted")

    def test_L8_suno_refresh_succeeded_adds_play_url_per_clip(self):
        self.backend.set_response(*SUNO_SUBMIT, 200, {"code": "success", "data": "task-s1"})
        self.wallet_ok(total_available=2500000)
        first, _ = self.music("crid-l8", model="suno")
        self.assertEqual(first["status"], "queued")

        self.backend.set_response(
            "GET",
            "/suno/fetch/task-s1",
            200,
            {
                "data": {
                    "status": "SUCCESS",
                    "data": [
                        {"audio_url": "https://cdn.example/1.mp3", "title": "A 面", "duration": 120.5},
                        {"audio_url": "https://cdn.example/2.mp3", "title": "B", "duration": 118},
                        {"audio_url": None, "title": "no url"},
                        {"audio_url": "ftp://cdn.example/3.mp3", "title": "wrong scheme"},
                    ],
                }
            },
        )
        got, code = self.call("tokenfish.tasks.get", {"task_id": "task-s1"})
        self.assertEqual(code, 0, msg=got)
        urls = [c.get("play_url") for c in got["clips"]]
        self.assertEqual(urls[0], self.expected_play_url("https://cdn.example/1.mp3", "A 面", 120.5))
        self.assertEqual(urls[1], self.expected_play_url("https://cdn.example/2.mp3", "B", 118))
        self.assertEqual(urls[2:], [None, None])
        self.assertDeliveryExtras(got, balance=5.0)
        self.assertNoSecret(got, KEY, KEY_FRAG)
        self.assertEqual(self.n(SUNO_SUBMIT), 1)

    def test_L9_suno_still_in_progress_gets_nothing(self):
        self.backend.set_response(*SUNO_SUBMIT, 200, {"code": "success", "data": "task-s2"})
        self.music("crid-l9", model="suno")
        self.backend.set_response("GET", "/suno/fetch/task-s2", 200, {"data": {"status": "IN_PROGRESS", "data": []}})
        got, _ = self.call("tokenfish.tasks.get", {"task_id": "task-s2"})
        self.assertEqual(got["status"], "running")
        self.assertNotIn("links", got)

    def test_L10_replaying_a_finished_music_request_also_carries_the_links(self):
        self.backend.set_response(*SUNO_SUBMIT, 200, {"code": "success", "data": "task-s3"})
        self.music("crid-l10", model="suno")
        self.backend.set_response(
            "GET",
            "/suno/fetch/task-s3",
            200,
            {"data": {"status": "SUCCESS", "data": [{"audio_url": "https://cdn.example/r.mp3", "title": "R", "duration": 60}]}},
        )
        self.wallet_ok()
        again, code = self.music("crid-l10", model="suno")  # same id, same args: refresh, no resubmit
        self.assertEqual(code, 0, msg=again)
        self.assertIn("play_url", again["clips"][0])
        self.assertEqual(self.n(SUNO_SUBMIT), 1)

    def test_L11_images_with_a_url_get_play_url_with_img_and_local_files_do_not(self):
        self.backend.set_response(
            *IMG_SUBMIT,
            200,
            {"data": [{"url": "https://img.example/1.png?a=1&b=2"}, {"b64_json": TINY_B64}], "quota_charged": 300000},
        )
        self.wallet_ok(total_available=1000000)
        payload, code = self.call(
            "tokenfish.images.generate", {"prompt": "a red fish", "n": 2, "client_request_id": "crid-l11"}
        )
        self.assertEqual(code, 0, msg=payload)
        first, second = payload["images"]
        self.assertEqual(
            first["play_url"], SITE + "/play.html#" + urllib.parse.urlencode({"img": "https://img.example/1.png?a=1&b=2"})
        )
        self.assertIn("img=", first["play_url"])
        self.assertNotIn("src=", first["play_url"])
        self.assertEqual(fragment_params(first["play_url"])["img"], ["https://img.example/1.png?a=1&b=2"])
        self.assertNotIn("play_url", second)  # only a local file
        self.assertTrue(Path(second["path"]).is_file())
        self.assertDeliveryExtras(payload, balance=2.0)
        self.assertNoSecret(payload, KEY, KEY_FRAG)

        replay, code2 = self.call(
            "tokenfish.images.generate", {"prompt": "a red fish", "n": 2, "client_request_id": "crid-l11"}
        )
        self.assertEqual(code2, 0)
        self.assertIn("play_url", replay["images"][0])
        self.assertEqual(self.n(IMG_SUBMIT), 1)

    def test_L12_play_url_never_contains_the_key_even_for_odd_titles(self):
        self.minimax_done(title="歌 & 词 #1 ?x=y", duration=61)
        self.wallet_ok()
        payload, _ = self.music("crid-l12", model="minimax")
        link = payload["clips"][0]["play_url"]
        self.assertNotIn(KEY_FRAG, link)
        self.assertEqual(fragment_params(link)["title"], ["歌 & 词 #1 ?x=y"])
        self.assertEqual(urllib.parse.urlsplit(link).netloc, "tokenfish.u-claw.org.cn")

    def test_L13_failed_results_stay_untouched(self):
        self.backend.set_response(*MM_SUBMIT, 200, {"id": "mm-f", "status": "failed"})
        payload, code = self.music("crid-l13", model="minimax")
        self.assertEqual(payload["status"], "failed")
        self.assertNotIn("links", payload)


class TestWholeYuanOnly(_Base):
    def setUp(self):
        super().setUp()
        self.write_credentials(KEY)

    def test_T14_decimal_amounts_never_reach_the_backend(self):
        """The backend does int64(amount*100) for wechat, which truncates fractional yuan."""
        for amount in (10.5, 1.15, 19.99, 0.29, 499.99, 1.0001):
            for channel in ("alipay", "wechat"):
                with self.subTest(amount=amount, channel=channel):
                    payload, code = self.call(TOPUP, {"amount": amount, "channel": channel})
                    self.assertErrorPayload(payload, "invalid_arguments", 2, code, retryable=False)
                    self.assertIn("amount", payload["error"]["message"])
        payload, code = self.call(TOPUP, {"amount": 10.5})
        self.assertIn("整数", payload["error"]["message"])
        self.assertEqual(self.n(RECHARGE_CREATE), 0)

    def test_T15_a_whole_number_float_is_sent_as_an_integer(self):
        self.backend.set_response(
            *RECHARGE_CREATE, 200, {"orderId": "uclaw_9", "codeUrl": WX_CODE, "channel": "wechat"}
        )
        payload, code = self.call(TOPUP, {"amount": 10.0, "channel": "wechat"})
        self.assertEqual(code, 0, msg=payload)
        (req,) = self.requests_for(RECHARGE_CREATE)
        self.assertEqual(req["body"]["amount"], 10)
        self.assertIsInstance(req["body"]["amount"], int)
        self.assertEqual(fragment_params(payload["pay_link"])["amt"], ["10"])


class TestRechargePointers(_Base):
    """insufficient_balance / wallet.get / the old `topup` subcommand all point at the
    in-conversation flow now (ask amount + channel -> tokenfish.wallet.topup -> pay_link ->
    topup_status -> retry the same client_request_id)."""

    FLOW_WORDS = ("tokenfish.wallet.topup", "tokenfish.wallet.topup_status", "client_request_id", "支付宝", "微信")

    def test_E1_insufficient_balance_text_describes_the_new_flow(self):
        e = self.tf.err_insufficient_balance()
        self.assertEqual((e.code, e.exit_code, e.retryable), ("insufficient_balance", 3, False))
        for text in (e.message, e.next_action):
            for word in self.FLOW_WORDS:
                self.assertIn(word, text)
        self.assertIn("本次未扣费", e.message)
        self.assertIn("整数", e.message)
        for text in (e.message, e.next_action):
            self.assertNotIn("tokenfish_client.py topup", text)
            self.assertNotIn("运行 topup", text)
            self.assertNotIn("运行 `python tokenfish_client.py topup`", text)

    def test_E2_a_real_402_surfaces_the_new_flow_in_the_error_payload(self):
        self.write_credentials(KEY)
        self.backend.set_response(
            *MM_SUBMIT, 402, {"error": {"type": "insufficient_quota", "message": "no quota"}}
        )
        payload, code = self.call(
            "tokenfish.music.generate", {"prompt": "x", "model": "minimax", "client_request_id": "crid-e2"}
        )
        self.assertErrorPayload(payload, "insufficient_balance", 3, code, retryable=False)
        self.assertIn("tokenfish.wallet.topup", payload["error"]["message"])
        self.assertIn("tokenfish.wallet.topup", payload["error"]["next_action"])

    def test_E3_wallet_get_topup_field_points_at_the_new_flow(self):
        self.write_credentials(KEY)
        self.wallet_ok(total_available=0)
        payload, code = self.call("tokenfish.wallet.get", {})
        self.assertEqual(code, 0, msg=payload)
        hint = payload["topup"]
        for word in ("tokenfish.wallet.topup", "tokenfish.wallet.topup_status", "支付宝", "微信", "整数"):
            self.assertIn(word, hint)
        self.assertNotIn("tokenfish_client.py topup", hint)

    def test_E4_old_topup_subcommand_still_exists_and_is_described_as_desktop_only(self):
        parser = self.tf.build_arg_parser()
        self.assertEqual(parser.parse_args(["topup"]).command, "topup")
        buf = io.StringIO()
        with mock.patch.dict(os.environ, {"COLUMNS": "300"}):
            top = self.tf.build_arg_parser().format_help()
            with contextlib.redirect_stdout(buf), self.assertRaises(SystemExit):
                self.tf.build_arg_parser().parse_args(["topup", "--help"])
        sub = buf.getvalue()
        self.assertIn("在本机浏览器打开充值页（桌面环境用）", top)
        self.assertIn("在本机浏览器打开充值页（桌面环境用）", sub)
        self.assertNotIn("复制到剪贴板", top)
        # without credentials it still stops before touching the browser (existing behavior)
        payload, code = self.tf.cmd_topup()
        self.assertEqual((payload["error"]["code"], code), ("not_logged_in", 3))


# ======================================================================
# P: static pages and versions
# ======================================================================


class TestStaticPages(unittest.TestCase):
    def read(self, rel):
        return (REPO_ROOT / rel).read_text(encoding="utf-8")

    def test_P1_play_js_and_pay_js_never_inject_html_or_evaluate_strings(self):
        for rel in ("site/assets/play.js", "site/assets/pay.js"):
            source = self.read(rel)
            for forbidden in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write", "eval(", "new Function"):
                with self.subTest(file=rel, forbidden=forbidden):
                    self.assertNotIn(forbidden, source)

    def test_P2_pages_have_no_external_resources_no_inline_handlers_and_no_referrer(self):
        for rel in ("site/play.html", "site/pay.html"):
            html = self.read(rel)
            with self.subTest(file=rel):
                self.assertIn('<meta name="referrer" content="no-referrer" />', html)
                self.assertIsNone(re.search(r"<script[^>]+src=[\"']https?:", html))
                self.assertIsNone(re.search(r"<link[^>]+href=[\"']https?:", html))
                self.assertIsNone(re.search(r"<img[^>]+src=[\"']https?:", html))
                self.assertIsNone(re.search(r"\son\w+\s*=", html), msg="inline event handler")
                self.assertNotIn("<script>", html)  # no inline scripts either
                self.assertIn(OFFICIAL, html)
                self.assertIsNone(re.search(r"(?<![\w.-])tokenfish\.cn(?![\w-])", html))
        self.assertIn('src="assets/play.js"', self.read("site/play.html"))
        self.assertIn('src="assets/qrcode.js"', self.read("site/pay.html"))
        self.assertIn('src="assets/pay.js"', self.read("site/pay.html"))

    def test_P3_page_copy_matches_the_agreed_wording(self):
        play = self.read("site/play.html") + self.read("site/assets/play.js")
        for text in ("再做一首", "虾盘云官网", "充值 10 元", "最短约 12 小时就会失效", "链接无效"):
            self.assertIn(text, play)
        pay = self.read("site/pay.html") + self.read("site/assets/pay.js")
        for text in ("付款后回到 AI 对话说『好了』，AI 会帮你查到账", "虾盘云官网", "weixin://", "链接无效"):
            self.assertIn(text, pay)
        self.assertIn("https:", self.read("site/assets/play.js"))

    def test_P4_qrcode_js_is_the_vendored_mit_library_with_its_license_header(self):
        source = self.read("site/assets/qrcode.js")
        self.assertIn("qrcode-generator 2.0.4", source)
        self.assertIn("Kazuhiko Arase", source)
        self.assertIn("MIT", source)
        self.assertIn("var qrcode=function(){", source)
        self.assertNotIn("eval(", source)

    def test_P5_llms_txt_mentions_both_pages(self):
        text = self.read("site/llms.txt")
        self.assertIn(SITE + "/play.html", text)
        self.assertIn(SITE + "/pay.html", text)

    def test_P6_versions_are_0_2_0_and_site_origin_is_the_launch_origin(self):
        tf = import_client_module()
        self.assertEqual(tf.CLIENT_VERSION, "0.2.1")
        self.assertEqual(tf.SITE_ORIGIN, SITE)
        self.assertEqual(tf.OFFICIAL_SITE_URL, OFFICIAL)
        for slug in ("tokenfish", "lyrics-to-song"):
            manifest = json.loads(self.read(f"skills/{slug}/manifest.json"))
            self.assertEqual(manifest["version"], "0.2.1")

    def test_P7_skill_docs_and_install_template_use_setup_and_keep_login_as_the_import_path(self):
        for rel in (
            "skills/tokenfish/SKILL.md",
            "skills/lyrics-to-song/SKILL.md",
            "skills/tokenfish/references/installation.md",
            "skills/lyrics-to-song/references/installation.md",
            "build/templates/install.md.tmpl",
        ):
            text = self.read(rel)
            with self.subTest(file=rel):
                self.assertIn("setup", text)
                self.assertIn("login --key-file", text)
                self.assertIn("backup_notice", text)
                self.assertIn("next_step", text)
        for rel in ("skills/tokenfish/SKILL.md", "skills/lyrics-to-song/SKILL.md"):
            text = self.read(rel)
            with self.subTest(file=rel):
                for needle in ("tokenfish.wallet.topup", "topup_status", "pay_link", "play_url", "links.site", "balance_after_cny", "topup_hint"):
                    self.assertIn(needle, text)
                # first run: after `setup` created an empty wallet, go straight to the recharge flow
                self.assertIn("立刻进入下面的「充值」流程", text)
                self.assertIn("不要先尝试一次付费生成", text)

    def test_P8_no_doc_tells_the_ai_to_run_the_old_topup_command_in_a_chat_flow(self):
        for rel in (
            "skills/tokenfish/SKILL.md",
            "skills/lyrics-to-song/SKILL.md",
            "skills/tokenfish/references/billing-and-errors.md",
            "skills/lyrics-to-song/references/billing-and-errors.md",
            "build/templates/install.md.tmpl",
        ):
            with self.subTest(file=rel):
                self.assertNotIn("tokenfish_client.py topup", self.read(rel).replace("topup_status", ""))
        for rel in ("skills/tokenfish/references/installation.md", "skills/lyrics-to-song/references/installation.md", "README.md"):
            with self.subTest(file=rel):
                line = [l for l in self.read(rel).splitlines() if re.search(r"tokenfish_client\.py topup\s", l)]
                self.assertEqual(len(line), 1)
                self.assertIn("在本机浏览器打开充值页（桌面环境用）", line[0])

    def test_P9_docs_say_whole_yuan_only(self):
        for rel in (
            "skills/tokenfish/SKILL.md",
            "skills/lyrics-to-song/SKILL.md",
            "skills/tokenfish/references/billing-and-errors.md",
            "skills/lyrics-to-song/references/billing-and-errors.md",
        ):
            with self.subTest(file=rel):
                text = self.read(rel)
                self.assertIn("整数", text)
                self.assertNotIn("两位小数", text)
        self.assertIn("var AMOUNT_RE = /^\\d{1,3}$/;", self.read("site/assets/pay.js"))

    def test_P10_first_run_copy_on_the_site_describes_setup_and_top_up(self):
        for rel in ("site/index.html", "site/skills/tokenfish/index.html"):
            text = self.read(rel)
            with self.subTest(file=rel):
                self.assertIn("免注册", text)
                self.assertIn("余额为 0", text)
                self.assertIn("充值 10 元", text)
                self.assertNotIn("登录一次", text)
        llms = self.read("site/llms.txt")
        for needle in ("`setup`", "免注册", "充值 N 元", "login --key-file"):
            self.assertIn(needle, llms)

    def test_P11_readme_and_spec_appendix_cover_v0_2(self):
        readme = self.read("README.md")
        for needle in ("## v0.2", "setup", "tokenfish.wallet.topup", "tokenfish.wallet.topup_status", "play.html", "pay.html"):
            self.assertIn(needle, readme)
        spec = self.read("SPEC.md")
        self.assertIn("## 附：v0.2 对话内开钱包、充值与回链（2026-10-03）", spec)
        appendix = spec.split("## 附：v0.2 对话内开钱包、充值与回链（2026-10-03）", 1)[1]
        for needle in ("不送体验金", "整数元", "int64(amount*100)", "POST 请求体", "play.html", "channel_unavailable", "order_not_found"):
            self.assertIn(needle, appendix)


if __name__ == "__main__":
    unittest.main()
