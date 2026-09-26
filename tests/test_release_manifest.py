import gzip
import hashlib
import unittest

from pipeline import release_manifest


class ReleaseManifestTest(unittest.TestCase):
    def test_manifest_checks_actual_guide_bytes(self):
        seed = {'schema': 1, 'overrides': {'1': 'one.uk'}}
        guide = gzip.compress(b'<tv><channel id="one.uk"><display-name>One</display-name></channel></tv>')
        manifest = release_manifest.create_manifest(guide, seed, 'a' * 40, '123')
        self.assertEqual(manifest['guide_sha256'], hashlib.sha256(guide).hexdigest())
        self.assertEqual(release_manifest.verify_manifest(manifest, guide, seed), {'one.uk'})
        with self.assertRaises(ValueError):
            release_manifest.verify_manifest(manifest, gzip.compress(b'<tv/>'), seed)


if __name__ == '__main__':
    unittest.main()
