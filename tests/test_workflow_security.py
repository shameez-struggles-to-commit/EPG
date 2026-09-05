import pathlib
import re
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
WORKFLOW = ROOT / ".github/workflows/build-epg.yml"
FULL_SHA = re.compile(r"^[0-9a-f]{40}$")


class WorkflowSecurityTest(unittest.TestCase):
    def test_all_actions_are_pinned_to_full_commit_shas(self):
        text = WORKFLOW.read_text(encoding="utf-8")
        refs = re.findall(r"^\s*-?\s*uses:\s*[^@\s]+@([^\s#]+)", text, re.M)
        self.assertGreaterEqual(len(refs), 6)
        self.assertTrue(all(FULL_SHA.fullmatch(ref) for ref in refs), refs)

    def test_iptv_org_checkout_is_pinned(self):
        text = WORKFLOW.read_text(encoding="utf-8")
        self.assertIn("IPTV_ORG_EPG_REF:", text)
        fetch = re.search(r"git[^\n]*\sfetch[^\n]+IPTV_ORG_EPG_REF[^\n]+", text)
        self.assertIsNotNone(fetch)
        self.assertIn('origin "$IPTV_ORG_EPG_REF"', fetch.group(0))

    def test_channel_catalogs_use_the_same_pinned_revision(self):
        for filename in ("make_iptvorg_channels.py", "fetch_skyhawk.py"):
            text = (ROOT / "pipeline" / filename).read_text(encoding="utf-8")
            with self.subTest(filename=filename):
                self.assertIn("IPTV_ORG_EPG_REF", text)
                self.assertNotIn("iptv-org/epg/master/", text)

    def test_pages_upload_uses_direct_pinned_action(self):
        text = WORKFLOW.read_text(encoding="utf-8")
        self.assertNotIn("actions/upload-pages-artifact@", text)
        self.assertIn(
            "actions/upload-artifact@ea165f8d65b6e75b540449e92b4886f43607fa02",
            text,
        )
        self.assertIn("name: github-pages", text)
        self.assertIn("github-pages.tar", text)
        self.assertIn("--dereference --hard-dereference", text)
        self.assertNotIn("github-pages.tar.gz", text)

    def test_build_and_health_have_read_only_permissions(self):
        text = WORKFLOW.read_text(encoding="utf-8")
        self.assertRegex(text, r"(?s)build:\n.*?permissions:\n\s+contents: read")
        self.assertRegex(text, r"(?s)health:\n.*?permissions:\n\s+contents: read")

    def test_only_deploy_gets_pages_and_id_token_write(self):
        text = WORKFLOW.read_text(encoding="utf-8")
        self.assertEqual(text.count("pages: write"), 1)
        self.assertEqual(text.count("id-token: write"), 1)
        self.assertRegex(
            text,
            r"(?s)deploy:\n.*?permissions:\n\s+actions: read\n\s+contents: read\n\s+pages: write\n\s+id-token: write",
        )

    def test_deploy_has_fifteen_minute_timeout(self):
        text = WORKFLOW.read_text(encoding="utf-8")
        self.assertRegex(text, r"(?s)deploy:\n.*?timeout-minutes: 15")

    def test_watchdog_dispatch_input_is_optional_string_with_empty_default(self):
        text = WORKFLOW.read_text(encoding="utf-8")
        self.assertRegex(
            text,
            r"(?s)workflow_dispatch:\n\s+inputs:\n\s+watchdog_id:\n"
            r"\s+description: Watchdog recovery ID \(UTC day plus random nonce\)\n"
            r"\s+required: false\n\s+default: ''\n\s+type: string",
        )

    def test_watchdog_run_name_uses_exact_recovery_prefix_and_input(self):
        text = WORKFLOW.read_text(encoding="utf-8")
        self.assertRegex(
            text,
            r"(?s)run-name:\s+>[-+]\n\s+\$\{\{ inputs\.watchdog_id != ''\n"
            r"\s+&& format\('EPG watchdog recovery \{0\}', inputs\.watchdog_id\)\n"
            r"\s+\|\| format\('Build EPG Guide \(\{0\}\)', github\.event_name\) \}\}",
        )

    def test_build_uses_serial_queue_without_cancellation(self):
        text = WORKFLOW.read_text(encoding="utf-8")
        self.assertRegex(text, r"(?s)concurrency:\n\s+group: epg-build\n\s+queue: max")
        self.assertNotIn("cancel-in-progress", text)

    def test_deploy_guard_is_pinned_checkout_before_pages_setup(self):
        text = WORKFLOW.read_text(encoding="utf-8")
        deploy = text[text.index("  deploy:\n") :]
        self.assertIn(
            "uses: actions/checkout@11d5960a326750d5838078e36cf38b85af677262",
            deploy,
        )
        self.assertIn("name: Refuse a superseded Pages release", deploy)
        checkout = deploy.index("uses: actions/checkout@11d5960a326750d5838078e36cf38b85af677262")
        guard = deploy.index("name: Refuse a superseded Pages release")
        pages = deploy.index("uses: actions/configure-pages@")
        self.assertLess(checkout, guard)
        self.assertLess(guard, pages)
        self.assertRegex(
            deploy[checkout:guard],
            r"(?s)actions/checkout@11d5960a326750d5838078e36cf38b85af677262.*?"
            r"persist-credentials: false",
        )

    def test_release_guard_receives_fixed_workflow_identity(self):
        text = WORKFLOW.read_text(encoding="utf-8")
        deploy = text[text.index("  deploy:\n") :]
        self.assertIn("name: Refuse a superseded Pages release", deploy)
        guard = deploy[deploy.index("name: Refuse a superseded Pages release") :]
        self.assertRegex(
            guard,
            r"(?s)python3 pipeline/release_order_guard\.py\n"
            r"\s+--repository \"\$\{\{ github\.repository \}\}\"\n"
            r"\s+--workflow \"build-epg\.yml\"\n"
            r"\s+--run-id \"\$\{\{ github\.run_id \}\}\"",
        )

    def test_workflow_has_no_ci_ntfy_secret_or_send_path(self):
        text = WORKFLOW.read_text(encoding="utf-8")
        for secret in ("NTFY_URL", "NTFY_TOKEN", "NTFY_TOPIC"):
            self.assertNotIn(secret, text)
        self.assertNotIn("ntfy", text.lower())

    def test_former_notification_paths_keep_status_logs_and_failures(self):
        text = WORKFLOW.read_text(encoding="utf-8")
        pk = text[text.index("      - name: Run PK scrapers") : text.index("      - name: Generate filtered channel lists")]
        coverage = text[text.index("      - name: Generate coverage report") : text.index("      - name: Coverage gap audit")]
        final = text[text.index("  final-status:") :]
        self.assertIn("data/pk_status.json", pk)
        self.assertIn("EPG PK scraper(s) failed", pk)
        self.assertIn("all PK scrapers OK", pk)
        self.assertIn("deployed channels:", coverage)
        self.assertIn("drop={drop}", coverage)
        for assertion in (
            '[ "$STATUS" = "200" ]',
            '[ "$BUILD_RESULT" = "success" ]',
            '[ "$DEPLOY_RESULT" = "success" ]',
            '[ "$HEALTH_RESULT" = "success" ]',
        ):
            self.assertIn(assertion, final)
        self.assertNotIn("subprocess.run(['curl'", pk)
        self.assertNotIn("coverage-drop ntfy", coverage)
        self.assertNotIn("curl --fail", final)


if __name__ == "__main__":
    unittest.main()
