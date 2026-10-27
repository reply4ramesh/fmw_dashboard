import os
import unittest


class OpatchUiSemanticsTests(unittest.TestCase):
    def test_current_level_uses_inventory_rows_only(self):
        app_js_path = os.path.join(
            os.path.dirname(os.path.abspath(__file__)),
            "static",
            "assets",
            "app.js",
        )
        with open(app_js_path, "r", encoding="utf-8") as handle:
            app_js = handle.read()

        start = app_js.rfind("function opatchInstalledRows(")
        end = app_js.find("function opatchCurrentLevelInfo(", start)
        function_source = app_js[start:end]

        self.assertIn("(inventory)", function_source)
        self.assertIn("inventory||{}).patches", function_source)
        self.assertNotIn("comparisonRows", function_source)
        self.assertNotIn("recommendation", function_source)


if __name__ == "__main__":
    unittest.main()
