"""Measured stream coverage, distinct from mapping candidates and legacy gates."""
import datetime as dt
import gzip
import json
from pathlib import Path
import tempfile
import unittest

from pipeline.coverage_gaps import main as report
from pipeline.guide_health import inspect_guide

NOW = dt.datetime(2026, 9, 26, 12, tzinfo=dt.timezone.utc)


def guide(path, channels, rows):
    from xml.sax.saxutils import escape
    parts = ['<tv>']
    parts += [f'<channel id="{escape(c)}" />' for c in channels]
    parts += [f'<programme channel="{escape(c)}" start="{a}" stop="{b}"><title>{escape(t)}</title></programme>' for c, a, b, t in rows]
    parts += ['</tv>']
    with gzip.open(path, 'wt') as f:
        f.write(''.join(parts))


def row(cid, start, stop, title='Show'):
    return cid, start, stop, title


class MeasuredCoverageTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.streams = [
            {'stream_id': 1, 'name': 'Same', 'cat_name': 'US | General', 'epg_channel_id': ''},
            {'stream_id': 2, 'name': 'Same', 'cat_name': 'CA | General', 'epg_channel_id': ''},
            {'stream_id': 3, 'name': 'Later', 'cat_name': 'US | General', 'epg_channel_id': 'later.us'},
        ]
        self.mapping = {'1': {'name': 'Same', 'canonical_id': 'xtream:1', 'candidates': [{'source': 'test'}]},
                        '3': {'name': 'Later', 'canonical_id': 'later.us', 'candidates': []}}
        self._write('streams.json', self.streams)
        self._write('mapping.json', self.mapping)
        self._write('sources.json', [])
        self._write('index.json', {})
        self._write('coverage.json', {'covered_channels': 99})

    def _write(self, name, value):
        (self.root / name).write_text(json.dumps(value))

    def run_report(self, with_guide=True, private=True):
        args = ['--streams', str(self.root / 'streams.json'), '--mapping', str(self.root / 'mapping.json'),
                '--sources', str(self.root / 'sources.json'), '--sources-index', str(self.root / 'index.json'),
                '--coverage', str(self.root / 'coverage.json'), '--out', str(self.root / 'out.json'),
                '--now', NOW.isoformat()]
        if with_guide:
            args += ['--guide', str(self.root / 'guide.xml.gz')]
        if private:
            args += ['--private-out', str(self.root / 'private.json')]
        report(args)
        public = json.loads((self.root / 'out.json').read_text())
        detail = json.loads((self.root / 'private.json').read_text()) if private else None
        return public, detail

    def test_overlaps_offset_active_future_and_same_name_country_are_stream_scoped(self):
        guide(self.root / 'guide.xml.gz', ['xtream:1', 'later.us'], [
            row('xtream:1', '20260926130000 +0100', '20260926200000 +0100'),
            row('xtream:1', '20260926150000 +0100', '20260927020000 +0100'),
            row('later.us', '20260927120000 +0000', '20260927140000 +0000'),
        ])
        public, private = self.run_report()
        m = public['measured']
        self.assertEqual('available', m['status'])
        self.assertEqual({'linear': 3, 'candidate': 1, 'published': 2, 'active': 1, 'future': 2},
                         {k: m[k] for k in ('linear', 'candidate', 'published', 'active', 'future')})
        self.assertEqual(13, m['usable_hours_next_24h'])
        self.assertEqual(15, m['usable_hours_next_48h'])
        self.assertEqual(1, m['per_country']['CA']['linear'])
        self.assertEqual(0, m['per_country']['CA']['published'])
        self.assertEqual(2, m['per_country']['US']['published'])
        self.assertEqual(3, sum(v['linear'] for v in m['per_country'].values()))
        self.assertEqual(3, sum(v['linear'] for v in m['per_class'].values()))
        self.assertEqual(3, len(private['streams']))
        self.assertEqual(0, private['streams']['2']['usable_hours_next_24h'])
        self.assertEqual(13, private['streams']['1']['usable_hours_next_24h'])
        self.assertNotIn('streams', public)
        self.assertNotIn('xtream:1', json.dumps(public))
        self.assertEqual(0, public['per_country']['CA']['covered'])

    def test_raw_name_metadata_is_not_reported_as_channel_data(self):
        self._write('streams.json', [{'stream_id': 9, 'name': '__raw_names__', 'cat_name': 'UK | General'}])
        self._write('mapping.json', {})
        self._write('sources.json', [{'source': 'sample'}])
        self._write('index.json', {'sample': {'sky showcase': ['hd', 'base'],
            '__raw_names__': {'sky showcase hd': ['hd'], 'sky showcase': ['base']}}})
        guide(self.root / 'guide.xml.gz', [], [])
        public, _ = self.run_report()
        self.assertEqual([], public['uncovered_with_hits'])
        self.assertEqual(1, public['per_source']['sample']['indexed_channels'])

    def test_empty_guide_does_not_turn_candidate_into_coverage(self):
        guide(self.root / 'guide.xml.gz', [], [])
        public, private = self.run_report()
        self.assertEqual(1, public['measured']['candidate'])
        self.assertEqual(0, public['measured']['published'])
        self.assertEqual(0, public['measured']['future'])
        self.assertEqual(3, len(private['streams']))

    def test_missing_guide_is_unavailable_not_positive_and_missing_names_untruncated(self):
        self.streams += [{'stream_id': n, 'name': 'Gap %s' % n, 'cat_name': 'US | General'} for n in range(100, 170)]
        self._write('streams.json', self.streams)
        public, _ = self.run_report(with_guide=False)
        self.assertEqual('unavailable', public['measured']['status'])
        self.assertIsNone(public['measured']['published'])
        self.assertIsNone(public['per_country']['US']['covered'])
        self.assertIsNone(public['covered_channels'])
        self.assertEqual([], public['per_country']['US']['uncovered_names'])
        guide(self.root / 'guide.xml.gz', [], [])
        public, _ = self.run_report()
        self.assertEqual(70, len([n for n in public['per_country']['US']['uncovered_names'] if n.startswith('Gap ')]))

    def test_partial_event_and_placeholder_rows_do_not_inflate_usable_hours(self):
        self.streams.append({'stream_id': 4, 'name': 'Cup Event', 'cat_name': 'US | Event'})
        self._write('streams.json', self.streams)
        guide(self.root / 'guide.xml.gz', ['xtream:1', 'xtream:4'], [
            row('xtream:1', '20260926120000 +0000', '20260926130000 +0000', 'To Be Announced'),
            row('xtream:1', '20260926130000 +0000', '20260926140000 +0000'),
            row('xtream:4', '20260926120000 +0000', '20260926130000 +0000'),
        ])
        public, private = self.run_report()
        self.assertEqual(1, public['measured']['usable_hours_next_24h'])
        self.assertEqual(1, public['measured']['published'])
        self.assertEqual(1, public['measured']['active'])
        self.assertNotIn('4', private['streams'])

    def test_malformed_guide_fails_closed_without_claiming_coverage(self):
        (self.root / 'guide.xml.gz').write_bytes(b'broken gzip')
        public, private = self.run_report()
        self.assertEqual('unavailable', public['measured']['status'])
        self.assertIsNone(public['measured']['active'])
        self.assertIsNone(public['covered_names'])
        self.assertIsNone(private['streams']['1']['published'])

    def test_private_path_cannot_point_into_public_directory(self):
        (self.root / 'public').mkdir()
        guide(self.root / 'guide.xml.gz', [], [])
        args = ['--streams', str(self.root / 'streams.json'), '--mapping', str(self.root / 'mapping.json'),
                '--sources', str(self.root / 'sources.json'), '--sources-index', str(self.root / 'index.json'),
                '--guide', str(self.root / 'guide.xml.gz'), '--out', str(self.root / 'public' / 'coverage_gaps.json'),
                '--private-out', str(self.root / 'public' / 'stream_details.json')]
        with self.assertRaises(SystemExit):
            report(args)
        self.assertFalse((self.root / 'public' / 'stream_details.json').exists())

    def test_guide_health_keeps_legacy_any_intersection_and_adds_union_diagnostics(self):
        guide(self.root / 'guide.xml.gz', ['a'], [
            row('a', '20260926120000 +0000', '20260926123000 +0000'),
            row('a', '20260926121500 +0000', '20260926130000 +0000'),
        ])
        health = inspect_guide(self.root / 'guide.xml.gz', now=NOW)
        self.assertEqual(1, health['channels_next_24h'])
        self.assertEqual(1, health['active_channels'])
        self.assertEqual(1, health['usable_hours_next_24h'])
        self.assertEqual(1, health['usable_hours_next_48h'])
        self.assertNotIn('per_channel', health)


if __name__ == '__main__':
    unittest.main()
