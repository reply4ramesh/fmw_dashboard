import pathlib
import unittest


APP_ROOT = pathlib.Path(__file__).resolve().parent


class StaticAssetVersionTests(unittest.TestCase):
    def test_index_uses_current_cache_busting_version(self):
        version = (APP_ROOT / "VERSION").read_text(encoding="utf-8").strip()
        index = (APP_ROOT / "static" / "index.html").read_text(encoding="utf-8")

        self.assertIn("/assets/app.js?v={0}".format(version), index)
        self.assertIn("/assets/styles.css?v={0}".format(version), index)

        report = (APP_ROOT / "static" / "report.html").read_text(encoding="utf-8")
        self.assertIn("/assets/report.js?v={0}".format(version), report)
        self.assertIn("/assets/report.css?v={0}".format(version), report)

    def test_ui_state_version_tracks_release(self):
        version = (APP_ROOT / "VERSION").read_text(encoding="utf-8").strip()
        app_js = (APP_ROOT / "static" / "assets" / "app.js").read_text(encoding="utf-8")

        self.assertIn("v{0}".format(version), app_js)

    def test_recipient_submit_uses_form_attribute_not_shadowed_id_property(self):
        app_js = (APP_ROOT / "static" / "assets" / "app.js").read_text(encoding="utf-8")
        self.assertIn('form.getAttribute("id")!=="recipient-form"', app_js)
        self.assertIn('event.preventDefault();event.stopImmediatePropagation();saveRecipient(form)', app_js)

    def test_notification_profile_scope_controls_are_present(self):
        app_js = (APP_ROOT / "static" / "assets" / "app.js").read_text(encoding="utf-8")
        self.assertIn("Notification Profiles", app_js)
        self.assertIn('data-notification-scope-form', app_js)
        self.assertIn('environmentScope:environmentScope,environmentIds:environmentIds', app_js)


if __name__ == "__main__":
    unittest.main()
