import pathlib
import unittest


APP_ROOT = pathlib.Path(__file__).resolve().parent


class StaticAssetVersionTests(unittest.TestCase):
    def test_index_uses_current_cache_busting_version(self):
        version = (APP_ROOT / "VERSION").read_text(encoding="utf-8").strip()
        index = (APP_ROOT / "static" / "index.html").read_text(encoding="utf-8")

        self.assertIn("/assets/app.js?v={0}".format(version), index)
        self.assertIn("/assets/styles.css?v={0}".format(version), index)

    def test_ui_state_version_tracks_release(self):
        version = (APP_ROOT / "VERSION").read_text(encoding="utf-8").strip()
        app_js = (APP_ROOT / "static" / "assets" / "app.js").read_text(encoding="utf-8")

        self.assertIn("security-patches-v{0}".format(version), app_js)


if __name__ == "__main__":
    unittest.main()
