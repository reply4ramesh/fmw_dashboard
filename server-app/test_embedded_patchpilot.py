import importlib.util
import json
import os
import shlex
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

    def test_static_path_resolution_does_not_require_newer_python_directory_argument(self):
        handler = object.__new__(app.EmbeddedPatchPilotHandler)
        resolved = handler.translate_path("/assets/app.js?version=1")
        self.assertEqual(
            os.path.join(str(app.patchpilot_server.ROOT), "assets", "app.js"),
            resolved,
        )

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

    def test_patchpilot_uses_passwordless_sudo_for_separate_software_owner(self):
        profile = app.patchpilot_server.ssh_profile({
            "host": "fmw.example.com",
            "port": 22,
            "user": "customer_ssh",
            "runAsUser": "oracle",
            "password": "secret",
        })
        command = app.patchpilot_server.build_ssh_command(profile, "id -un")
        self.assertIn("customer_ssh@fmw.example.com", command)
        self.assertEqual(
            "sudo -n -iu oracle -- sh -lc {0}".format(shlex.quote("id -un")),
            command[-1],
        )

    def test_patchpilot_does_not_use_sudo_when_login_is_software_owner(self):
        profile = app.patchpilot_server.ssh_profile({
            "host": "fmw.example.com",
            "user": "oracle",
            "runAsUser": "oracle",
        })
        command = app.patchpilot_server.build_ssh_command(profile, "id -un")
        self.assertEqual("sh -lc {0}".format(shlex.quote("id -un")), command[-1])

    def test_patchpilot_rejects_unsafe_run_as_user(self):
        with self.assertRaisesRegex(ValueError, "valid operating-system username"):
            app.patchpilot_server.ssh_profile({
                "host": "fmw.example.com",
                "user": "customer_ssh",
                "runAsUser": "oracle; id",
            })

    def test_patchpilot_reports_missing_passwordless_sudo_clearly(self):
        message = app.patchpilot_server.ssh_error_message(
            "sudo: a password is required",
            1,
        )
        self.assertIn("passwordless sudo", message)
        self.assertIn("Test SSH + Path", message)


if __name__ == "__main__":
    unittest.main()
