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

    def test_genuinely_missing_patch_uses_real_patch_identity(self):
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

        missing_samples = [
            row
            for row in recommendation["comparisonRows"]
            if row.get("isMissingRecommendation") and "WEBLOGIC SAMPLES" in str(row.get("description") or "").upper()
        ]
        self.assertEqual(len(missing_samples), 1)
        self.assertTrue(missing_samples[0]["patchId"])
        self.assertEqual(missing_samples[0]["appliedOn"], "Not installed")


if __name__ == "__main__":
    unittest.main()
