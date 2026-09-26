import unittest
from ops import tivimate_refresh


class PlaylistVerificationTest(unittest.TestCase):
    def test_same_count_wrong_playlist_is_rejected(self):
        wanted = b'#EXTM3U\n#EXTINF:-1 tvg-id="a",A\nhttps://example.test/1\n'
        wrong = wanted.replace(b'tvg-id="a"', b'tvg-id="b"')
        self.assertFalse(tivimate_refresh.exact_roundtrip(wanted, wrong))
        self.assertTrue(tivimate_refresh.exact_roundtrip(wanted, wanted))

    def test_publisher_retries_stale_bytes_and_requires_exact_readback(self):
        from unittest.mock import Mock
        fetch = Mock(side_effect=[b'old', b'wrong', b'new'])
        send = Mock()
        release = Mock(return_value={'version': 1})
        result = tivimate_refresh.publish_verified(b'new', fetch, send, release,
                                                   {'version': 1}, sleep=lambda _: None)
        self.assertEqual(result, 'updated')
        send.assert_called_once_with(b'new')
        self.assertEqual(fetch.call_count, 3)

    def test_playlist_requires_every_guide_id_and_unique_entries(self):
        playlist = b'#EXTM3U\n#EXTINF:-1 tvg-id="a&amp;b",A\nhttps://example.test/1\n'
        self.assertEqual(tivimate_refresh.validate_playlist(playlist, {'a&b'}, 1), {'a&b'})
        with self.assertRaises(ValueError):
            tivimate_refresh.validate_playlist(playlist, {'missing'}, 1)
        with self.assertRaises(ValueError):
            tivimate_refresh.validate_playlist(playlist + playlist.split(b'\n', 1)[1], {'a&b'}, 2)

    def test_real_refresh_path_dry_run_never_publishes(self):
        import base64, gzip, json, tempfile
        from pathlib import Path
        from unittest.mock import patch
        from pipeline.release_manifest import create_manifest
        seed = {'schema': 1, 'overrides': {'1': 'a'}}
        guide = gzip.compress(b'<tv><channel id="a"><display-name>A</display-name></channel></tv>')
        release = create_manifest(guide, seed, 'a' * 40, '123')
        expected = b'#EXTM3U\n#EXTINF:-1 tvg-id="a",A\nhttps://example.test/1\n'
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / 'seed').write_text(base64.b64encode(gzip.compress(json.dumps(seed).encode())).decode())
            (root / 'auth').write_text(json.dumps({'server_info': {'url': 'example.test'}, 'user_info': {'username': 'fixture', 'password': 'fixture'}}))
            config = {'repo_dir': str(root), 'work_dir': str(root / 'work'), 'auth_file': str(root / 'auth'), 'identity_file': str(root / 'seed'), 'guide_url': 'https://guide.test/guide.xml.gz', 'gist_id': 'a' * 32, 'gist_file': 'playlist.m3u', 'minimum_entries': 1, 'gh': '/fixture/gh'}
            def command(args, **kwargs):
                if 'fetch_provider.py' in str(args):
                    dest = Path(args[-1]); (dest / 'streams.json').write_text('[{"stream_id":1,"name":"A"}]')
                elif 'make_tivimate_m3u.py' in str(args):
                    Path(args[-1]).write_bytes(expected)
                else:
                    self.fail('Dry run must not invoke GitHub writes')
                return b''
            def fetch(url):
                return json.dumps(release).encode() if 'release.json' in url else guide
            with patch.object(tivimate_refresh, 'run_command', side_effect=command), patch.object(tivimate_refresh, 'fetch_bytes', side_effect=fetch):
                result = tivimate_refresh.run_refresh(config, dry_run=True)
            self.assertEqual(result['outcome'], 'dry-run')
            self.assertEqual(result['guide_ids'], 1)

    def test_changed_release_stops_before_write(self):
        from unittest.mock import Mock
        send = Mock()
        with self.assertRaises(ValueError):
            tivimate_refresh.publish_verified(b'new', lambda: b'old', send,
                                               lambda: {'version': 2}, {'version': 1})
        send.assert_not_called()


if __name__ == '__main__':
    unittest.main()
