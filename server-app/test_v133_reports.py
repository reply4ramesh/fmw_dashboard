import json
import os
import sqlite3
import tempfile
import time
import unittest

from environment_registry import get_global_defaults, save_global_defaults
from job_runner import history_storage_details, list_environment_history, save_environment_snapshot


class Version133ReportTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.temp_dir.name, "state", "iam-monitoring.sqlite")

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_safe_history_defaults(self):
        history = get_global_defaults(self.db_path)["history"]
        self.assertEqual(1, history["retentionDays"])
        self.assertEqual(48, history["maxSnapshots"])
        self.assertEqual("", history["storageDirectory"])

    def test_legacy_history_defaults_migrate_to_safe_limits(self):
        get_global_defaults(self.db_path)
        with sqlite3.connect(self.db_path) as conn:
            conn.execute(
                "INSERT OR REPLACE INTO global_settings(setting_key, payload_json, updated_at) "
                "VALUES ('connection_defaults', ?, '2026-07-06T00:00:00Z')",
                (json.dumps({"history": {"retentionDays": 90, "maxSnapshots": 500}}),),
            )
        history = get_global_defaults(self.db_path)["history"]
        self.assertEqual(1, history["retentionDays"])
        self.assertEqual(48, history["maxSnapshots"])
        self.assertEqual("", history["storageDirectory"])

    def test_custom_history_directory_is_used_and_indexed(self):
        custom_dir = os.path.join(self.temp_dir.name, "external-history")
        save_global_defaults(self.db_path, {
            "history": {"retentionDays": 1, "maxSnapshots": 48, "storageDirectory": custom_dir}
        })
        save_environment_snapshot(self.db_path, "demo", {
            "generatedAt": "2026-07-06T20:00:00Z",
            "generatedAtEpoch": int(time.time()),
            "status": "healthy",
        })
        reports = list_environment_history(self.db_path, "demo")
        self.assertEqual(1, len(reports))
        self.assertTrue(os.path.isfile(os.path.join(custom_dir, "demo", reports[0]["id"] + ".json")))
        self.assertTrue(os.path.isfile(os.path.join(custom_dir, "demo", ".report-index.json")))
        self.assertEqual(custom_dir, history_storage_details(self.db_path)["effectiveDirectory"])

    def test_history_settings_can_be_saved_without_erasing_other_defaults(self):
        save_global_defaults(self.db_path, {"ssh": {"username": "oracle"}})
        save_global_defaults(self.db_path, {"history": {"retentionDays": 2, "maxSnapshots": 24}})
        defaults = get_global_defaults(self.db_path)
        self.assertEqual("oracle", defaults["ssh"]["username"])
        self.assertEqual(2, defaults["history"]["retentionDays"])
        self.assertEqual(24, defaults["history"]["maxSnapshots"])

    def test_custom_history_directory_must_be_absolute(self):
        with self.assertRaises(ValueError):
            save_global_defaults(self.db_path, {"history": {"storageDirectory": "relative/history"}})


if __name__ == "__main__":
    unittest.main()
