import base64
import gzip
import json
import os
import unittest
from unittest.mock import patch

from pipeline import build_mapping


class StableIdentityTest(unittest.TestCase):
    def test_stable_output_does_not_trust_now_duplicated_provider_id(self):
        import contextlib, io, sys, tempfile
        from pathlib import Path
        seed = base64.b64encode(gzip.compress(json.dumps({'schema': 1, 'overrides': {'1': 'example.uk'}}).encode())).decode()
        streams = [{'stream_id': n, 'name': 'Example %s' % n, 'cat_name': 'UK | Entertainment', 'epg_channel_id': 'example.uk'} for n in (1, 2)]
        with tempfile.TemporaryDirectory() as temp:
            p = Path(temp); (p/'streams.json').write_text(json.dumps(streams)); (p/'index.json').write_text('{}')
            args = ['mapping', '--streams', str(p/'streams.json'), '--sources-index', str(p/'index.json'), '-o', str(p/'out.json')]
            with patch.dict(os.environ, {'EPG_IDENTITY_SEED': seed}), patch.object(sys, 'argv', args), contextlib.redirect_stdout(io.StringIO()):
                build_mapping.main()
            result = json.loads((p/'out.json').read_text())
            self.assertFalse(any(c['source']=='provider' for row in result.values() for c in row['candidates']))

    def test_seed_preserves_output_id_after_provider_change(self):
        seed = base64.b64encode(gzip.compress(json.dumps({'schema': 1, 'overrides': {'1': 'example.uk'}}).encode())).decode()
        streams = [{'stream_id': 1, 'name': 'Example', 'epg_channel_id': 'other.uk'}]
        with patch.dict(os.environ, {'EPG_IDENTITY_SEED': seed}):
            self.assertEqual(build_mapping.build_identity_map(streams), {'1': 'example.uk'})


if __name__ == '__main__':
    unittest.main()
