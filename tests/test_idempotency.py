import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from fake_server import FakeBackend  # noqa: E402
from testutil import import_client_module, make_isolated_home  # noqa: E402


class TestSunoIdempotency(unittest.TestCase):
    def setUp(self):
        self.home = make_isolated_home()
        self.backend = FakeBackend()
        self.base_url = self.backend.start()
        os.environ["TOKENFISH_API_BASE"] = self.base_url
        self.tf = import_client_module()
        self.key = "sk-idemtestkey001"

    def tearDown(self):
        self.backend.stop()

    def _submit_count(self):
        return sum(1 for r in self.backend.requests if r["method"] == "POST" and r["path"] == "/suno/submit/music")

    def test_same_client_request_id_same_args_returns_existing_task_without_resubmitting(self):
        self.backend.set_response("POST", "/suno/submit/music", 200, {"code": "success", "data": "task-abc"})
        self.backend.set_response(
            "GET",
            "/suno/fetch/task-abc",
            200,
            {"data": {"status": "IN_PROGRESS", "data": []}},
        )

        args = {"prompt": "city pop", "model": "suno", "client_request_id": "crid-same"}

        first = self.tf.tool_music_generate(dict(args), self.key)
        self.assertEqual(first["task_id"], "task-abc")
        self.assertEqual(first["provider"], "suno")
        self.assertEqual(self._submit_count(), 1)

        second = self.tf.tool_music_generate(dict(args), self.key)
        self.assertEqual(second["task_id"], "task-abc")
        # No resubmission happened.
        self.assertEqual(self._submit_count(), 1)
        # But status was refreshed via the fetch endpoint (queued -> running).
        self.assertEqual(second["status"], "running")

    def test_same_client_request_id_different_args_conflicts_without_resubmitting(self):
        self.backend.set_response("POST", "/suno/submit/music", 200, {"code": "success", "data": "task-xyz"})

        first_args = {"prompt": "city pop", "model": "suno", "client_request_id": "crid-conflict"}
        self.tf.tool_music_generate(dict(first_args), self.key)
        self.assertEqual(self._submit_count(), 1)

        second_args = {"prompt": "heavy metal", "model": "suno", "client_request_id": "crid-conflict"}
        with self.assertRaises(self.tf.ToolError) as cm:
            self.tf.tool_music_generate(dict(second_args), self.key)
        self.assertEqual(cm.exception.code, "idempotency_conflict")
        # Still no second submission.
        self.assertEqual(self._submit_count(), 1)


class TestMinimaxIdempotencyKeyCompliance(unittest.TestCase):
    def setUp(self):
        self.home = make_isolated_home()
        self.backend = FakeBackend()
        self.base_url = self.backend.start()
        os.environ["TOKENFISH_API_BASE"] = self.base_url
        self.tf = import_client_module()
        self.key = "sk-idemtestkey002"

    def tearDown(self):
        self.backend.stop()

    def test_compliant_client_request_id_passed_through_unchanged(self):
        compliant = "abcDEF012345"  # 12 chars, allowed charset
        self.assertRegex(compliant, self.tf.IDEMPOTENCY_KEY_RE)
        self.assertEqual(self.tf.make_idempotency_key(compliant), compliant)

    def test_noncompliant_client_request_id_is_hashed(self):
        for bad in ("short", "has spaces here", "包含中文字符超过限制的id", "a" * 5):
            key = self.tf.make_idempotency_key(bad)
            self.assertRegex(key, self.tf.IDEMPOTENCY_KEY_RE)
            self.assertNotEqual(key, bad)
            self.assertEqual(len(key), 64)  # sha256 hex digest

    def test_end_to_end_noncompliant_id_sent_as_hash_to_minimax(self):
        self.backend.set_response(
            "POST",
            "/v1/music/generations",
            200,
            {"id": "mm-1", "object": "music", "model": "minimax-music-v2.6", "status": "succeeded", "output_url": "https://example.invalid/a.mp3"},
        )
        crid = "id with spaces not compliant"
        result = self.tf.tool_music_generate(
            {"prompt": "ambient", "model": "minimax", "client_request_id": crid}, self.key
        )
        self.assertEqual(result["provider"], "minimax")
        minimax_requests = [r for r in self.backend.requests if r["path"] == "/v1/music/generations"]
        self.assertEqual(len(minimax_requests), 1)
        sent_key = minimax_requests[0]["body"]["idempotency_key"]
        self.assertNotEqual(sent_key, crid)
        self.assertRegex(sent_key, self.tf.IDEMPOTENCY_KEY_RE)
        self.assertEqual(sent_key, self.tf.make_idempotency_key(crid))


if __name__ == "__main__":
    unittest.main()
