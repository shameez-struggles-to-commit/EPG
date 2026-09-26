"""Transport and playlist regressions: no real network access."""
import contextlib
import io
import json
import os
import tempfile
import unittest
from unittest import mock
from urllib.error import HTTPError
from urllib.request import Request

from pipeline import fetch_provider, make_tivimate_m3u
from pipeline import provider_http


class ProviderHttpsTest(unittest.TestCase):
    def test_authenticated_url_encodes_credential_components(self):
        url = provider_http.metadata_url('panel.example:443', 'u&+ /', 'p?#%', 'player_api.php', action='get_live_streams')
        self.assertEqual('https://panel.example:443/player_api.php?username=u%26%2B+%2F&password=p%3F%23%25&action=get_live_streams', url)

    def test_rejects_insecure_or_malformed_server_and_proto(self):
        for server in ('http://panel.example', 'panel.example/path', 'name:pass@panel.example', 'panel.example?x=y', 'panel.example#frag'):
            with self.subTest(server=server), self.assertRaises(ValueError):
                provider_http.metadata_url(server, 'u', 'p', 'player_api.php')
        with self.assertRaises(ValueError):
            provider_http.metadata_url('panel.example', 'u', 'p', 'player_api.php', proto='http')

    def test_rejects_plaintext_initial_request_before_network(self):
        with mock.patch('urllib.request.build_opener') as opener:
            with self.assertRaises(ValueError):
                provider_http.fetch('http://panel.example/player_api.php?username=secret')
            opener.assert_not_called()

    def test_cross_origin_guide_redirect_requires_credential_free_https(self):
        old = Request('https://panel.example/xmltv.php?username=alice&password=private', headers={'Authorization': 'Basic private', 'Cookie': 'session=private'})
        handler = provider_http.SafeRedirectHandler(allow_cross_origin_guide=True)
        target = handler.redirect_request(old, None, 301, 'Moved', {}, 'https://guide.example/guide.xml')
        self.assertEqual('https://guide.example/guide.xml', target.full_url)
        self.assertNotIn('Authorization', target.headers)
        self.assertNotIn('Cookie', target.headers)
        for url in ('http://guide.example/guide.xml', 'https://guide.example/guide.xml?token=private', 'https://alice@guide.example/guide.xml', 'https://guide.example/guide.xml#fragment', 'https://guide.example/alice/private/guide.xml'):
            with self.subTest(target=url), self.assertRaises(ValueError):
                handler.redirect_request(old, None, 301, 'Moved', {}, url)

    def test_cross_origin_metadata_redirect_is_rejected(self):
        old = Request('https://panel.example/player_api.php?username=alice&password=private')
        with self.assertRaises(ValueError):
            provider_http.SafeRedirectHandler().redirect_request(old, None, 302, 'Moved', {}, 'https://other.example/data')

    def test_same_origin_downgrade_is_rejected(self):
        old = Request('https://panel.example/xmltv.php?username=alice&password=private')
        with self.assertRaises(ValueError):
            provider_http.SafeRedirectHandler(allow_cross_origin_guide=True).redirect_request(old, None, 302, 'Moved', {}, 'http://panel.example/xmltv.php')

    def test_errors_and_retry_logs_do_not_disclose_url(self):
        secret = 'password=topsecret'
        url = f'https://panel.example/xmltv.php?{secret}'
        with mock.patch('urllib.request.build_opener') as builder:
            builder.return_value.open.side_effect = HTTPError(url, 503, f'failed {secret}', {}, None)
            with self.assertRaises(Exception) as caught:
                provider_http.fetch(url)
            self.assertNotIn(secret, str(caught.exception))
            self.assertNotIn(url, str(caught.exception))
        output = io.StringIO()
        with contextlib.redirect_stderr(output):
            with self.assertRaises(HTTPError):
                fetch_provider.fetch_with_retry(url, attempts=2, delay=0, fetcher=lambda url, timeout: (_ for _ in ()).throw(HTTPError(url, 503, secret, {}, None)))
        self.assertNotIn(secret, output.getvalue())
        self.assertNotIn(url, output.getvalue())

    def test_fetcher_uses_safe_guide_redirect_policy_only_for_xmltv(self):
        with mock.patch.object(provider_http, 'fetch', return_value=b'{}') as fetch:
            fetch_provider.fetch('https://panel.example/player_api.php?username=a')
            fetch_provider.fetch('https://panel.example/xmltv.php?username=a')
        self.assertEqual([False, True], [call.kwargs['allow_cross_origin_guide'] for call in fetch.call_args_list])

    def test_playlist_uses_enriched_category_without_network(self):
        with tempfile.TemporaryDirectory(dir='/Users/shameez/.hermes/cache/scratch') as tmp:
            streams = os.path.join(tmp, 'streams.json')
            auth = os.path.join(tmp, 'auth.json')
            output = os.path.join(tmp, 'out.m3u')
            with open(streams, 'w') as file:
                json.dump([{'stream_id': 12, 'name': 'Channel', 'cat_name': 'News & Talk', 'category_id': '7', 'epg_channel_id': 'channel.example'}], file)
            with open(auth, 'w') as file:
                json.dump({'server_info': {'url': 'panel.example'}, 'user_info': {'username': 'u', 'password': 'p'}}, file)
            log = io.StringIO()
            with mock.patch('sys.argv', ['make_tivimate_m3u.py', streams, auth, output]), mock.patch('urllib.request.urlopen', side_effect=AssertionError('network attempted')) as network, contextlib.redirect_stdout(log):
                make_tivimate_m3u.main()
            network.assert_not_called()
            self.assertNotIn('categories unavailable', log.getvalue())
            with open(output) as file:
                text = file.read()
            self.assertIn('group-title="News &amp; Talk"', text)
            self.assertIn('https://panel.example/live/u/p/12.ts', text)


if __name__ == '__main__':
    unittest.main()
