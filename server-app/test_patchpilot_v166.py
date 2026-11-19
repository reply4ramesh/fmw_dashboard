import importlib.util
import pathlib
import sys
import unittest


APP_ROOT = pathlib.Path(__file__).resolve().parent
APP_PATH = APP_ROOT / "app.py"
sys.path.insert(0, str(APP_ROOT))
SPEC = importlib.util.spec_from_file_location("fmw_console_v166", str(APP_PATH))
app = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(app)


class PatchPilotV166Tests(unittest.TestCase):
    def test_batch_napply_supports_all_ids_and_patch_list_modes(self):
        server = app.patchpilot_server

        all_script = server.build_patch_apply_script({
            "oracleHome": "/opt/oracle/fmw",
            "patchPath": "/stage/oneoffs",
            "applyMode": "batch",
            "batchSelection": "all",
        })
        self.assertIn('[opatch, "napply", patch_path, "-oh", oracle_home, "-silent"]', all_script)
        self.assertIn('import sys', all_script)
        compile(all_script, "<patchpilot-napply-all>", "exec")

        ids_script = server.build_patch_apply_script({
            "oracleHome": "/opt/oracle/fmw",
            "patchPath": "/stage/oneoffs",
            "applyMode": "batch",
            "batchSelection": "ids",
            "batchPatchIds": "40058371, 40024749 40058371",
        })
        self.assertIn('[opatch, "napply", patch_path, "-id", ",".join(requested_patch_ids)', ids_script)
        self.assertIn('\\"batchPatchIds\\": [\\"40058371\\", \\"40024749\\"]', ids_script)
        compile(ids_script, "<patchpilot-napply-ids>", "exec")

        file_script = server.build_patch_apply_script({
            "oracleHome": "/opt/oracle/fmw",
            "patchPath": "/stage/oneoffs",
            "applyMode": "batch",
            "batchSelection": "file",
            "patchListFile": "/stage/oneoffs/linux64_patchlist.txt",
        })
        self.assertIn('[opatch, "napply", "-oh", oracle_home, "-phBaseFile", patch_list_file', file_script)
        self.assertIn('missing_patch_ids = [patch_id for patch_id in verification_patch_ids', file_script)
        compile(file_script, "<patchpilot-napply-file>", "exec")

    def test_batch_napply_rejects_invalid_or_missing_selection(self):
        server = app.patchpilot_server
        with self.assertRaisesRegex(ValueError, "numeric"):
            server.build_patch_apply_script({
                "oracleHome": "/opt/oracle/fmw",
                "patchPath": "/stage/oneoffs",
                "applyMode": "batch",
                "batchSelection": "ids",
                "batchPatchIds": "40058371, rm -rf",
            })
        with self.assertRaisesRegex(ValueError, "Patch-list file"):
            server.build_patch_apply_script({
                "oracleHome": "/opt/oracle/fmw",
                "patchPath": "/stage/oneoffs",
                "applyMode": "batch",
                "batchSelection": "file",
            })

    def test_cleanup_empty_directory_is_a_successful_noop(self):
        source = (APP_ROOT / "patchpilot" / "server.py").read_text(encoding="utf-8")
        self.assertIn('has_warning = any(status == "missing" for status in statuses)', source)
        self.assertNotIn('has_warning = any(status in ("missing", "empty")', source)

        javascript = (APP_ROOT / "patchpilot" / "assets" / "app.js").read_text(encoding="utf-8")
        self.assertIn('const warning = item.status === "missing" || item.status === "preview";', javascript)

    def test_report_route_validates_requested_phase(self):
        source = (APP_ROOT / "patchpilot" / "server.py").read_text(encoding="utf-8")
        route = source.split('if self.path == "/api/spb/report/latest":', 1)[1].split("return", 1)[0]
        self.assertIn('phase = str(body.get("phase")', route)
        self.assertIn('if phase and phase not in ("prestop", "downtime", "poststart")', route)

    def test_completed_steps_batch_safety_and_report_consistency_are_present(self):
        javascript = (APP_ROOT / "patchpilot" / "assets" / "app.js").read_text(encoding="utf-8")
        self.assertIn('if (isBatchNapply()) return false;', javascript)
        self.assertIn('Batch napply rollback requires a reviewed patch-by-patch recovery plan.', javascript)
        self.assertIn('const completedReadOnly =', javascript)
        self.assertIn('function reportSelectedHome()', javascript)
        self.assertIn('spbCleanupAttempts: state.spbCleanupAttempts', javascript)
        self.assertIn('Service Inventory', javascript)
        self.assertIn('Service Validation', javascript)

    def test_responsive_layout_keeps_patch_controls_visible(self):
        css = (APP_ROOT / "patchpilot" / "assets" / "styles.css").read_text(encoding="utf-8")
        self.assertIn("@media (max-width: 1500px)", css)
        self.assertIn("grid-template-columns: minmax(280px, 300px) minmax(0, 1fr);", css)
        self.assertIn("max-width: 100%;", css)
        self.assertIn(".wizard-step strong {\n  min-width: 0;", css)
        self.assertNotIn(".wizard-rail {\n  grid-column: 1;\n  position: relative;\n  z-index: 2;\n  display: grid;\n  gap: 14px;\n  min-width: 0;\n  max-width: 100%;\n  overflow: hidden;", css)
        self.assertIn("grid-column: 2;", css)
        self.assertIn("@media (max-width: 1180px)", css)
        self.assertIn("grid-template-columns: 1fr;", css)

    def test_explicit_patch_type_remains_authoritative_after_readme_analysis(self):
        javascript = (APP_ROOT / "patchpilot" / "assets" / "app.js").read_text(encoding="utf-8")
        stack_method = javascript.split("function isStackPatchBundle()", 1)[1].split("function isBatchNapply()", 1)[0]
        readme_action = javascript.split("async function analyzeReadmeAction()", 1)[1].split("async function", 1)[0]

        self.assertIn('return selectedPatchMethod() === "spbat";', stack_method)
        self.assertNotIn("detectedReadmePatchMethod", stack_method)
        self.assertIn("const detectedMethod = selectedPatchMethod();", javascript)
        self.assertIn("Workflow Method", javascript)
        self.assertNotIn("PatchPilot switched from", readme_action)
        self.assertNotIn("state.form.patchType =", readme_action)
        self.assertIn("keeps the ${selectedMethodLabel} workflow selected in Connect", readme_action)

    def test_one_off_readme_database_actions_create_a_preinstall_gate(self):
        javascript = (APP_ROOT / "patchpilot" / "assets" / "app.js").read_text(encoding="utf-8")

        self.assertIn('{ id: "preinstall", label: "Pre-Install"', javascript)
        self.assertIn("const installationSection = extractReadmeSection", javascript)
        self.assertIn("const preinstallSteps = beforeApplySteps.filter(installationNeedsManualAction);", javascript)
        self.assertIn("sqlplus|sqldeveloper|sql\\s+client|\\.sql\\b|schema\\s+user", javascript)
        self.assertIn("Database credentials stay outside PatchPilot", javascript)
        self.assertIn("state.readmeAnalysis.preinstallRequiresManual && !state.preinstallDone", javascript)
        self.assertIn("preinstall: confirmPreinstall", javascript)
        self.assertIn("PatchPilot does not request, save, or use the OIG schema password", javascript)

    def test_inactive_cleanup_repeats_delete_and_recheck_for_multiple_ru_chains(self):
        server = app.patchpilot_server
        request = {
            "oracleHome": "/opt/oracle/fmw",
            "retainLevel": 1,
            "opatchHeapOptions": "-Xmx3072m",
            "confirmed": True,
        }
        check_script = server.build_spb_inactive_check_script(request)
        cleanup_script = server.build_spb_inactive_delete_script(request)

        self.assertIn("sum(chain_counts) <= retain_level", check_script)
        self.assertIn('while after.get("hasInactive") and not after.get("desiredReached")', cleanup_script)
        self.assertIn('run_command(list_command)', cleanup_script)
        self.assertIn("Inactive patch deletion pass", cleanup_script)
        self.assertIn("did not change after two delete passes", cleanup_script)
        compile(check_script, "<patchpilot-inactive-check>", "exec")
        compile(cleanup_script, "<patchpilot-inactive-cleanup>", "exec")

        javascript = (APP_ROOT / "patchpilot" / "assets" / "app.js").read_text(encoding="utf-8")
        self.assertIn('id="spbInactiveRemoveNowButton"', javascript)
        self.assertIn("Remove Inactive Patches", javascript)
        self.assertIn("multiple delete and recheck passes", javascript)


if __name__ == "__main__":
    unittest.main()
