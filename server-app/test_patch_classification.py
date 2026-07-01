import unittest

from collector import patch_presentation_classification


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


if __name__ == "__main__":
    unittest.main()
