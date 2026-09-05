import datetime as dt
import gzip
import pathlib
import tempfile
import unittest

from pipeline.guide_health import compare_guides, inspect_guide


def write_guide(path, channels, programmes):
    rows = ['<tv>']
    rows.extend(f'<channel id="{channel}"><display-name>{channel}</display-name></channel>' for channel in channels)
    rows.extend(
        f'<programme start="{start}" stop="{stop}" channel="{channel}"><title>Show</title></programme>'
        for channel, start, stop in programmes
    )
    rows.append('</tv>')
    with gzip.open(path, 'wt', encoding='utf-8') as handle:
        handle.write('\n'.join(rows))


class GuideHealthTest(unittest.TestCase):
    def test_workflow_has_always_run_final_status_without_ci_notification(self):
        workflow = pathlib.Path('.github/workflows/build-epg.yml').read_text(encoding='utf-8')
        self.assertIn('final-status:', workflow)
        self.assertIn('if: always()', workflow)
        self.assertNotIn('NTFY_TOPIC: ${{ secrets.NTFY_TOPIC }}', workflow)
        self.assertIn('epg-diagnostics-${{ github.run_attempt }}', workflow)
        self.assertIn('"$URL" || true', workflow)
        self.assertNotIn('hermes-shameez-', workflow)
        self.assertNotIn('Authorization: ***', workflow)

    def test_inspect_counts_channels_programmes_and_next_24_hours(self):
        now = dt.datetime(2026, 9, 1, 12, tzinfo=dt.timezone.utc)
        with tempfile.TemporaryDirectory() as directory:
            path = pathlib.Path(directory) / 'guide.xml.gz'
            write_guide(path, ['a', 'b'], [
                ('a', '20260901110000 +0000', '20260901130000 +0000'),
                ('b', '20260903110000 +0000', '20260903130000 +0000'),
            ])
            result = inspect_guide(path, now=now)
        self.assertEqual(2, result['channels'])
        self.assertEqual(2, result['programmes'])
        self.assertEqual(1, result['channels_next_24h'])

    def test_comparison_rejects_material_channel_loss(self):
        previous = {'channels': 100, 'programmes': 1000, 'channels_next_24h': 80}
        candidate = {'channels': 94, 'programmes': 1000, 'channels_next_24h': 80}
        failures = compare_guides(candidate, previous, absolute_minimums={})
        self.assertTrue(any('channels' in failure for failure in failures))

    def test_comparison_rejects_material_current_coverage_loss(self):
        previous = {'channels': 100, 'programmes': 1000, 'channels_next_24h': 80}
        candidate = {'channels': 100, 'programmes': 1000, 'channels_next_24h': 71}
        failures = compare_guides(candidate, previous, absolute_minimums={})
        self.assertTrue(any('24h' in failure for failure in failures))

    def test_comparison_allows_small_daily_variation(self):
        previous = {'channels': 100, 'programmes': 1000, 'channels_next_24h': 80}
        candidate = {'channels': 98, 'programmes': 900, 'channels_next_24h': 76}
        self.assertEqual([], compare_guides(candidate, previous, absolute_minimums={}))

    def test_comparison_rejects_low_absolute_24h_coverage(self):
        previous = {'channels': 0, 'programmes': 0, 'channels_next_24h': 0}
        candidate = {'channels': 2000, 'programmes': 120000, 'channels_next_24h': 1999}
        failures = compare_guides(candidate, previous)
        self.assertTrue(any('absolute minimum' in failure for failure in failures))


if __name__ == '__main__':
    unittest.main()

