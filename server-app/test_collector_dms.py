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

SAMPLE_DMS_TEXT_OUTPUT = """
IAM_DMS_TARGET|DMS Application#12.2.1.1.0|oamservers|oam_server1
IAM_DMS_TABLE|OAMS.OAM_Authn
IAM_DMS_TABLE|JVM_Memory
IAM_DMS_TEXT_BEGIN
--------------
OAMS.OAM_Authn
--------------

Host: oam.example.com
Name: OAM Authentication
Process: oam_server1:14100
ServerName: oam_server1
authentication.completed: 42 ops
authentication.avg: 3.5 msecs

----------
JVM_Memory
----------

Host: oam.example.com
Name: heap
Process: oam_server1:14100
ServerName: oam_server1
used.value: 1000 kbytes
IAM_DMS_TEXT_END
"""


class DmsCollectorTests(unittest.TestCase):
    def test_generated_wlst_script_is_valid_python_syntax(self):
        script = build_dms_wlst_script("weblogic", "secret", "t3://admin.example:7001")

        compile(script, "dms_wlst.py", "exec")
        self.assertIn("displayMetricTableNames(servers=dms_servers)", script)
        self.assertIn("apply(displayMetricTables, selected_table_names", script)
        self.assertNotIn("return default_value if", script)
        self.assertNotIn("print(clean_dms(dms_xml) if", script)
        self.assertIn("print('IAM_DMS_TEXT_BEGIN')", script)
        self.assertIn("open(dms_output_file, 'r')", script)
        self.assertIn("os.remove(dms_output_file)", script)
        self.assertIn("System.currentTimeMillis()", script)
        self.assertNotIn("time.time()", script)

    def test_parses_targeted_display_metric_tables(self):
        result = parse_dms_wlst_output(SAMPLE_DMS_TEXT_OUTPUT)

        self.assertEqual(result["servers"], ["oam_server1"])
        self.assertEqual(len(result["tables"]), 2)
        self.assertIn(
            {
                "server": "oam_server1",
                "table": "OAMS.OAM_Authn",
                "instance": "OAM Authentication / oam.example.com / oam_server1:14100 / oam_server1",
                "metric": "authentication.completed",
                "value": "42 ops",
                "type": "",
            },
            result["metrics"],
        )

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

    def test_reports_names_without_xml_table_content(self):
        result = parse_dms_wlst_output(
            "IAM_DMS_TARGET|dms|AdminServer|AdminServer\n"
            "IAM_DMS_TABLE|OAMS.OAM_Authn\n"
            "IAM_DMS_XML_BEGIN\nIAM_DMS_XML_END"
        )

        self.assertIn("no XML table content", result["error"])


if __name__ == "__main__":
    unittest.main()
