"""The build log must not describe guide-ID/name counts as coverage."""
import datetime as dt
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class BuildPipelineReportingTests(unittest.TestCase):
    def test_log_reports_guide_ids_and_linear_names_separately(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            streams = [
                {'stream_id': '1', 'name': 'Alpha', 'cat_name': 'UK | General'},
                {'stream_id': '2', 'name': 'Beta', 'cat_name': 'UK | General'},
            ]
            mapping = {'1': {'canonical_id': 'alpha.id', 'candidates': [
                {'source': 'pk', 'source_id': 'pk-alpha', 'method': 'exact'}]}}
            now = dt.datetime.now(dt.timezone.utc)
            pk = {'pk-alpha': [{'start': (now - dt.timedelta(minutes=5)).strftime('%Y%m%d%H%M%S +0000'),
                                'stop': (now + dt.timedelta(hours=2)).strftime('%Y%m%d%H%M%S +0000'),
                                'title': 'News'}]}
            for name, data in [('streams', streams), ('mapping', mapping),
                               ('sources', []), ('pk', pk)]:
                (root / f'{name}.json').write_text(json.dumps(data))
            result = subprocess.run([
                sys.executable, str(ROOT / 'pipeline/build_pipeline.py'),
                '--streams', str(root / 'streams.json'),
                '--mapping', str(root / 'mapping.json'),
                '--sources', str(root / 'sources.json'),
                '--pk', str(root / 'pk.json'),
                '--out', str(root / 'guide.xml'),
                '--coverage-out', str(root / 'coverage.json'),
            ], check=True, capture_output=True, text=True)
            coverage = json.loads((root / 'coverage.json').read_text())
            self.assertEqual(coverage['covered_channels'], 1)
            self.assertEqual(coverage['linear_unique_names'], 2)
            self.assertIn('1 guide IDs', result.stdout)
            self.assertIn('2 linear unique names', result.stdout)
            self.assertNotIn('linear unique names covered', result.stdout)
            self.assertNotIn('%', result.stdout)


if __name__ == '__main__':
    unittest.main()
