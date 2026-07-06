import os
import unittest
from unittest import mock

import auto_updater


class AutoUpdaterTest(unittest.TestCase):
    def test_compare_versions(self):
        self.assertEqual(-1, auto_updater.compare_versions("131", "132"))
        self.assertEqual(0, auto_updater.compare_versions("131", "131"))
        self.assertEqual(1, auto_updater.compare_versions("132", "131"))
        self.assertEqual(-1, auto_updater.compare_versions("3i", "3j"))
        self.assertEqual(-1, auto_updater.compare_versions("1.9.0", "1.10.0"))
        self.assertIsNone(auto_updater.compare_versions("release-a", "release-b"))

    @mock.patch.object(auto_updater, "append_upgrade_log")
    @mock.patch.object(auto_updater, "effective_proxy_settings", return_value={})
    @mock.patch.object(auto_updater, "read_version", return_value="131")
    def test_current_version_does_not_queue(self, _read_version, _proxy, _log):
        with mock.patch.object(auto_updater, "queue_github_upgrade") as queue:
            status, _ = auto_updater.check_and_queue_update(lambda _settings, _proxy_settings: "131")
        self.assertEqual("current", status)
        queue.assert_not_called()

    @mock.patch.object(auto_updater, "append_upgrade_log")
    @mock.patch.object(auto_updater, "effective_proxy_settings", return_value={"httpProxy": "proxy"})
    @mock.patch.object(auto_updater, "read_version", return_value="131")
    def test_newer_version_queues_existing_upgrade_flow(self, _read_version, _proxy, _log):
        with mock.patch.object(auto_updater, "queue_github_upgrade") as queue:
            status, _ = auto_updater.check_and_queue_update(lambda _settings, _proxy_settings: "132")
        self.assertEqual("queued", status)
        request = queue.call_args.args[1]
        self.assertEqual("daily-auto-update", request["requestedBy"])
        self.assertEqual("131", request["currentVersion"])
        self.assertEqual("132", request["targetVersion"])

    @mock.patch.dict(os.environ, {"IAM_MONITORING_AUTO_UPDATE_ENABLED": "false"})
    @mock.patch.object(auto_updater, "append_upgrade_log")
    def test_auto_update_can_be_disabled(self, _log):
        with mock.patch.object(auto_updater, "queue_github_upgrade") as queue:
            status, _ = auto_updater.check_and_queue_update()
        self.assertEqual("disabled", status)
        queue.assert_not_called()


if __name__ == "__main__":
    unittest.main()
