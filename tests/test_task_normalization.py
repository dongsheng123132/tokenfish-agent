import os
import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from fake_server import FakeBackend  # noqa: E402
from testutil import import_client_module, make_isolated_home  # noqa: E402


class TestStatusNormalizationUnit(unittest.TestCase):
    def setUp(self):
        self.home = make_isolated_home()
        self.tf = import_client_module()

    def test_suno_status_mapping(self):
        self.assertEqual(self.tf.normalize_suno_status("SUCCESS"), "succeeded")
        self.assertEqual(self.tf.normalize_suno_status("FAILURE"), "failed")
        self.assertEqual(self.tf.normalize_suno_status("IN_PROGRESS"), "running")
        self.assertEqual(self.tf.normalize_suno_status("SOMETHING_ELSE"), "queued")
        self.assertEqual(self.tf.normalize_suno_status(None), "queued")

    def test_minimax_status_mapping(self):
        self.assertEqual(self.tf.normalize_minimax_status("processing", False), "running")
        self.assertEqual(self.tf.normalize_minimax_status("failed", False), "failed")
        self.assertEqual(self.tf.normalize_minimax_status("queued", False), "queued")
        # Presence of output_url always wins -> succeeded, regardless of raw status text.
        self.assertEqual(self.tf.normalize_minimax_status("anything", True), "succeeded")

    def test_build_suno_fetch_task_object_shape(self):
        resp = {
            "data": {
                "status": "SUCCESS",
                "data": [
                    {"audio_url": "https://x/1.mp3", "title": "A", "duration": 120},
                    {"audio_url": "https://x/2.mp3", "title": "B", "duration": 118},
                ],
            }
        }
        obj = self.tf.build_suno_fetch_task_object(resp, "task-1", "crid-1")
        self.assertEqual(obj["status"], "succeeded")
        self.assertEqual(obj["provider"], "suno")
        self.assertEqual(len(obj["clips"]), 2)
        self.assertEqual(obj["clips"][0]["audio_url"], "https://x/1.mp3")
        self.assertIsNone(obj["charged_cny"])

    def test_build_minimax_task_object_shape_with_charge(self):
        resp = {
            "id": "mm-9",
            "status": "succeeded",
            "output_url": "https://x/song.mp3",
            "quota_charged": 250000,
            "title": "T",
            "duration": 90,
        }
        obj = self.tf.build_minimax_task_object(resp, "crid-9")
        self.assertEqual(obj["provider"], "minimax")
        self.assertEqual(obj["status"], "succeeded")
        self.assertEqual(obj["clips"][0]["audio_url"], "https://x/song.mp3")
        self.assertEqual(obj["charged_cny"], 0.5)

    def test_build_minimax_task_object_no_charge_field_is_null(self):
        resp = {"id": "mm-10", "status": "running"}
        obj = self.tf.build_minimax_task_object(resp, "crid-10")
        self.assertIsNone(obj["charged_cny"])
        self.assertEqual(obj["clips"], [])


class TestTasksGetIntegration(unittest.TestCase):
    def setUp(self):
        self.home = make_isolated_home()
        self.backend = FakeBackend()
        self.base_url = self.backend.start()
        os.environ["TOKENFISH_API_BASE"] = self.base_url
        self.tf = import_client_module()
        self.key = "sk-tasksgettest01"

    def tearDown(self):
        self.backend.stop()

    def test_tasks_get_normalizes_suno_task(self):
        self.backend.set_response("POST", "/suno/submit/music", 200, {"code": "success", "data": "task-s1"})
        self.tf.tool_music_generate(
            {"prompt": "jazz", "model": "suno", "client_request_id": "crid-s1"}, self.key
        )
        self.backend.set_response(
            "GET",
            "/suno/fetch/task-s1",
            200,
            {"data": {"status": "SUCCESS", "data": [{"audio_url": "https://x/a.mp3", "title": "A", "duration": 100}]}},
        )
        result = self.tf.tool_tasks_get({"task_id": "task-s1"}, self.key)
        self.assertEqual(result["status"], "succeeded")
        self.assertEqual(result["provider"], "suno")
        self.assertEqual(result["clips"][0]["audio_url"], "https://x/a.mp3")

    def test_tasks_get_normalizes_minimax_task(self):
        self.backend.set_response(
            "POST",
            "/v1/music/generations",
            200,
            {"id": "mm-t1", "status": "running"},
        )
        created = self.tf.tool_music_generate(
            {"prompt": "ambient", "model": "minimax", "client_request_id": "crid-mm1"}, self.key
        )
        self.assertEqual(created["status"], "running")

        self.backend.set_response(
            "GET",
            "/v1/music/generations/mm-t1",
            200,
            {"id": "mm-t1", "status": "succeeded", "output_url": "https://x/mm.mp3", "quota_charged": 500000},
        )
        result = self.tf.tool_tasks_get({"task_id": "mm-t1"}, self.key)
        self.assertEqual(result["status"], "succeeded")
        self.assertEqual(result["provider"], "minimax")
        self.assertEqual(result["charged_cny"], 1.0)

    def test_tasks_get_unknown_task_id_errors(self):
        with self.assertRaises(self.tf.ToolError) as cm:
            self.tf.tool_tasks_get({"task_id": "does-not-exist"}, self.key)
        self.assertEqual(cm.exception.code, "invalid_arguments")

    def test_tasks_list_returns_local_records_newest_first(self):
        self.backend.set_response("POST", "/suno/submit/music", 200, {"code": "success", "data": "task-l1"})
        self.tf.tool_music_generate(
            {"prompt": "one", "model": "suno", "client_request_id": "crid-l1"}, self.key
        )
        time.sleep(0.01)
        self.backend.set_response("POST", "/suno/submit/music", 200, {"code": "success", "data": "task-l2"})
        self.tf.tool_music_generate(
            {"prompt": "two", "model": "suno", "client_request_id": "crid-l2"}, self.key
        )
        listing = self.tf.tool_tasks_list({}, self.key)["tasks"]
        self.assertEqual(len(listing), 2)
        self.assertEqual(listing[0]["client_request_id"], "crid-l2")
        self.assertEqual(listing[1]["client_request_id"], "crid-l1")


if __name__ == "__main__":
    unittest.main()
