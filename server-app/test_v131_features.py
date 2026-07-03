import os
import tempfile
import unittest

from config_store import normalize_environment
from environment_registry import get_environment, save_environment, save_global_defaults
from job_runner import list_environment_history, load_environment_history_snapshot, save_environment_snapshot


class Version131FeatureTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.temp_dir.name, "state.sqlite")

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_oig_cluster_is_preserved_and_domain_home_can_be_blank(self):
        environment = normalize_environment({
            "name": "OIG Cluster",
            "environmentType": "oig",
            "products": {"oig": True},
            "server": {"host": "admin.example"},
            "weblogic": {
                "adminUrl": "t3://admin.example:7001",
                "oracleHome": "/u01/oracle",
                "domainHome": "",
                "adminHost": {"host": "admin.example"},
                "cluster": {"enabled": True, "nodes": [{"host": "node2.example"}]},
            },
            "oig": {"oracleHome": "/u01/oracle", "domainHome": ""},
        })
        self.assertTrue(environment["weblogic"]["cluster"]["enabled"])
        self.assertEqual(environment["oig"]["domainHome"], "")

    def test_global_defaults_are_inherited_and_history_is_retained(self):
        save_global_defaults(self.db_path, {
            "ssh": {"username": "shared-oracle"},
            "weblogic": {"oracleHome": "/u01/shared"},
            "history": {"retentionDays": 30, "maxSnapshots": 10},
        })
        saved = save_environment(self.db_path, {
            "name": "Inherited Profile",
            "environmentType": "oig",
            "products": {"oig": True},
            "server": {"host": "admin.example", "username": ""},
            "weblogic": {"adminHost": {"host": "admin.example"}, "oracleHome": ""},
        })
        loaded = get_environment(self.db_path, saved["id"], include_secret=True)
        self.assertEqual(loaded["server"]["username"], "shared-oracle")
        self.assertEqual(loaded["weblogic"]["oracleHome"], "/u01/shared")
        save_environment_snapshot(self.db_path, saved["id"], {
            "generatedAt": "2026-07-02T12:00:00Z",
            "generatedAtEpoch": 1782993600,
            "status": "healthy",
        })
        reports = list_environment_history(self.db_path, saved["id"])
        self.assertEqual(len(reports), 1)
        report = load_environment_history_snapshot(self.db_path, saved["id"], reports[0]["id"])
        self.assertEqual(report["status"], "healthy")


if __name__ == "__main__":
    unittest.main()
