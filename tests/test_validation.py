import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from testutil import import_client_module, make_isolated_home  # noqa: E402


class TestMusicValidation(unittest.TestCase):
    def setUp(self):
        self.home = make_isolated_home()
        # Point at an address nothing listens on: if validation didn't run
        # before the network call, these tests would fail with a *different*
        # error (transient/exit 4) instead of invalid_arguments/exit 2 --
        # which is exactly how we prove "rejected before the request is sent".
        import os

        os.environ["TOKENFISH_API_BASE"] = "http://127.0.0.1:1"
        self.tf = import_client_module()

    def test_instrumental_with_lyrics_rejected(self):
        with self.assertRaises(self.tf.ToolError) as cm:
            self.tf.validate_music_args(
                {
                    "prompt": "upbeat pop",
                    "lyrics": "la la la",
                    "instrumental": True,
                    "client_request_id": "req-1",
                }
            )
        self.assertEqual(cm.exception.code, "invalid_arguments")
        self.assertEqual(cm.exception.exit_code, 2)
        self.assertIn("lyrics", cm.exception.message)

    def test_instrumental_with_lyrics_rejected_end_to_end_before_network(self):
        key = "sk-doesnotmatter"
        with self.assertRaises(self.tf.ToolError) as cm:
            self.tf.tool_music_generate(
                {
                    "prompt": "upbeat pop",
                    "lyrics": "la la la",
                    "instrumental": True,
                    "client_request_id": "req-1b",
                },
                key,
            )
        self.assertEqual(cm.exception.code, "invalid_arguments")
        self.assertEqual(cm.exception.exit_code, 2)

    def test_duration_out_of_range_rejected(self):
        for bad in (0, -1, 301, 10000):
            with self.assertRaises(self.tf.ToolError) as cm:
                self.tf.validate_music_args(
                    {
                        "prompt": "chill lofi",
                        "duration": bad,
                        "client_request_id": f"req-dur-{bad}",
                    }
                )
            self.assertEqual(cm.exception.code, "invalid_arguments")
            self.assertIn("duration", cm.exception.message)

    def test_duration_out_of_range_rejected_before_network_call(self):
        key = "sk-doesnotmatter"
        with self.assertRaises(self.tf.ToolError) as cm:
            self.tf.tool_music_generate(
                {
                    "prompt": "chill lofi",
                    "duration": 301,
                    "client_request_id": "req-dur-network",
                },
                key,
            )
        self.assertEqual(cm.exception.code, "invalid_arguments")
        self.assertEqual(cm.exception.exit_code, 2)

    def test_duration_in_range_accepted_by_validator(self):
        clean = self.tf.validate_music_args(
            {"prompt": "chill lofi", "duration": 30, "client_request_id": "req-dur-ok"}
        )
        self.assertEqual(clean["duration"], 30)

    def test_missing_prompt_rejected(self):
        with self.assertRaises(self.tf.ToolError) as cm:
            self.tf.validate_music_args({"client_request_id": "req-noprompt"})
        self.assertEqual(cm.exception.code, "invalid_arguments")

    def test_missing_client_request_id_rejected(self):
        with self.assertRaises(self.tf.ToolError) as cm:
            self.tf.validate_music_args({"prompt": "pop"})
        self.assertEqual(cm.exception.code, "invalid_arguments")

    def test_invalid_model_rejected(self):
        with self.assertRaises(self.tf.ToolError) as cm:
            self.tf.validate_music_args(
                {"prompt": "pop", "client_request_id": "req-badmodel", "model": "udio"}
            )
        self.assertEqual(cm.exception.code, "invalid_arguments")


class TestImagesValidation(unittest.TestCase):
    def setUp(self):
        self.home = make_isolated_home()
        self.tf = import_client_module()

    def test_n_out_of_range_rejected(self):
        for bad in (0, 5, -1):
            with self.assertRaises(self.tf.ToolError) as cm:
                self.tf.validate_images_args(
                    {"prompt": "a cat", "n": bad, "client_request_id": f"req-n-{bad}"}
                )
            self.assertEqual(cm.exception.code, "invalid_arguments")

    def test_invalid_quality_rejected(self):
        with self.assertRaises(self.tf.ToolError) as cm:
            self.tf.validate_images_args(
                {"prompt": "a cat", "quality": "ultra", "client_request_id": "req-q"}
            )
        self.assertEqual(cm.exception.code, "invalid_arguments")

    def test_defaults_applied(self):
        clean = self.tf.validate_images_args({"prompt": "a cat", "client_request_id": "req-defaults"})
        self.assertEqual(clean["n"], 1)
        self.assertEqual(clean["size"], "1024x1024")
        self.assertEqual(clean["quality"], "auto")


class TestModelsListValidation(unittest.TestCase):
    def setUp(self):
        self.home = make_isolated_home()
        self.tf = import_client_module()

    def test_missing_capability_lists_valid_values(self):
        with self.assertRaises(self.tf.ToolError) as cm:
            self.tf.tool_models_list({}, "sk-whatever")
        self.assertEqual(cm.exception.code, "invalid_arguments")
        self.assertIn("text_to_music", cm.exception.message)
        self.assertIn("text_to_image", cm.exception.message)

    def test_invalid_capability_lists_valid_values(self):
        with self.assertRaises(self.tf.ToolError) as cm:
            self.tf.tool_models_list({"capability": "text_to_video"}, "sk-whatever")
        self.assertIn("text_to_music", cm.exception.message)

    def test_valid_music_capability(self):
        result = self.tf.tool_models_list({"capability": "text_to_music"}, "sk-whatever")
        self.assertEqual(result["models"][0]["id"], "auto")
        self.assertEqual(result["models"][0]["routing_order"], ["suno", "minimax"])

    def test_valid_image_capability(self):
        result = self.tf.tool_models_list({"capability": "text_to_image"}, "sk-whatever")
        self.assertEqual(result["models"][0]["id"], "gpt-image-2")


if __name__ == "__main__":
    unittest.main()
