import importlib.util
import json
import os
import sys
import threading
import unittest
from urllib.request import urlopen


APP_PATH = os.path.join(os.path.dirname(__file__), "app.py")
sys.path.insert(0, os.path.dirname(APP_PATH))
SPEC = importlib.util.spec_from_file_location("fmw_console_app", APP_PATH)
app = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(app)


class EmbeddedPatchPilotTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        app.start_embedded_patchpilot()
        cls.console = app.ThreadingHTTPServer(("127.0.0.1", 0), app.Handler)
        cls.thread = threading.Thread(target=cls.console.serve_forever, daemon=True)
        cls.thread.start()
        cls.base_url = "http://127.0.0.1:{0}".format(cls.console.server_address[1])

    @classmethod
    def tearDownClass(cls):
        cls.console.shutdown()
        cls.console.server_close()
        if app.PATCHPILOT_HTTPD is not None:
            app.PATCHPILOT_HTTPD.shutdown()
            app.PATCHPILOT_HTTPD.server_close()

    def test_patchpilot_page_is_served_inside_console(self):
        with urlopen(self.base_url + "/patchpilot/", timeout=5) as response:
            html = response.read().decode("utf-8")
        self.assertIn("PatchPilot", html)
        self.assertIn('id="wizardSteps"', html)
        self.assertNotIn("PatchPilot Application URL", html)

    def test_patchpilot_assets_use_embedded_api_prefix(self):
        with urlopen(self.base_url + "/patchpilot/assets/app.js", timeout=5) as response:
            javascript = response.read().decode("utf-8")
        self.assertIn('patchType: "Stack Patch Bundle"', javascript)
        self.assertIn('postJson("api/ssh/test"', javascript)
        self.assertNotIn('postJson("/api/', javascript)

    def test_patchpilot_health_is_proxied(self):
        with urlopen(self.base_url + "/patchpilot/healthz", timeout=5) as response:
            payload = json.loads(response.read().decode("utf-8"))
        self.assertTrue(payload["ok"])
        self.assertEqual("patchpilot", payload["service"])


if __name__ == "__main__":
    unittest.main()
