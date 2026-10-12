import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


class StableReleaseWiringTest(unittest.TestCase):
    def test_manifest_health_verifies_public_hash_and_run(self):
        workflow = (ROOT / '.github/workflows/build-epg.yml').read_text(encoding='utf-8')
        health = workflow.split('  health:\n', 1)[1].split('  final-status:\n', 1)[0]
        helper = (ROOT / 'pipeline/verify_release_health.py').read_text(encoding='utf-8')
        self.assertIn('pipeline/verify_release_health.py', health)
        self.assertIn('--base-url', health)
        self.assertIn('EXPECTED_RUN_ID', health)
        self.assertIn('EXPECTED_REVISION', health)
        self.assertIn('actions/checkout@', health)
        self.assertNotIn('python3 - <<', health)
        self.assertIn('hashlib.sha256', helper)
        self.assertIn('release.get("guide_sha256")', helper)
        self.assertIn('no current programmes', helper)

    def test_health_job_has_bounded_external_wall(self):
        workflow = (ROOT / '.github/workflows/build-epg.yml').read_text(encoding='utf-8')
        health = workflow.split('  health:\n', 1)[1].split('  final-status:\n', 1)[0]
        # The helper's 180s budget is enforced with checks; per-operation
        # socket timeouts cannot bound one transfer, so the job needs its own
        # small wall-clock timeout.
        self.assertIn('timeout-minutes: 5', health)


if __name__=='__main__':unittest.main()
