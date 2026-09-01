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
            r"(?s)deploy:\n.*?permissions:\n\s+pages: write\n\s+id-token: write",
        )


if __name__ == "__main__":
    unittest.main()
