import unittest
from unittest import mock

import collector


class ConfiguredProductPathsTests(unittest.TestCase):
    def test_collects_oracle_domain_and_named_instance_homes(self):
        environment = {
            "weblogic": {
                "oracleHome": "/opt/oracle/fmw",
                "domainHome": "/opt/oracle/domains/base_domain",
            },
            "oud": {
                "oracleHome": "/opt/oracle/middleware/OUD",
                "instanceHome": "/opt/oracle/middleware/instances/DS1",
                "instances": [
                    {"name": "DS1", "instanceHome": "/opt/oracle/middleware/instances/DS1"},
                ],
            },
        }

        rows = collector.configured_product_paths(environment)
        by_path = {row["path"]: row["labels"] for row in rows}

        self.assertEqual(by_path["/opt/oracle/fmw"], ["WebLogic ORACLE_HOME"])
        self.assertEqual(by_path["/opt/oracle/domains/base_domain"], ["WebLogic DOMAIN_HOME"])
        self.assertEqual(by_path["/opt/oracle/middleware/OUD"], ["OUD ORACLE_HOME"])
        self.assertEqual(
            by_path["/opt/oracle/middleware/instances/DS1"],
            ["OUD DS1 INSTANCE_HOME"],
        )

    @mock.patch("collector.run_target")
    def test_groups_paths_on_the_same_filesystem_mount(self, run_target):
        run_target.side_effect = [
            {
                "exit_code": 0,
                "output": "Filesystem 1-blocks Used Available Capacity Mounted on\n/dev/mapper/opt 1000 400 600 40% /opt",
            },
            {
                "exit_code": 0,
                "output": "Filesystem 1-blocks Used Available Capacity Mounted on\n/dev/mapper/opt 1000 400 600 40% /opt",
            },
        ]

        rows = collector.collect_product_filesystems(
            {"mode": "local"},
            [
                {"path": "/opt/oracle/middleware/OUD", "labels": ["OUD ORACLE_HOME"]},
                {"path": "/opt/oracle/middleware/instances/DS1", "labels": ["OUD DS1 INSTANCE_HOME"]},
            ],
        )

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["mount"], "/opt")
        self.assertEqual(rows[0]["usedPercent"], 40.0)
        self.assertEqual(len(rows[0]["configuredPaths"]), 2)
        self.assertEqual(rows[0]["labels"], ["OUD ORACLE_HOME", "OUD DS1 INSTANCE_HOME"])

    @mock.patch("collector.run_target")
    def test_ignores_unavailable_configured_paths(self, run_target):
        run_target.return_value = {"exit_code": 0, "output": ""}

        rows = collector.collect_product_filesystems(
            {"mode": "local"},
            [{"path": "/missing", "labels": ["OUD ORACLE_HOME"]}],
        )

        self.assertEqual(rows, [])


class ProductFilesystemHealthTests(unittest.TestCase):
    def test_product_filesystem_pressure_affects_host_health(self):
        snapshot = {
            "reachable": True,
            "uptime": {"load1": 0.1, "cpuBreakdown": {"ioWaitPercent": 0, "idlePercent": 90}},
            "memory": {"usedPercent": 20},
            "rootDisk": {"usedPercent": 10},
            "refreshDisk": {},
            "productFilesystems": [
                {
                    "mount": "/opt",
                    "labels": ["OUD ORACLE_HOME", "OUD DS1 INSTANCE_HOME"],
                    "usedPercent": 81,
                },
            ],
        }

        health = collector.build_server_health(snapshot)

        self.assertEqual(health["status"], "critical")
        self.assertEqual(health["checks"]["productFilesystem:0"]["severity"], "critical")
        self.assertIn("/opt", health["checks"]["productFilesystem:0"]["label"])


if __name__ == "__main__":
    unittest.main()
