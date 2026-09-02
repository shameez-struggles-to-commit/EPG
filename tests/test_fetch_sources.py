"""Tests for fetch_sources retry, host circuit breaker, and status tracking.

Network-free: download is injected as an explicit argument (never via module
patching, which the default-arg binding in fetch_source_with_retry would
silently bypass). Covers the 2026-09-02 incident class (epgshare01 host-wide
404) — retries must be bounded and must not amplify across the many files
that share one host."""
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'pipeline'))

import fetch_sources  # noqa: E402

EMPTY_ENV = {
    'ESHARE_FILES': '',
    'IPTV_ORG_FILES': '',
    'SKYHAWK_FILE': '', 'DSTV_FILE': '', 'EPGONE_FILE': '',
    'ALLENTE_FILE': '', 'TEAMS_FILE': '', 'BBCRADIO_FILE': '',
}


FEED = b'<tv></tv>'


def write_feed(path, body=FEED):
    os.makedirs(os.path.dirname(path) or '.', exist_ok=True)
    with open(path, 'wb') as f:
        f.write(body)
    return len(body)


class TestFetchSourceWithRetry(unittest.TestCase):
    def test_success_first_try(self):
        calls = []

        def dl(url, dest, timeout=300, insecure=False):
            calls.append(url)
            return write_feed(dest)

        sleeps = []
        with mock.patch.object(fetch_sources.time, 'sleep', side_effect=sleeps.append):
            n, used = fetch_sources.fetch_source_with_retry(
                'https://h/f.xml.gz', '/tmp/x.xml.gz', attempts=3, delay=5.0, download=dl)
        self.assertEqual(n, len(FEED))
        self.assertEqual(used, 1)
        self.assertEqual(len(calls), 1)
        self.assertEqual(sleeps, [])

    def test_transient_failure_then_success(self):
        calls = []

        def dl(url, dest, timeout=300, insecure=False):
            calls.append(url)
            if len(calls) < 2:
                raise OSError('HTTP Error 404: Not Found')
            return write_feed(dest)

        sleeps = []
        with mock.patch.object(fetch_sources.time, 'sleep', side_effect=sleeps.append):
            n, used = fetch_sources.fetch_source_with_retry(
                'https://h/f.xml.gz', '/tmp/x.xml.gz', attempts=3, delay=5.0, download=dl)
        self.assertEqual(n, len(FEED))
        self.assertEqual(used, 2)
        self.assertEqual(len(calls), 2)
        self.assertEqual(sleeps, [5.0])

    def test_exhaustion_raises_last_error(self):
        def dl(url, dest, timeout=300, insecure=False):
            raise OSError('HTTP Error 404: Not Found')

        sleeps = []
        with mock.patch.object(fetch_sources.time, 'sleep', side_effect=sleeps.append):
            with self.assertRaises(OSError):
                fetch_sources.fetch_source_with_retry(
                    'https://h/f.xml.gz', '/tmp/x.xml.gz', attempts=3, delay=5.0, download=dl)
        self.assertEqual(sleeps, [5.0, 10.0])


class TestHostBreaker(unittest.TestCase):
    def run_main(self, td, dl):
        env = dict(EMPTY_ENV)
        env['ESHARE_FILES'] = 'A1,B1,C1,D1,E1'
        with mock.patch.object(fetch_sources, 'download', dl), \
             mock.patch.object(fetch_sources.time, 'sleep'), \
             mock.patch.dict(os.environ, env):
            fetch_sources.main()
        return json.load(open(os.path.join(td, 'fetch_status.json')))

    def test_breaker_opens_after_three_failures(self):
        calls = []

        def dl(url, dest, timeout=300, insecure=False):
            calls.append(url)
            raise OSError('HTTP Error 404: Not Found')

        with tempfile.TemporaryDirectory() as td:
            # main() takes the outdir as argv[1]
            with mock.patch.object(sys, 'argv', ['fetch_sources.py', td]):
                entries = self.run_main(td, dl)
        by = {e['source']: e for e in entries}
        # EPG_PW/TVEPG/... URLs use their real hosts — they fail too, but on
        # DIFFERENT hosts, so the epgshare01 breaker must count only its own.
        es = [e for e in entries if e['source'].startswith('epgshare01:')]
        self.assertEqual([e['status'] for e in es],
                         ['failed', 'failed', 'failed', 'skipped', 'skipped'])
        self.assertEqual(es[3]['attempts'], 0)
        # only A1,B1,C1 hit the network (3 attempts each); epg.pw etc. also
        # fail (their own host counters) — but no epgshare URL past C1 does
        es_urls = [u for u in calls if 'epg_ripper' in u]
        self.assertEqual(len(es_urls), 9)

    def test_success_resets_breaker(self):
        state = {'n': 0}

        def dl(url, dest, timeout=300, insecure=False):
            state['n'] += 1
            # fail exactly the first two epgshare files (2 x 3 attempts = 6)
            if 'epg_ripper_A1' in url or 'epg_ripper_B1' in url:
                raise OSError('HTTP Error 404: Not Found')
            return write_feed(dest)

        with tempfile.TemporaryDirectory() as td:
            with mock.patch.object(sys, 'argv', ['fetch_sources.py', td]):
                entries = self.run_main(td, dl)
        by = {e['source']: e for e in entries}
        self.assertEqual(by['epgshare01:A1']['status'], 'failed')
        self.assertEqual(by['epgshare01:B1']['status'], 'failed')
        self.assertEqual(by['epgshare01:C1']['status'], 'ok')
        self.assertEqual(by['epgshare01:D1']['status'], 'ok')
        self.assertEqual(by['epgshare01:E1']['status'], 'ok')


class TestStatusShape(unittest.TestCase):
    def test_tracker_entries_allowlisted(self):
        tr = fetch_sources.SourceFetchTracker('/tmp')
        tr.record('epgshare01:UK1', 'https://epgshare01.online/x.gz', 'failed',
                  attempts=3, error=OSError('HTTP Error 404: Not Found ' + 'x' * 300))
        e = tr.entries[-1]
        self.assertEqual(e['source'], 'epgshare01:UK1')
        self.assertEqual(e['host'], 'epgshare01.online')
        self.assertEqual(e['status'], 'failed')
        self.assertEqual(e['attempts'], 3)
        self.assertLessEqual(len(e['error']), 200)
        self.assertEqual(sorted(e.keys()),
                         ['attempts', 'error', 'host', 'source', 'status'])


if __name__ == '__main__':
    unittest.main()
