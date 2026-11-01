"""text-to-video (tokenfish.video.generate / tokenfish.tasks.get provider=video).

Covers SPEC.md §2.3-§2.6 and the "附：防重复扣费" appendix as applied to the
video path:
  - argument validation happens BEFORE any request is sent
  - submit -> poll -> download -> on-disk MP4 (with ftyp sanity check)
  - bounded polling: no terminal status -> status "running" + task_id, never an
    error and never a second submit
  - a 2xx JSON error body -> outcome_unknown (not retryable, no resubmit)
  - client-side idempotency: same client_request_id + same args -> cached
    result, different args -> idempotency_conflict
  - tasks.get supports provider "video" and downloads on success
"""

import json
import os
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from fake_server import FakeBackend  # noqa: E402
from testutil import SYSTEM_TMP, import_client_module, make_isolated_home  # noqa: E402

VIDEO_SUBMIT = ("POST", "/v1/video/generations")
KEY = "sk-videotestkey0001"
MODEL = "doubao-seedance-2-0-mini-260615"

# A small but structurally plausible MP4: an `ftyp` box at offset 4, padded
# over the 1 KB floor enforced by the client.
MP4_BYTES = b"\x00\x00\x00\x18ftypisom\x00\x00\x02\x00isomiso2mp41" + b"\x00" * 4096


class _VideoBase(unittest.TestCase):
    def setUp(self):
        self.home = make_isolated_home()
        self.cwd = Path(tempfile.mkdtemp(prefix="tokenfish-video-cwd-", dir=SYSTEM_TMP))
        self._old_cwd = os.getcwd()
        self.addCleanup(shutil.rmtree, self.cwd, True)
        self.addCleanup(shutil.rmtree, self.home, True)
        self.addCleanup(os.chdir, self._old_cwd)
        os.chdir(self.cwd)

        self.backend = FakeBackend()
        self.base_url = self.backend.start()
        self.addCleanup(self.backend.stop)
        os.environ["TOKENFISH_API_BASE"] = self.base_url

        cred_dir = self.home / ".tokenfish"
        cred_dir.mkdir(parents=True)
        (cred_dir / "credentials.json").write_text(json.dumps({"key": KEY}), encoding="utf-8")

        self.tf = import_client_module()
        # Fast polling for tests: 1 ms between polls, ~50 ms budget.
        self.tf.VIDEO_POLL_INTERVAL = 0.001
        self.tf.VIDEO_POLL_TIMEOUT = 0.05

    # -- helpers ---------------------------------------------------------

    def video(self, crid, **extra):
        args = {"prompt": "一只橘猫在窗台上打哈欠，阳光洒进来，写实风格", "client_request_id": crid}
        args.update(extra)
        return self.tf.cmd_call("tokenfish.video.generate", json.dumps(args))

    def submit_count(self):
        return sum(1 for r in self.backend.requests if r["method"] == "POST" and r["path"] == VIDEO_SUBMIT[1])

    def status_count(self, task_id):
        path = f"/v1/video/generations/{task_id}"
        return sum(1 for r in self.backend.requests if r["method"] == "GET" and r["path"] == path)

    def route_download(self, url_path="/video.mp4"):
        url = self.base_url + url_path
        self.backend.set_bytes("GET", url_path, 200, MP4_BYTES, "video/mp4")
        return url

    def route_status(self, task_id, payload, download_url=None):
        def handler(_method, _path, _headers, _body):
            body = dict(payload)
            if download_url is not None:
                body["result_url"] = download_url
            return 200, body

        self.backend.set_handler("GET", f"/v1/video/generations/{task_id}", handler)


