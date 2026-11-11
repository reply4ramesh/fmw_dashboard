import unittest
from unittest.mock import patch

from collector import (
    collect_opatch_inventory,
    collect_oud_monitoring_and_policies,
    collect_oud_proxy_configuration,
    collect_oud_replication_status,
    collect_oud_replication_server_configuration,
    collect_single_oud_metrics,
    discover_oud_admin_host,
    oud_secure_command,
    parse_dsconfig_list,
    parse_oud_replication,
)
from config_store import LEGACY_DEFAULT_OUD_CHECKS, normalize_environment, normalize_oud_oracle_home_path


class OudCollectionTests(unittest.TestCase):
    @patch("collector.run_target")
    def test_admin_endpoint_discovery_tries_status_and_saved_hosts(self, run_target_mock):
        run_target_mock.return_value = {
            "exit_code": 0,
            "output": "__FMW_OUD_ADMIN_HOST__=ds1violet.cuny.edu\n",
        }

        host = discover_oud_admin_host(
            {"mode": "local"},
            {"host": "ds1violet.cuny.edu", "adminPort": 4444},
            "/opt/oracle/instances/DS1/bin",
            "cn=Directory Manager",
            "secret",
            status_host="ds1violet",
        )

        command = run_target_mock.call_args[0][1]
        self.assertEqual("ds1violet.cuny.edu", host)
        self.assertIn("ds1violet ds1violet.cuny.edu 127.0.0.1 localhost", command)
        self.assertIn('list-password-policies >/dev/null 2>&1', command)

    def test_oud_oracle_home_strips_instance_path_below_standard_oracle_home(self):
        self.assertEqual(
            "/refresh/home/oracle/Oracle/Middleware/Oracle_Home",
            normalize_oud_oracle_home_path(
                "/refresh/home/oracle/Oracle/Middleware/Oracle_Home/asinst_ds2/OUD/bin"
            ),
        )
        self.assertEqual(
            "/opt/oracle/middleware/OUD14/oud",
            normalize_oud_oracle_home_path("/opt/oracle/middleware/OUD14/oud"),
        )

    @patch("collector.run_target")
    def test_opatch_inventory_searches_configured_home_and_bounded_parents(self, run_target_mock):
        run_target_mock.return_value = {
            "exit_code": 0,
            "output": "OPATCH_PATH=/refresh/home/oracle/Oracle/Middleware/Oracle_Home/OPatch/opatch\n",
        }

        result = collect_opatch_inventory(
            {"mode": "ssh"},
            "/refresh/home/oracle/Oracle/Middleware/Oracle_Home/asinst_ds2/OUD/bin",
            label="OUD",
        )

        command = run_target_mock.call_args[0][1]
        self.assertIn('candidate="$ORACLE_HOME"; depth=0', command)
        self.assertIn('[ "$depth" -le 6 ]', command)
        self.assertIn('$candidate/OPatch/opatch', command)
        self.assertEqual(
            "/refresh/home/oracle/Oracle/Middleware/Oracle_Home/OPatch/opatch",
            result["opatchPath"],
        )

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
            {"adminHost": "ds1violet", "adminPort": 4444},
            "/opt/oracle/instances/PS1/bin",
            "PS1",
            "cn=Directory Manager",
            "secret",
        )

        self.assertTrue(result["proxyConfigured"])
        self.assertEqual(len(result["proxyWorkflowElements"]), 2)
        self.assertEqual(result["proxyWorkflowElements"][0]["ldap-server-extension"], "DS1")
        self.assertEqual(result["proxyDataSources"][0]["remote-ldap-server-address"], "ds1.example.com")
        self.assertIn("-h ds1violet -p 4444", run_target_mock.call_args_list[0][0][1])

    @patch("collector.run_target")
    def test_proxy_replication_uses_remote_directory_admin_host(self, run_target_mock):
        run_target_mock.return_value = {
            "exit_code": 0,
            "output": "dc=example,dc=com - Replication Enabled\n",
        }

        result = collect_oud_replication_status(
            {"mode": "local"},
            {"adminHost": "proxy1.example.com", "adminPort": 4444},
            "/opt/oracle/instances/PS1/bin",
            "cn=Directory Manager",
            "secret",
            proxy_payload={
                "proxyDataSources": [{
                    "remote-ldap-server-address": "directory1.example.com",
                    "remote-ldap-server-port": "1636",
                }],
            },
        )

        command = run_target_mock.call_args[0][1]
        self.assertIn("./dsreplication status -h directory1.example.com -p 4444", command)
        self.assertEqual("directory1.example.com", result["host"])
        self.assertEqual("proxied directory server", result["source"])
        self.assertNotIn("secret", str(result))

    def test_replication_parser_accepts_advanced_status_table(self):
        output = """
dc=example,dc=com - Replication Enabled
Server                 : Entries : M.C. : A.O.M.C. : Port : Status : Conflicts
-----------------------:---------:------:----------:------:--------:----------
directory1.example.com : 2743545 : 0    : 0 second : 8989 : Normal : 0
directory2.example.com : 2743545 : 0    : 0 second : 8989 : Normal : 0
"""
        sections = parse_oud_replication(output)

        self.assertEqual(1, len(sections))
        self.assertTrue(sections[0]["enabled"])
        self.assertEqual(2, len(sections[0]["servers"]))
        self.assertEqual("Normal", sections[0]["servers"][0]["status"])

    @patch("collector.run_target")
    def test_monitoring_and_policies_use_discovered_admin_host(self, run_target_mock):
        run_target_mock.return_value = {
            "exit_code": 1,
            "output": "Connect Error Result Code: 91",
        }

        result = collect_oud_monitoring_and_policies(
            {"mode": "local"},
            {"adminHost": "ds1violet", "adminPort": 4444},
            "/opt/oracle/instances/DS1/bin",
            "DS1",
            "cn=Directory Manager",
            "secret",
        )

        commands = [call[0][1] for call in run_target_mock.call_args_list]
        self.assertEqual(8, len(commands))
        self.assertTrue(all("-h ds1violet -p 4444" in command for command in commands))
        self.assertIn("Connect Error Result Code: 91", result["monitorError"])
        self.assertIn("Connect Error Result Code: 91", result["passwordPolicyError"])
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

    def test_saving_standalone_oud_does_not_restore_weblogic_from_stale_paths(self):
        existing = normalize_environment({
            "name": "OUD with old WebLogic data",
            "environmentType": "oud",
            "products": {"oud": True, "weblogic": True},
            "server": {"host": "ds1.example.com"},
            "oud": {"host": "ds1.example.com", "oracleHome": "/opt/oracle/oud"},
            "weblogic": {
                "enabled": True,
                "adminUrl": "https://ds1.example.com:7002/console",
                "oracleHome": "/opt/oracle/oud",
                "domainHome": "/opt/oracle/domains/oud",
                "adminHost": {"host": "ds1.example.com"},
            },
        })
        saved = normalize_environment({
            "name": "Standalone OUD",
            "environmentType": "oud",
            "products": {"oud": True, "weblogic": False},
            "server": {"host": "ds1.example.com"},
            "oud": {
                "host": "ds1.example.com",
                "oracleHome": "/opt/oracle/oud",
                "weblogicEnabled": False,
            },
            "weblogic": {
                "enabled": False,
                "adminUrl": "https://ds1.example.com:7002/console",
                "oracleHome": "/opt/oracle/oud",
                "domainHome": "/opt/oracle/domains/oud",
                "adminHost": {"host": "ds1.example.com"},
            },
        }, existing)

        self.assertTrue(saved["products"]["oud"])
        self.assertFalse(saved["products"]["weblogic"])
        self.assertFalse(saved["weblogic"]["enabled"])
        self.assertEqual("", saved["weblogic"]["adminUrl"])
        self.assertEqual("", saved["weblogic"]["oracleHome"])
        self.assertEqual("", saved["weblogic"]["domainHome"])
        self.assertEqual("", saved["weblogic"]["adminHost"]["host"])

    def test_saving_oud_with_weblogic_keeps_explicit_selection(self):
        saved = normalize_environment({
            "name": "OUD with WebLogic",
            "environmentType": "oud",
            "products": {"oud": True, "weblogic": True},
            "server": {"host": "admin.example.com"},
            "oud": {
                "host": "ds1.example.com",
                "oracleHome": "/opt/oracle/oud",
                "weblogicEnabled": True,
            },
            "weblogic": {
                "enabled": True,
                "adminUrl": "https://admin.example.com:7002/console",
                "oracleHome": "/opt/oracle/oud",
                "domainHome": "/opt/oracle/domains/oud",
                "adminHost": {"host": "admin.example.com"},
            },
        })

        self.assertTrue(saved["products"]["oud"])
        self.assertTrue(saved["products"]["weblogic"])
        self.assertTrue(saved["weblogic"]["enabled"])
        self.assertEqual("https://admin.example.com:7002/console", saved["weblogic"]["adminUrl"])


if __name__ == "__main__":
    unittest.main()
