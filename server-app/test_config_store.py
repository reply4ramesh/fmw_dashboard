import unittest

from config_store import default_oam_checks, normalize_oam_checks


class OamCheckUrlTests(unittest.TestCase):
    def test_defaults_retain_admin_hostname_scheme_and_port(self):
        checks = default_oam_checks("https://as1violet.cuny.edu:7002/console")
        urls = {item["name"]: item["url"] for item in checks}

        self.assertEqual(urls["OAM Console"], "https://as1violet.cuny.edu:7002/oamconsole")
        self.assertEqual(urls["Fusion Middleware EM"], "https://as1violet.cuny.edu:7002/em")
        self.assertEqual(urls["OAM Access"], "http://as1violet.cuny.edu:14150/access")

    def test_existing_local_defaults_are_migrated(self):
        existing = [
            {"product": "oam", "name": "OAM Console", "url": "https://localhost:7002/oamconsole"},
            {"product": "oam", "name": "OAM Access", "url": "http://localhost:14150/access"},
            {"product": "oam", "name": "Fusion Middleware EM", "url": "https://127.0.0.1:7002/em"},
        ]

        checks = normalize_oam_checks(existing, "https://as1violet.cuny.edu:7002/console")
        urls = {item["name"]: item["url"] for item in checks}

        self.assertEqual(urls["OAM Console"], "https://as1violet.cuny.edu:7002/oamconsole")
        self.assertEqual(urls["OAM Access"], "http://as1violet.cuny.edu:14150/access")
        self.assertEqual(urls["Fusion Middleware EM"], "https://as1violet.cuny.edu:7002/em")

    def test_custom_distributed_endpoint_is_preserved(self):
        existing = [
            {"product": "oam", "name": "OAM Access", "url": "https://oam-managed.example:14151/access"},
        ]

        checks = normalize_oam_checks(existing, "https://admin.example:7002/console")

        self.assertEqual(checks[0]["url"], "https://oam-managed.example:14151/access")


if __name__ == "__main__":
    unittest.main()