class TestVideoValidation(_VideoBase):
    def test_duration_out_of_range_rejected_before_any_request(self):
        for bad in (4, 16, 0, -5):
            with self.assertRaises(self.tf.ToolError) as cm:
                self.tf.validate_video_args(
                    {"prompt": "a cat", "duration": bad, "client_request_id": f"crid-dur-{bad}"}
                )
            self.assertEqual(cm.exception.code, "invalid_arguments")
            self.assertEqual(cm.exception.exit_code, 2)
            self.assertIn("duration", cm.exception.message)
            self.assertIn("5..15", cm.exception.message)
        for bad in ("5", 5.5, True):
            with self.assertRaises(self.tf.ToolError) as cm:
                self.tf.validate_video_args(
                    {"prompt": "a cat", "duration": bad, "client_request_id": f"crid-dur-{bad}"}
                )
            self.assertEqual(cm.exception.code, "invalid_arguments")
            self.assertIn("整数", cm.exception.message)
        self.assertEqual(self.backend.requests, [])

    def test_resolution_invalid_lists_all_values(self):
        with self.assertRaises(self.tf.ToolError) as cm:
            self.tf.validate_video_args(
                {"prompt": "a cat", "resolution": "4k", "client_request_id": "crid-res-bad"}
            )
        self.assertEqual(cm.exception.code, "invalid_arguments")
        for value in ("480p", "720p", "1080p"):
            self.assertIn(value, cm.exception.message)

    def test_model_invalid_lists_all_five_models(self):
        with self.assertRaises(self.tf.ToolError) as cm:
            self.tf.validate_video_args(
                {"prompt": "a cat", "model": "veo-3", "client_request_id": "crid-model-bad"}
            )
        self.assertEqual(cm.exception.code, "invalid_arguments")
        for model in self.tf.VIDEO_MODELS:
            self.assertIn(model, cm.exception.message)

    def test_prompt_required(self):
        with self.assertRaises(self.tf.ToolError) as cm:
            self.tf.validate_video_args({"client_request_id": "crid-no-prompt"})
        self.assertEqual(cm.exception.code, "invalid_arguments")
        self.assertIn("prompt", cm.exception.message)

    def test_prompt_length_capped(self):
        with self.assertRaises(self.tf.ToolError) as cm:
            self.tf.validate_video_args({"prompt": "x" * 2001, "client_request_id": "crid-long"})
        self.assertEqual(cm.exception.code, "invalid_arguments")
        self.assertIn("2000", cm.exception.message)

    def test_client_request_id_required(self):
        with self.assertRaises(self.tf.ToolError) as cm:
            self.tf.validate_video_args({"prompt": "a cat"})
        self.assertEqual(cm.exception.code, "invalid_arguments")
        self.assertIn("client_request_id", cm.exception.message)

    def test_defaults_and_estimate(self):
        clean = self.tf.validate_video_args({"prompt": "a cat", "client_request_id": "crid-defaults"})
        self.assertEqual(clean["duration"], 5)
        self.assertEqual(clean["resolution"], "480p")
        self.assertEqual(clean["model"], MODEL)
        self.assertEqual(self.tf.estimate_video_cost_cny(MODEL, 5, "480p"), 2.9)
        self.assertEqual(self.tf.estimate_video_cost_cny(MODEL, 10, "480p"), 5.8)
        self.assertEqual(self.tf.estimate_video_cost_cny(MODEL, 5, "720p"), 4.35)
        self.assertEqual(self.tf.estimate_video_cost_cny(MODEL, 15, "1080p"), 21.75)
        self.assertIsNone(self.tf.estimate_video_cost_cny("wanx2.1-t2v-turbo", 5, "480p"))

    def test_tool_rejects_bad_enum_without_touching_network(self):
        payload, code = self.video("crid-bad-res", resolution="8k")
        self.assertEqual(code, 2)
        self.assertEqual(payload["error"]["code"], "invalid_arguments")
        self.assertEqual(self.backend.requests, [])


