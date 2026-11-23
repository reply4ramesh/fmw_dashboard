import contextlib
import io
import importlib.util
import pathlib
import sys
import tempfile
import unittest


APP_ROOT = pathlib.Path(__file__).resolve().parent
APP_PATH = APP_ROOT / "app.py"
sys.path.insert(0, str(APP_ROOT))
SPEC = importlib.util.spec_from_file_location("fmw_console_v166", str(APP_PATH))
app = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(app)


class PatchPilotV166Tests(unittest.TestCase):
    def test_spb_bundle_is_resolved_from_parent_or_bundle_directory(self):
        server = app.patchpilot_server
        with tempfile.TemporaryDirectory() as temp_dir:
            extraction_root = pathlib.Path(temp_dir) / "OctoberSPB"
            bundle_root = extraction_root / "IDM_SPB_14.1.2.1.261008"
            spbat_dir = bundle_root / "tools" / "spbat" / "generic" / "SPBAT"
            spbat_dir.mkdir(parents=True)
            (spbat_dir / "spbat.sh").write_text("#!/bin/sh\n", encoding="utf-8")
            (bundle_root / "README.txt").write_text("Stack Patch Bundle\n", encoding="utf-8")

            for supplied_path in (extraction_root, bundle_root):
                script = server.build_ssh_test_script({
                    "patchPath": str(supplied_path),
                    "patchType": "Stack Patch Bundle",
                })
                output = io.StringIO()
                with contextlib.redirect_stdout(output):
                    exec(compile(script, "<patchpilot-spb-path-test>", "exec"), {})
                payload = server.parse_discovery_output(output.getvalue())
                validation = payload["patchPath"]
                self.assertTrue(validation["ok"])
                self.assertEqual(str(bundle_root), validation["bundlePath"])
                self.assertEqual(str(spbat_dir / "spbat.sh"), validation["spbatScript"])
                self.assertEqual(str(bundle_root / "README.txt"), validation["readmePath"])

            prepare_script = server.build_spb_prepare_script(str(extraction_root), "", "CHG-189")
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                exec(compile(prepare_script, "<patchpilot-spb-prepare-test>", "exec"), {})
            prepared = server.parse_discovery_output(output.getvalue())
            self.assertEqual(str(bundle_root), prepared["patchPath"])
            self.assertEqual(str(spbat_dir / "spbat.sh"), prepared["spbatScript"])
            self.assertEqual(str(bundle_root / "spbat_logs"), prepared["logDir"])

            second_spbat = extraction_root / "IDM_SPB_14.1.2.1.261099" / "tools" / "spbat" / "generic" / "SPBAT"
            second_spbat.mkdir(parents=True)
            (second_spbat / "spbat.sh").write_text("#!/bin/sh\n", encoding="utf-8")
            ambiguous_script = server.build_ssh_test_script({
                "patchPath": str(extraction_root),
                "patchType": "Stack Patch Bundle",
            })
            with self.assertRaisesRegex(RuntimeError, "Multiple SPB bundles"):
                exec(compile(ambiguous_script, "<patchpilot-ambiguous-spb-test>", "exec"), {})

        javascript = (APP_ROOT / "patchpilot" / "assets" / "app.js").read_text(encoding="utf-8")
        self.assertIn("state.form.patchPath = resolvedBundlePath;", javascript)
        self.assertIn("Resolved SPB bundle", javascript)

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

    def test_patch_apply_streams_heartbeats_and_opatch_log_updates(self):
        server = app.patchpilot_server
        script = server.build_patch_apply_script({
            "oracleHome": "/opt/oracle/fmw",
            "patchPath": "/stage/oneoff/40040980",
            "applyMode": "single",
        })

        self.assertIn("select.select([proc.stdout], [], [], 2.0)", script)
        self.assertIn("import select", script)
        self.assertIn("PatchPilot activity: %s is still running", script)
        self.assertIn("[OPatch log] %s", script)
        self.assertIn("File comparison is active", script)
        self.assertIn("complete details remain in %s", script)
        self.assertIn("CheckConflictAgainstOHWithDetail", script)
        compile(script, "<patchpilot-live-opatch-progress>", "exec")

        javascript = (APP_ROOT / "patchpilot" / "assets" / "app.js").read_text(encoding="utf-8")
        self.assertIn("patchLastActivityAt", javascript)
        self.assertIn("Latest remote message", javascript)
        self.assertIn("The command is running on the target server through SSH", javascript)
        self.assertIn("lastOutputCommandIndex", javascript)
        self.assertIn("Current phase elapsed", javascript)
        self.assertNotIn('lower.lastIndexOf("opatch lsinventory")', javascript)

    def test_zero_exit_conflict_output_still_blocks_patch_apply(self):
        server = app.patchpilot_server
        output = '''
Prereq "checkConflictAgainstOHWithDetail" failed.
Interim patch 40162498 requires prerequisite patch(es) [39904932] which are not present in the Oracle Home.
There are no patches that can be applied now.
OPatch succeeded.
'''

        self.assertEqual(server.required_opatch_prerequisite_ids(output), ["39904932"])
        self.assertEqual(
            server.opatch_conflict_failure_message(output),
            "OPatch conflict prerequisite failed. Apply required prerequisite patch(es) 39904932 first.",
        )
        self.assertEqual(server.opatch_conflict_failure_message("OPatch succeeded."), "")

        script = server.build_patch_apply_script({
            "oracleHome": "/opt/oracle/fmw",
            "patchPath": "/stage/oneoff/40162498",
            "dryRun": False,
        })
        self.assertIn("target_failure = opatch_conflict_failure_message(target_output)", script)
        self.assertIn('conflict_code = target_code or 1', script)
        self.assertIn('"requiredPrerequisitePatches": required_prerequisite_patches', script)
        compile(script, "<patchpilot-zero-exit-conflict>", "exec")

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

    def test_inactive_cleanup_rechecks_until_each_ru_chain_meets_retention(self):
        server = app.patchpilot_server
        request = {
            "oracleHome": "/opt/oracle/fmw",
            "retainLevel": 1,
            "opatchHeapOptions": "-Xmx3072m",
            "confirmed": True,
        }
        check_script = server.build_spb_inactive_check_script(request)
        cleanup_script = server.build_spb_inactive_delete_script(request)

        self.assertIn("max(chain_counts) <= retain_level", check_script)
        self.assertIn('while after.get("hasInactive") and not after.get("desiredReached")', cleanup_script)
        self.assertIn('run_command(list_command)', cleanup_script)
        self.assertIn("Inactive patch deletion pass", cleanup_script)
        self.assertIn("did not change after two delete passes", cleanup_script)
        self.assertLess(
            cleanup_script.index("def parse_inactive_output(output):"),
            cleanup_script.index("after = parse_inactive_output(recheck_output)"),
        )
        compile(check_script, "<patchpilot-inactive-check>", "exec")
        compile(cleanup_script, "<patchpilot-inactive-cleanup>", "exec")

        helper_source = cleanup_script.split("def parse_inactive_output(output):", 1)[1].split("\n\noutput_parts = []", 1)[0]
        helper_namespace = {
            "re": __import__("re"),
            "retain_level": 1,
            "oracle_home": "/opt/oracle/fmw",
            "opatch": "/opt/oracle/fmw/OPatch/opatch",
            "opatch_heap": {"effective": "-Xmx3072m"},
            "properties_path": "/opt/oracle/fmw/OPatch/config/opatch.properties",
            "current_retain_value": lambda: "1",
            "command_text": lambda command: " ".join(command),
        }
        exec("def parse_inactive_output(output):" + helper_source, helper_namespace)
        sample = "\n".join(
            "There are 1 inactive RU patches in chain {0}".format(index)
            for index in range(1, 18)
        )
        parsed = helper_namespace["parse_inactive_output"](sample)
        self.assertEqual(17, parsed["inactivePatchCount"])
        self.assertTrue(parsed["desiredReached"])
        needs_cleanup = helper_namespace["parse_inactive_output"](
            "There are 2 inactive RU patches in chain 1\nThere are 1 inactive RU patches in chain 2"
        )
        self.assertEqual(3, needs_cleanup["inactivePatchCount"])
        self.assertFalse(needs_cleanup["desiredReached"])

        javascript = (APP_ROOT / "patchpilot" / "assets" / "app.js").read_text(encoding="utf-8")
        self.assertIn('id="spbInactiveRemoveNowButton"', javascript)
        self.assertIn("Remove Inactive Patches", javascript)
        self.assertIn("multiple delete and recheck passes", javascript)
        self.assertIn('dom.secondaryActionButton.hidden = hasContextualInactiveAction;', javascript)
        self.assertIn('${!jobRunning && earlyComplete ? `', javascript)
        self.assertIn('state.spbInactiveSkipConfirmed = false;', javascript)
        self.assertIn('data-auto-follow-log', javascript)
        self.assertIn('element.scrollTop = element.scrollHeight;', javascript)
        self.assertIn('Inactive Patch Cleanup Job Output', javascript)
        self.assertIn('function spbInactiveRetainDescription', javascript)
        self.assertIn('retain up to ${level} inactive RU/CPU patch', javascript)
        self.assertIn('retained for ${retention} rollback coverage', javascript)
        self.assertIn('detected; cleanup target is ${retention}', javascript)
        self.assertIn('Rollback retention target', javascript)
        self.assertNotIn('Accept N-1 Retention and Continue', javascript)
        self.assertIn('if (check && check.desiredReached) return true;', javascript)
        self.assertIn('state.spbInactiveReviewed = true;', javascript)
        self.assertIn('state.completed.add("spbInactive");', javascript)
        self.assertIn('Nothing is eligible for deletion at the selected retention level.', javascript)
        self.assertIn('change the retention level and scan again', javascript)


if __name__ == "__main__":
    unittest.main()
