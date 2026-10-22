import unittest

from collector import build_fmw_patch_recommendation, patch_category, patch_presentation_classification


class PatchClassificationTests(unittest.TestCase):
    def assert_security(self, description, label):
        result = patch_presentation_classification(description)
        self.assertEqual(result["patchGroup"], "security")
        self.assertEqual(result["patchGroupLabel"], label)

    def test_weblogic_psu_is_security(self):
        self.assert_security(
            "WLS PATCH SET UPDATE 12.2.1.4.260514",
            "WebLogic PSU",
        )

    def test_explicit_security_update_is_security(self):
        self.assert_security(
            "PERL SECURITY PATCH UPDATE 12.2.1.4.260120",
            "Security Update",
        )

    def test_spu_is_security(self):
        self.assert_security(
            "WEBLOGIC SAMPLES SPU 12.2.1.4.240416",
            "Security Patch Update",
        )

    def test_third_party_bundle_is_security_platform(self):
        self.assert_security(
            "FMW THIRDPARTY BUNDLE PATCH 12.2.1.4.260317",
            "Security Platform Update",
        )

    def test_product_bundle_remains_product(self):
        result = patch_presentation_classification("OAM BUNDLE PATCH 12.2.1.4.260527")
        self.assertEqual(result["patchGroup"], "product")
        self.assertEqual(result["patchGroupLabel"], "Product / Component")

    def test_older_installed_patch_and_latest_recommendation_share_one_row(self):
        recommendation = build_fmw_patch_recommendation(
            {
                "patches": [
                    {
                        "patchId": "38156117",
                        "description": "WLS PATCH SET UPDATE 12.2.1.4.250706",
                        "appliedOn": "2025-07-16",
                    }
                ],
                "versions": [{"key": "Oracle Fusion Middleware", "value": "12.2.1.4.0"}],
            },
            {"products": {"oam": True}},
            "/refresh/home/mwoam",
        )

        wls_rows = [
            row
            for row in recommendation["comparisonRows"]
            if patch_category("{0} {1}".format(row.get("description") or "", row.get("recommendation") or "")) == "WLS"
        ]
        self.assertEqual(len(wls_rows), 1)
        self.assertEqual(wls_rows[0]["patchId"], "38156117")
        self.assertEqual(wls_rows[0]["recommendationStatus"], "recommended")

    def test_missing_members_do_not_create_redundant_table_rows(self):
        recommendation = build_fmw_patch_recommendation(
            {
                "patches": [
                    {
                        "patchId": "38156117",
                        "description": "WLS PATCH SET UPDATE 12.2.1.4.250706",
                        "appliedOn": "2025-07-16",
                    }
                ],
                "versions": [{"key": "Oracle Fusion Middleware", "value": "12.2.1.4.0"}],
            },
            {"products": {"oam": True}},
            "/refresh/home/mwoam",
        )

        self.assertEqual(len(recommendation["comparisonRows"]), 1)
        self.assertGreater(len(recommendation["missingPatches"]), 1)
        self.assertNotIn("Not installed", [row.get("description") for row in recommendation["comparisonRows"]])

    def test_october_catalog_and_superseded_generic_one_off(self):
        recommendation = build_fmw_patch_recommendation(
            {
                "patches": [{"patchId": "39904932", "description": "One-off", "appliedOn": "2026-09-08"}],
                "versions": [{"key": "Oracle Fusion Middleware", "value": "14.1.2.1.0"}],
            },
            {"products": {"oig": True}},
            "/opt/oracle/IAM14c",
        )

        self.assertEqual(recommendation["latestPatchId"], "40131416")
        self.assertEqual(recommendation["comparisonRows"][0]["recommendationStatus"], "recommended")
        self.assertIn("40068276", recommendation["comparisonRows"][0]["recommendation"])

    def test_current_spb_is_authoritative_and_has_no_missing_members(self):
        recommendation = build_fmw_patch_recommendation(
            {
                "patches": [{"patchId": "40131601", "description": "One-off", "appliedOn": "2026-10-22"}],
                "versions": [{"key": "Oracle Fusion Middleware", "value": "12.2.1.4.0"}],
            },
            {"products": {"oam": True}},
            "/opt/oracle/middleware12c",
        )

        self.assertEqual(recommendation["status"], "latest")
        self.assertEqual(recommendation["missingPatches"], [])
        self.assertEqual(recommendation["comparisonRows"][0]["recommendation"], "Latest")

    def test_duplicate_category_updates_are_shown_once(self):
        recommendation = build_fmw_patch_recommendation(
            {
                "patches": [
                    {"patchId": "11111111", "description": "WLS PATCH SET UPDATE 14.1.2.0.250101"},
                    {"patchId": "22222222", "description": "WLS PATCH SET UPDATE 14.1.2.0.250202"},
                ],
                "versions": [{"key": "Oracle Fusion Middleware", "value": "14.1.2.1.0"}],
            },
            {"products": {"oig": True}},
            "/opt/oracle/IAM14c",
        )

        recommended = [row for row in recommendation["comparisonRows"] if row["recommendationStatus"] == "recommended"]
        self.assertEqual(len(recommended), 1)
        self.assertIn("40058371", recommended[0]["recommendation"])
        self.assertEqual(recommendation["comparisonRows"][1]["recommendation"], "-")


if __name__ == "__main__":
    unittest.main()
