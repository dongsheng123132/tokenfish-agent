import json
import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from testutil import import_client_module, make_isolated_home  # noqa: E402


class TestCredentialPriority(unittest.TestCase):
    def setUp(self):
        self.home = make_isolated_home()
        self.tf = import_client_module()

    def test_no_credentials_returns_none(self):
        self.assertIsNone(self.tf.load_credentials())

    def test_uking_fallback_when_no_tokenfish_credentials(self):
        uking_dir = self.home / ".uking"
        uking_dir.mkdir(parents=True, exist_ok=True)
        (uking_dir / "device.json").write_text(json.dumps({"key": "sk-fromuking0001"}), encoding="utf-8")
        self.assertEqual(self.tf.load_credentials(), "sk-fromuking0001")
        # Must never write ~/.tokenfish/credentials.json as a side effect of the fallback read.
        self.assertFalse((self.home / ".tokenfish" / "credentials.json").exists())

    def test_tokenfish_credentials_take_priority_over_uking(self):
        uking_dir = self.home / ".uking"
        uking_dir.mkdir(parents=True, exist_ok=True)
        (uking_dir / "device.json").write_text(json.dumps({"key": "sk-fromuking0001"}), encoding="utf-8")

        tf_dir = self.home / ".tokenfish"
        tf_dir.mkdir(parents=True, exist_ok=True)
        (tf_dir / "credentials.json").write_text(json.dumps({"key": "sk-fromtokenfish"}), encoding="utf-8")

        self.assertEqual(self.tf.load_credentials(), "sk-fromtokenfish")

    def test_save_credentials_roundtrip_and_never_touches_uking(self):
        self.tf.save_credentials("sk-savedkey00001")
        self.assertEqual(self.tf.load_credentials(), "sk-savedkey00001")
        self.assertFalse((self.home / ".uking").exists())

    def test_save_credentials_permissions_posix(self):
        if os.name == "nt":
            self.skipTest("POSIX-only permission check")
        self.tf.save_credentials("sk-permtest0001")
        mode = self.tf.CREDENTIALS_PATH.stat().st_mode & 0o777
        self.assertEqual(mode, 0o600)


class TestMaskKey(unittest.TestCase):
    def setUp(self):
        self.home = make_isolated_home()
        self.tf = import_client_module()

    def test_mask_typical_key(self):
        masked = self.tf.mask_key("sk-1234567890abcdef")
        self.assertTrue(masked.startswith("sk-"))
        self.assertTrue(masked.endswith("cdef"))
        self.assertNotIn("1234567890ab", masked)

    def test_mask_never_reveals_full_short_key(self):
        masked = self.tf.mask_key("abcd")
        self.assertNotEqual(masked, "abcd")

    def test_mask_empty(self):
        self.assertEqual(self.tf.mask_key(""), "****")


if __name__ == "__main__":
    unittest.main()
