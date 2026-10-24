import unittest
from unittest.mock import patch

from collector import (
    collect_oud_proxy_configuration,
    collect_oud_replication_server_configuration,
    collect_single_oud_metrics,
    oud_secure_command,
    parse_dsconfig_list,
)
from config_store import LEGACY_DEFAULT_OUD_CHECKS, normalize_environment


class OudCollectionTests(unittest.TestCase):
    def test_secure_command_handles_noclobber_and_clears_inherited_java_flags(self):
        command = oud_secure_command(
            "/opt/oracle/instances/DS1/bin",
            "cn=Directory Manager",
            "secret",
            './status -D "$BIND_DN" -j "$pwfile"',
        )

        self.assertIn('${TMPDIR:-/tmp}/fmw-oud-pwd.XXXXXX', command)
        self.assertIn('tee "$pwfile"', command)
        self.assertNotIn('> "$pwfile"', command)
        self.assertIn("unset JAVA_TOOL_OPTIONS _JAVA_OPTIONS JDK_JAVA_OPTIONS", command)
        self.assertIn("trap 'rm -f", command)

    def test_parse_dsconfig_list_ignores_headers_and_separators(self):
        output = """
Workflow Element : Type               : enabled
-----------------:--------------------:--------
adminRoot         : ldif-local-backend : true
load-bal-we1      : load-balancing     : true
proxy-we1         : proxy-ldap         : true
"""
        rows = parse_dsconfig_list(output, ("name", "type", "enabled"))

        self.assertEqual(len(rows), 3)
        self.assertEqual(rows[1]["name"], "load-bal-we1")
        self.assertEqual(rows[2]["type"], "proxy-ldap")

    @patch("collector.run_target")
    def test_proxy_collection_reports_workflows_and_remote_ldap_sources(self, run_target_mock):
        run_target_mock.side_effect = [
            {
                "exit_code": 0,
                "output": "Workflow Element : Type : enabled\nproxy-we1 : proxy-ldap : true\nload-bal-we1 : load-balancing : true",
            },
            {"exit_code": 0, "output": "Extension : Type\nDS1 : ldap-server"},
            {
                "exit_code": 0,
                "output": "Property : Value(s)\nenabled : true\nldap-server-extension : DS1\nclient-cred-mode : use-client-identity",
            },
            {
                "exit_code": 0,
                "output": "Property : Value(s)\nenabled : true\nload-balancing-algorithm : optimal",
            },
            {
                "exit_code": 0,
                "output": "Property : Value(s)\nremote-ldap-server-address : ds1.example.com\nremote-ldap-server-port : 1636\nremote-ldap-server-ssl-policy : always\nhealth-check-interval : 10 s",
            },
        ]

        result = collect_oud_proxy_configuration(
            {"mode": "local"},
            {"adminPort": 4444},
            "/opt/oracle/instances/PS1/bin",
            "PS1",
            "cn=Directory Manager",
            "secret",
        )

        self.assertTrue(result["proxyConfigured"])
        self.assertEqual(len(result["proxyWorkflowElements"]), 2)
        self.assertEqual(result["proxyWorkflowElements"][0]["ldap-server-extension"], "DS1")
        self.assertEqual(result["proxyDataSources"][0]["remote-ldap-server-address"], "ds1.example.com")
        self.assertNotIn("secret", str(result))

    @patch("collector.run_target")
    def test_replication_server_role_requires_local_server_configuration(self, run_target_mock):
        run_target_mock.return_value = {
            "exit_code": 0,
            "output": (
                "Property : Value(s)\n"
                "enabled : true\n"
                "replication-server-id : 4821\n"
                "replication-port : 8989"
            ),
        }

        result = collect_oud_replication_server_configuration(
            {"mode": "local"},
            {"adminPort": 4444},
            "/opt/oracle/instances/RS1/bin",
            "cn=Directory Manager",
            "secret",
        )

        self.assertTrue(result["replicationServerConfigured"])
        self.assertEqual(result["replicationServerProperties"]["replication-port"], "8989")
        self.assertNotIn("secret", str(result))

        run_target_mock.return_value = {
            "exit_code": 0,
            "output": "Property : Value(s)\nenabled : true\nreplication-server : rs1.example.com:8989",
        }
        participant = collect_oud_replication_server_configuration(
            {"mode": "local"},
            {"adminPort": 4444},
            "/opt/oracle/instances/DS1/bin",
            "cn=Directory Manager",
            "secret",
        )
        self.assertFalse(participant["replicationServerConfigured"])

    @patch("collector.run_target")
    def test_missing_instance_path_stops_before_running_oud_tools(self, run_target_mock):
        run_target_mock.return_value = {"exit_code": 1, "output": ""}
        environment = {
            "products": {"oud": True},
            "oud": {
                "instanceHome": "/opt/oracle/instances/DS2",
                "bindDn": "cn=Directory Manager",
                "bindPassword": "secret",
            },
        }

        result = collect_single_oud_metrics({"mode": "local"}, environment)

        self.assertFalse(result["configuredOnHost"])
        self.assertEqual(result["serverRole"], "Not present on this host")
        self.assertIn("DS2", result["error"])
        self.assertEqual(run_target_mock.call_count, 1)

    def test_legacy_standalone_oud_profile_drops_implicit_weblogic_and_oudsm(self):
        existing = normalize_environment({
            "name": "Legacy OUD",
            "environmentType": "oud",
            "products": {"oud": True, "weblogic": True},
            "server": {"host": "ds1.example.com"},
            "oud": {
                "weblogicEnabled": False,
                "checks": LEGACY_DEFAULT_OUD_CHECKS,
            },
            "weblogic": {"enabled": False},
        })

        self.assertTrue(existing["products"]["oud"])
        self.assertFalse(existing["products"]["weblogic"])
        self.assertEqual(existing["oud"]["checks"], [])


if __name__ == "__main__":
    unittest.main()
