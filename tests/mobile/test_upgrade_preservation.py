"""Use only a temporary instance and synthetic keys to exercise real reopening paths."""
from concurrent.futures import ThreadPoolExecutor
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from config import Config
import util


class UpgradePreservationTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.data = Path(temp.name) / "data"
        self.data.mkdir()
        self.config = self.data / "config.json"
        self.key = self.data / ".secret_key"
        for name, value in (("DATA_DIR", str(self.data)), ("KEY_PATH", str(self.key))):
            override = patch.object(util, name, value)
            override.start()
            self.addCleanup(override.stop)

    def test_initialized_instance_reopens_with_existing_settings_and_key(self):
        first = Config(str(self.config))
        first.set("allow_groups", [190002])
        first.set("login_uin", "190001")
        key = self.key.read_bytes()
        (self.data / "runtime-ready.json").write_text('{"ready":true}')
        restarted = Config(str(self.config))
        self.assertEqual(restarted.get("allow_groups"), [190002])
        self.assertEqual(restarted.get("login_uin"), "190001")
        self.assertEqual(restarted.get("onebot_token"), first.get("onebot_token"))
        self.assertEqual(self.key.read_bytes(), key)

    def test_unchanged_restart_preserves_original_file_and_previous_backup(self):
        first = Config(str(self.config))
        first.set("login_uin", "190001")
        backup = self.config.with_suffix(".json.bak")
        old = backup.read_bytes()
        current = self.config.read_bytes()
        self.assertNotEqual(old, current)
        Config(str(self.config))
        self.assertEqual(self.config.read_bytes(), current)
        self.assertEqual(backup.read_bytes(), old)

    def test_restart_persists_new_defaults_and_corrected_values_only_once(self):
        Config(str(self.config))
        saved = json.loads(self.config.read_bytes())
        del saved["trigger_name"]
        saved["webhook_host"] = "invalid"
        self.config.write_text(json.dumps(saved), encoding="utf-8")
        original = self.config.read_bytes()
        Config(str(self.config))
        migrated = self.config.read_bytes()
        self.assertEqual(json.loads(migrated)["webhook_host"], "127.0.0.1")
        self.assertIn("trigger_name", json.loads(migrated))
        self.assertEqual(self.config.with_suffix(".json.bak").read_bytes(), original)
        Config(str(self.config))
        self.assertEqual(self.config.read_bytes(), migrated)
        self.assertEqual(self.config.with_suffix(".json.bak").read_bytes(), original)

    def test_legacy_plaintext_credentials_are_encrypted_on_first_reopen(self):
        first = Config(str(self.config))
        first.data["models"] = [{"id": "test", "name": "test", "base_url": "https://service.invalid/v1",
                                 "model": "test", "api_key": "synthetic-model-value", "enabled": True}]
        first.save()
        fields = ("api_key", "onebot_token", "admin_token", "alert_smtp_pass")
        for field in (*fields, "model_entry"):
            with self.subTest(field=field):
                saved = json.loads(self.config.read_bytes())
                entry = saved["models"][0] if field == "model_entry" else saved
                entry["api_key" if field == "model_entry" else field] = "synthetic-legacy-value"
                self.config.write_text(json.dumps(saved), encoding="utf-8")
                Config(str(self.config))
                encoded = self.config.read_bytes()
                migrated = json.loads(encoded)
                entry = migrated["models"][0] if field == "model_entry" else migrated
                self.assertTrue(entry["api_key" if field == "model_entry" else field].startswith("enc2:"))
                Config(str(self.config))
                self.assertEqual(self.config.read_bytes(), encoded)

    def test_missing_configuration_does_not_reset_existing_instance(self):
        for name in ("config.json.bak", "runtime-ready.json", "bot.db"):
            with self.subTest(name=name):
                evidence = self.data / name
                evidence.write_bytes(b"old-instance")
                with self.assertRaises(SystemExit):
                    Config(str(self.config))
                self.assertFalse(self.config.exists())
                self.assertFalse(self.key.exists())
                self.assertEqual(evidence.read_bytes(), b"old-instance")
                evidence.unlink()

    def test_invalid_existing_configuration_is_not_replaced_with_defaults(self):
        (self.data / "runtime-ready.json").write_text('{"ready":true}')
        for damaged in (b"[]", b"null", b"{}"):
            with self.subTest(damaged=damaged):
                self.config.write_bytes(damaged)
                with self.assertRaises(SystemExit):
                    Config(str(self.config))
                self.assertEqual(self.config.read_bytes(), damaged)
                self.assertFalse(self.key.exists())

    def test_reading_old_ciphertext_with_missing_key_creates_no_files(self):
        with self.assertRaises(ValueError):
            util.dpapi_decrypt("enc2:fictional-incomplete-ciphertext", strict=True)
        self.assertEqual(list(self.data.iterdir()), [])

    def test_missing_key_on_reopening_encrypted_config_does_not_rotate(self):
        Config(str(self.config))
        stored = self.config.read_bytes()
        self.assertTrue(json.loads(stored)["onebot_token"].startswith("enc2:"))
        self.key.unlink()
        with self.assertRaises(SystemExit):
            Config(str(self.config))
        self.assertEqual(self.config.read_bytes(), stored)
        self.assertFalse(self.key.exists())

    def test_old_database_keeps_first_encryption_from_creating_a_replacement_key(self):
        database = self.data / "bot.db"
        database.write_bytes(b"fictional database")
        with self.assertRaises(ValueError):
            util.dpapi_encrypt("temporary text", strict=True)
        self.assertFalse(self.key.exists())
        self.assertEqual(database.read_bytes(), b"fictional database")

    def test_new_instance_recovers_from_interrupted_first_key_publication(self):
        with patch.object(util.os, "replace", side_effect=OSError("interrupted")):
            with self.assertRaises(ValueError):
                util.dpapi_encrypt("temporary text")
        self.assertFalse(self.key.exists())
        with ThreadPoolExecutor(max_workers=4) as executor:
            ciphertexts = list(executor.map(util.dpapi_encrypt, ["sample"] * 4))
        self.assertEqual(self.key.stat().st_size, 32)
        self.assertEqual([util.dpapi_decrypt(item, strict=True) for item in ciphertexts], ["sample"] * 4)
