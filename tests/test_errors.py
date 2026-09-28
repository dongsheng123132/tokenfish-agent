import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from fake_server import FakeBackend  # noqa: E402
from testutil import import_client_module, make_isolated_home  # noqa: E402


class TestErrorNormalization(unittest.TestCase):
    def setUp(self):
        self.home = make_isolated_home()
        self.backend = FakeBackend()
        self.base_url = self.backend.start()
        os.environ["TOKENFISH_API_BASE"] = self.base_url
        self.tf = import_client_module()
        self.key = "sk-errtestkey0001"

    def tearDown(self):
        self.backend.stop()

    def _music_args(self, crid):
        return {"prompt": "synthwave", "model": "minimax", "client_request_id": crid}

    def test_402_insufficient_quota_maps_to_insufficient_balance(self):
        self.backend.set_response(
            "POST",
            "/v1/music/generations",
            402,
            {"error": {"type": "insufficient_quota", "message": "not enough quota"}},
        )
        with self.assertRaises(self.tf.ToolError) as cm:
            self.tf.tool_music_generate(self._music_args("crid-402"), self.key)
        self.assertEqual(cm.exception.code, "insufficient_balance")
        self.assertEqual(cm.exception.exit_code, 3)

    def test_401_with_balance_le_zero_maps_to_insufficient_balance(self):
        self.backend.set_response("POST", "/v1/music/generations", 401, {"error": "Invalid token"})
        self.backend.set_response(
            "GET",
            "/api/usage/token/",
            200,
            {"code": True, "data": {"name": "u", "total_available": -12345, "total_used": 0, "total_granted": 0}},
        )
        with self.assertRaises(self.tf.ToolError) as cm:
            self.tf.tool_music_generate(self._music_args("crid-401-neg"), self.key)
        self.assertEqual(cm.exception.code, "insufficient_balance")
        self.assertEqual(cm.exception.exit_code, 3)

    def test_401_with_balance_gt_zero_maps_to_auth_invalid(self):
        self.backend.set_response("POST", "/v1/music/generations", 401, {"error": "Invalid token"})
        self.backend.set_response(
            "GET",
            "/api/usage/token/",
            200,
            {"code": True, "data": {"name": "u", "total_available": 200000, "total_used": 0, "total_granted": 0}},
        )
        with self.assertRaises(self.tf.ToolError) as cm:
            self.tf.tool_music_generate(self._music_args("crid-401-pos"), self.key)
        self.assertEqual(cm.exception.code, "auth_invalid")
        self.assertEqual(cm.exception.exit_code, 3)

    def test_429_maps_to_rate_limited_and_retryable(self):
        self.backend.set_response("POST", "/v1/music/generations", 429, {"error": {"message": "slow down"}})
        with self.assertRaises(self.tf.ToolError) as cm:
            self.tf.tool_music_generate(self._music_args("crid-429"), self.key)
        self.assertEqual(cm.exception.code, "rate_limited")
        self.assertTrue(cm.exception.retryable)
        self.assertEqual(cm.exception.exit_code, 4)

    def test_5xx_maps_to_transient_and_retryable(self):
        self.backend.set_response("POST", "/v1/music/generations", 503, {"error": {"message": "down"}})
        with self.assertRaises(self.tf.ToolError) as cm:
            self.tf.tool_music_generate(self._music_args("crid-503"), self.key)
        self.assertEqual(cm.exception.code, "transient")
        self.assertTrue(cm.exception.retryable)
        self.assertEqual(cm.exception.exit_code, 4)


if __name__ == "__main__":
    unittest.main()