class TestVideoSubmitPollDownload(_VideoBase):
    def test_submit_poll_success_downloads_mp4(self):
        download_url = self.route_download()
        calls = {"n": 0}

        def status_handler(_method, _path, _headers, _body):
            calls["n"] += 1
            if calls["n"] == 1:
                return 200, {"data": {"status": "IN_PROGRESS", "progress": 30}}
            return 200, {"data": {"status": "SUCCESS", "progress": 100, "result_url": download_url}}

        self.backend.set_response("POST", VIDEO_SUBMIT[1], 200, {"task_id": "vt-success"})
        self.backend.set_handler("GET", "/v1/video/generations/vt-success", status_handler)

        payload, code = self.video("crid-video-ok", duration=5, resolution="480p")
        self.assertEqual(code, 0)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["provider"], "video")
        self.assertEqual(payload["status"], "succeeded")
        self.assertEqual(payload["task_id"], "vt-success")
        self.assertEqual(payload["client_request_id"], "crid-video-ok")
        self.assertEqual(payload["video_url"], download_url)
        self.assertEqual(payload["estimated_cost_cny"], 2.9)
        self.assertEqual(payload["cost_note"], "估算，实扣以钱包流水为准")
        self.assertIsNone(payload["charged_cny"])
        self.assertIn("links", payload)
        self.assertIn("topup_hint", payload)

        out_path = Path(payload["file"])
        self.assertTrue(out_path.is_file())
        self.assertEqual(out_path.name, "crid-video-ok.mp4")
        self.assertEqual(out_path.read_bytes(), MP4_BYTES)
        self.assertIn(b"ftyp", out_path.read_bytes()[:64])

        # The submit body matches the documented backend contract.
        submitted = [r for r in self.backend.requests if r["method"] == "POST"][0]
        self.assertEqual(
            submitted["body"],
            {
                "model": MODEL,
                "prompt": "一只橘猫在窗台上打哈欠，阳光洒进来，写实风格",
                "duration": 5,
                "resolution": "480p",
            },
        )
        self.assertEqual(submitted["headers"].get("Authorization"), f"Bearer {KEY}")
        # The download is authenticated too (CDN requires the Bearer token).
        download = [r for r in self.backend.requests if r["method"] == "GET" and r["path"] == "/video.mp4"][0]
        self.assertEqual(download["headers"].get("Authorization"), f"Bearer {KEY}")
        # Polled at least twice (running -> succeeded), exactly one submit.
        self.assertGreaterEqual(self.status_count("vt-success"), 2)
        self.assertEqual(self.submit_count(), 1)

    def test_accepts_id_instead_of_task_id_and_flat_status_payload(self):
        download_url = self.route_download("/flat.mp4")
        self.backend.set_response("POST", VIDEO_SUBMIT[1], 200, {"id": "vt-flat"})
        # Status flattened at the top level (no "data" wrapper) + SUCCEED spelling.
        self.backend.set_response(
            "GET", "/v1/video/generations/vt-flat", 200, {"status": "SUCCEED", "result_url": download_url}
        )
        payload, code = self.video("crid-video-flat")
        self.assertEqual(code, 0)
        self.assertEqual(payload["task_id"], "vt-flat")
        self.assertEqual(payload["status"], "succeeded")
        self.assertTrue(Path(payload["file"]).is_file())

    def test_failed_status_is_reported_with_reason(self):
        self.backend.set_response("POST", VIDEO_SUBMIT[1], 200, {"task_id": "vt-fail"})
        self.backend.set_response(
            "GET",
            "/v1/video/generations/vt-fail",
            200,
            {"data": {"status": "FAILURE", "fail_reason": "内容审核未通过"}},
        )
        payload, code = self.video("crid-video-fail")
        self.assertEqual(code, 0)
        self.assertEqual(payload["status"], "failed")
        self.assertEqual(payload["fail_reason"], "内容审核未通过")
        self.assertIsNone(payload["file"])
        self.assertEqual(self.submit_count(), 1)


class TestVideoPollingTimeout(_VideoBase):
    def test_timeout_returns_running_with_task_id_without_resubmitting(self):
        self.backend.set_response("POST", VIDEO_SUBMIT[1], 200, {"task_id": "vt-slow"})
        self.backend.set_response(
            "GET", "/v1/video/generations/vt-slow", 200, {"data": {"status": "IN_PROGRESS", "progress": 42}}
        )

        payload, code = self.video("crid-video-slow")
        self.assertEqual(code, 0)  # no error: a still-running task is a normal result
        self.assertEqual(payload["status"], "running")
        self.assertEqual(payload["task_id"], "vt-slow")
        self.assertEqual(payload["provider"], "video")
        self.assertEqual(payload["progress"], 42)
        self.assertIn("tokenfish.tasks.get", payload["warning"])
        self.assertIsNone(payload["file"])
        self.assertEqual(self.submit_count(), 1)

        # ... and the same client_request_id must not submit again either.
        again, code = self.video("crid-video-slow")
        self.assertEqual(code, 0)
        self.assertEqual(again["task_id"], "vt-slow")
        self.assertEqual(self.submit_count(), 1)

        # tasks.get finishes the job once the video is ready.
        download_url = self.route_download("/late.mp4")
        self.backend.set_response(
            "GET",
            "/v1/video/generations/vt-slow",
            200,
            {"data": {"status": "SUCCESS", "result_url": download_url}},
        )
        finished, code = self.tf.cmd_call("tokenfish.tasks.get", json.dumps({"task_id": "vt-slow"}))
        self.assertEqual(code, 0)
        self.assertEqual(finished["status"], "succeeded")
        self.assertEqual(finished["video_url"], download_url)
        self.assertTrue(Path(finished["file"]).is_file())
        self.assertEqual(Path(finished["file"]).name, "crid-video-slow.mp4")


