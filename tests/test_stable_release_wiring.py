import unittest
from pathlib import Path


class StableReleaseWiringTest(unittest.TestCase):
    def test_manifest_health_verifies_public_hash_and_run(self):
        text=(Path(__file__).resolve().parents[1]/'.github/workflows/build-epg.yml').read_text()
        health=text.split('  health:\n',1)[1].split('  final-status:\n',1)[0]
        self.assertIn('release.json',health)
        self.assertIn('hashlib.sha256',health)
        self.assertIn('EXPECTED_RUN_ID',health)
        self.assertIn("release['guide_sha256']",health)


if __name__=='__main__':unittest.main()
