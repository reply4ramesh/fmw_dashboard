import unittest

from collector import build_dms_wlst_script, parse_dms_wlst_output


SAMPLE_DMS_OUTPUT = """
IAM_DMS_TARGET|dms|oam_cluster|oam_server1
IAM_DMS_TARGET|dms|oam_cluster|oam_server2
IAM_DMS_TABLE|oracle_security_oam_runtime
IAM_DMS_TABLE|JVM_Memory
IAM_DMS_XML_BEGIN
<table name="oracle_security_oam_runtime" keys="ServerName agentName" componentId="oam_server1">
  <row>
    <column name="ServerName"><![CDATA[oam_server1]]></column>
    <column name="agentName"><![CDATA[WebGate_1]]></column>
    <column name="requests.completed" type="LONG">42</column>
    <column name="latency.avg" type="DOUBLE">3.5</column>
  </row>
</table>
<table name="JVM_Memory" keys="ServerName name" componentId="oam_server1">
  <row>
    <column name="ServerName">oam_server1</column>
    <column name="name">heap</column>
    <column name="used.value" type="LONG">1000</column>
  </row>
</table>
IAM_DMS_XML_END
"""


class DmsCollectorTests(unittest.TestCase):
    def test_generated_wlst_script_is_valid_python_syntax(self):
        script = build_dms_wlst_script("weblogic", "secret", "t3://admin.example:7001")

        compile(script, "dms_wlst.py", "exec")
        self.assertIn("displayMetricTableNames(servers=dms_servers)", script)
        self.assertIn("dumpMetrics(servers=dms_servers, format='xml')", script)

    def test_parses_deployment_targets_and_metric_values(self):
        result = parse_dms_wlst_output(SAMPLE_DMS_OUTPUT)

        self.assertEqual(result["servers"], ["oam_server1", "oam_server2"])
        self.assertEqual(result["tableCount"], 2)
        self.assertEqual(len(result["tables"]), 2)
        self.assertIn(
            {
                "server": "oam_server1",
                "table": "oracle_security_oam_runtime",
                "instance": "oam_server1 / WebGate_1",
                "metric": "requests.completed",
                "value": "42",
                "type": "LONG",
            },
            result["metrics"],
        )

    def test_reports_missing_metric_document(self):
        result = parse_dms_wlst_output("IAM_DMS_TARGET|dms|AdminServer|AdminServer")

        self.assertIn("no XML", result["error"])


if __name__ == "__main__":
    unittest.main()