class TestVideoNoDoubleCharge(_VideoBase):
    def test_2xx_json_error_body_is_outcome_unknown_and_never_retried(self):
        self.backend.set_response(
            "POST", VIDEO_SUBMIT[1], 200, {"error": {"message": "prompt blocked"}, "code": "failed"}
        )
        args = {"prompt": "a cat", "client_request_id": "crid-video-json"}
        payload, code = self.tf.cmd_call("tokenfish.video.generate", json.dumps(args))
        self.assertEqual(code, 3)
        self.assertEqual(payload["error"]["code"], "outcome_unknown")
        self.assertFalse(payload["error"]["retryable"])
        self.assertEqual(self.submit_count(), 1)

        # Replaying the same client_request_id must not submit again.
        payload2, code2 = self.tf.cmd_call("tokenfish.video.generate", json.dumps(args))
        self.assertEqual(code2, 3)
        self.assertEqual(payload2["error"]["code"], "outcome_unknown")
        self.assertEqual(self.submit_count(), 1)

    def test_invalid_download_removes_file_and_never_resubmits(self):
        # result_url points at a 200 JSON body (the classic CDN/auth failure).
        self.backend.set_response("POST", VIDEO_SUBMIT[1], 200, {"task_id": "vt-badfile"})
        self.backend.set_response(
            "GET",
            "/v1/video/generations/vt-badfile",
            200,
            {"data": {"status": "SUCCESS", "result_url": self.base_url + "/not-a-video"}},
        )
        self.backend.set_response("GET", "/not-a-video", 200, {"error": "expired"})

        payload, code = self.video("crid-video-badfile")
        self.assertEqual(code, 3)
        self.assertEqual(payload["error"]["code"], "download_failed")
        self.assertFalse(payload["error"]["retryable"])
        self.assertIn("vt-badfile", payload["error"]["message"])
        self.assertEqual(self.submit_count(), 1)
        self.assertFalse((self.cwd / "tokenfish-outputs" / "crid-video-badfile.mp4").exists())
        self.assertFalse((self.cwd / "tokenfish-outputs" / ".crid-video-badfile.part").exists())

    def test_truncated_mp4_is_rejected(self):
        self.backend.set_response("POST", VIDEO_SUBMIT[1], 200, {"task_id": "vt-tiny"})
        self.backend.set_bytes("GET", "/tiny.mp4", 200, b"not-an-mp4", "video/mp4")
        self.backend.set_response(
            "GET",
            "/v1/video/generations/vt-tiny",
            200,
            {"data": {"status": "SUCCESS", "result_url": self.base_url + "/tiny.mp4"}},
        )
        payload, code = self.video("crid-video-tiny")
        self.assertEqual(code, 3)
        self.assertEqual(payload["error"]["code"], "download_failed")
        self.assertEqual(self.submit_count(), 1)


class TestVideoIdempotency(_VideoBase):
    def test_same_crid_same_args_returns_cached_result_without_resubmitting(self):
        download_url = self.route_download("/idem.mp4")
        self.backend.set_response("POST", VIDEO_SUBMIT[1], 200, {"task_id": "vt-idem"})
        self.backend.set_response(
            "GET",
            "/v1/video/generations/vt-idem",
            200,
            {"data": {"status": "SUCCESS", "result_url": download_url}},
        )

        first, code = self.video("crid-video-idem", duration=8, resolution="720p")
        self.assertEqual(code, 0)
        self.assertEqual(first["status"], "succeeded")
        self.assertEqual(first["estimated_cost_cny"], 6.96)
        self.assertEqual(self.submit_count(), 1)

        second, code = self.video("crid-video-idem", duration=8, resolution="720p")
        self.assertEqual(code, 0)
        self.assertEqual(second["task_id"], "vt-idem")
        self.assertEqual(second["file"], first["file"])
        self.assertEqual(self.submit_count(), 1)
        self.assertTrue(Path(second["file"]).is_file())

    def test_same_crid_different_args_is_idempotency_conflict(self):
        download_url = self.route_download("/conflict.mp4")
        self.backend.set_response("POST", VIDEO_SUBMIT[1], 200, {"task_id": "vt-conflict"})
        self.backend.set_response(
            "GET",
            "/v1/video/generations/vt-conflict",
            200,
            {"data": {"status": "SUCCESS", "result_url": download_url}},
        )
        _, code = self.video("crid-video-conflict")
        self.assertEqual(code, 0)

        payload, code = self.video("crid-video-conflict", prompt="另一只猫")
        self.assertEqual(code, 2)
        self.assertEqual(payload["error"]["code"], "idempotency_conflict")
        self.assertEqual(self.submit_count(), 1)


class TestVideoTasksGetProvider(_VideoBase):
    def test_tasks_get_with_provider_video_without_local_record(self):
        download_url = self.route_download("/adopted.mp4")
        self.backend.set_response(
            "GET",
            "/v1/video/generations/vt-adopted",
            200,
            {"data": {"status": "SUCCESS", "result_url": download_url}},
        )
        payload, code = self.tf.cmd_call(
            "tokenfish.tasks.get", json.dumps({"task_id": "vt-adopted", "provider": "video"})
        )
        self.assertEqual(code, 0)
        self.assertEqual(payload["provider"], "video")
        self.assertEqual(payload["status"], "succeeded")
        self.assertEqual(payload["video_url"], download_url)
        self.assertTrue(Path(payload["file"]).is_file())
        self.assertEqual(Path(payload["file"]).name, "vt-adopted.mp4")


if __name__ == "__main__":
    unittest.main()
