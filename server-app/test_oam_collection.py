import unittest
from unittest.mock import patch

import collector


class OamCollectionTests(unittest.TestCase):
    def test_curated_parser_supports_ordinary_xml_wrappers(self):
        xml_text = """
        <NGAMConfiguration>
          <DeployedComponent>
            <Server><NGAMServer><Profile><Sme><SessionConfigurations>
              <Timeout>15</Timeout><MaxSessionsPerUser>8</MaxSessionsPerUser>
            </SessionConfigurations></Sme></Profile></NGAMServer></Server>
            <Agent><WebGate><Instance><VioletWebGate>
              <preferredHost>violet.example.com</preferredHost><security>Simple</security>
            </VioletWebGate></Instance></WebGate></Agent>
          </DeployedComponent>
          <Resource><LDAP><VioletStore>
            <LDAP_URL>ldaps://oid.example.com:636</LDAP_URL>
            <SECURITY_PRINCIPAL>cn=oam,dc=example,dc=com</SECURITY_PRINCIPAL>
          </VioletStore></LDAP></Resource>
        </NGAMConfiguration>
        """
        paths = collector.parse_oam_setting_paths(xml_text)
        curated = collector.extract_oam_curated_config(paths)

        self.assertEqual(curated["sessionConfigurations"][0]["value"], "15 M")
        self.assertEqual(curated["webgates"][0]["name"], "VioletWebGate")
        self.assertEqual(curated["webgates"][0]["preferredHost"], "violet.example.com")
        self.assertEqual(curated["identityStores"][0]["host"], "oid.example.com")
        self.assertEqual(curated["identityStores"][0]["port"], "636")

    def test_setting_value_attribute_is_collected(self):
        xml_text = """
        <Setting Name="NGAMConfiguration">
          <Setting Name="DeployedComponent"><Setting Name="Server">
            <Setting Name="NGAMServer"><Setting Name="Profile"><Setting Name="Sme">
              <Setting Name="SessionConfigurations"><Setting Name="Timeout" Value="30" /></Setting>
            </Setting></Setting></Setting>
          </Setting></Setting>
        </Setting>
        """
        paths = collector.parse_oam_setting_paths(xml_text)
        curated = collector.extract_oam_curated_config(paths)

        self.assertEqual(curated["sessionConfigurations"][0]["value"], "30 M")

    @patch("collector.run_target")
    def test_config_discovery_returns_selected_file_and_curated_rows(self, run_target_mock):
        run_target_mock.return_value = {
            "exit_code": 0,
            "output": """IAM_MONITORING_OAM_CONFIG=/opt/oracle/user_projects/domains/oam/config/fmwconfig/oam-config.xml
            <NGAMConfiguration><DeployedComponent><Agent><WebGate><Instance><WG1>
            <preferredHost>violet.example.com</preferredHost>
            </WG1></Instance></WebGate></Agent></DeployedComponent></NGAMConfiguration>""",
        }

        result = collector.collect_oam_config_metrics(
            {"host": "oam.example.com"},
            "/opt/oracle/middleware/Oracle_IAM14C",
            "/opt/oracle/user_projects/domains/oam",
        )

        self.assertEqual(result["configFile"], "/opt/oracle/user_projects/domains/oam/config/fmwconfig/oam-config.xml")
        self.assertEqual(result["curated"]["webgates"][0]["name"], "WG1")
        command = run_target_mock.call_args.args[1]
        self.assertIn("is_oam_config", command)
        self.assertIn("SessionConfigurations", command)

    @patch("collector.run_target")
    def test_oauth_retries_self_signed_certificate_read_only_call(self, run_target_mock):
        def fake_run(_target, command, timeout=60):
            if " -k " not in command:
                return {"exit_code": 60, "output": "curl: (60) SSL certificate problem: self signed certificate"}
            if "oauthidentitydomain" in command:
                return {"exit_code": 0, "output": '{"name":"OAADomain"}'}
            if "application?" in command:
                return {"exit_code": 0, "output": '{"name":"OAAResource"}'}
            return {"exit_code": 0, "output": "[]"}

        run_target_mock.side_effect = fake_run
        result = collector.collect_oam_oauth_rest_metrics(
            {"host": "oam.example.com"},
            {
                "adminUrl": "https://oam.example.com:7002",
                "adminUsername": "weblogic",
                "adminPassword": "secret",
            },
            {},
        )

        self.assertEqual(result["domainName"], "OAADomain")
        self.assertEqual(result["tlsVerification"], "certificate-validation-bypassed")
        self.assertIn("not trusted", result["warning"])
        self.assertTrue(any("certificate fallback" in command for command in result["commands"]))


if __name__ == "__main__":
    unittest.main()
