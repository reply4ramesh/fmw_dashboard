import unittest

from collector import build_weblogic_runtime_script, product_metrics_status, weblogic_connect_candidates


class RemoteMonitoringTests(unittest.TestCase):
    def test_same_remote_admin_host_adds_loopback_fallback(self):
        candidates = weblogic_connect_candidates(
            "https://prod-admin.example.com:7002",
            {"host": "prod-admin.example.com"},
        )

        self.assertEqual(
            candidates,
            ["t3s://prod-admin.example.com:7002", "t3s://127.0.0.1:7002"],
        )

    def test_separate_admin_host_does_not_add_loopback_fallback(self):
        candidates = weblogic_connect_candidates(
            "t3://admin.example.com:7001",
            {"host": "managed-1.example.com"},
        )

        self.assertEqual(candidates, ["t3://admin.example.com:7001"])

    def test_secure_loopback_retry_ignores_only_hostname_mismatch(self):
        local_script = build_weblogic_runtime_script("weblogic", "secret", "t3s://127.0.0.1:7002")
        remote_script = build_weblogic_runtime_script("weblogic", "secret", "t3s://admin.example.com:7002")

        self.assertIn("weblogic.security.SSL.ignoreHostnameVerification", local_script)
        self.assertNotIn("weblogic.security.SSL.ignoreHostnameVerification", remote_script)

    def test_missing_process_rows_do_not_prove_weblogic_is_down(self):
        status = product_metrics_status({
            "weblogic": {
                "expectedServers": ["AdminServer", "oam_server1"],
                "serverInventory": [
                    {"name": "AdminServer", "state": "RUNNING"},
                    {"name": "oam_server1", "state": "RUNNING"},
                ],
                "servers": [],
                "missingServers": ["AdminServer", "oam_server1"],
            }
        })

        self.assertEqual(status, "warning")

    def test_collection_error_is_warning_not_down(self):
        status = product_metrics_status({
            "weblogic": {
                "expectedServers": ["AdminServer"],
                "serverInventory": [],
                "serverInventoryError": "Connection refused",
                "servers": [],
            }
        })

        self.assertEqual(status, "warning")

    def test_confirmed_non_running_weblogic_inventory_is_down(self):
        status = product_metrics_status({
            "weblogic": {
                "expectedServers": ["AdminServer"],
                "serverInventory": [{"name": "AdminServer", "state": "SHUTDOWN"}],
                "servers": [],
            }
        })

        self.assertEqual(status, "down")

    def test_oid_process_collection_error_is_not_reported_as_down(self):
        status = product_metrics_status({
            "oid": {
                "enabled": True,
                "processCount": 0,
                "processError": "ps command failed",
            }
        })

        self.assertEqual(status, "warning")


if __name__ == "__main__":
    unittest.main()
