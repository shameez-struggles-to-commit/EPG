"""The required DStv fetch must preserve the source's available later days."""
import datetime as dt
import importlib.util
import pathlib
import sys
from unittest import TestCase
from unittest.mock import patch
import xml.etree.ElementTree as ET

ROOT = pathlib.Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location('fetch_dstv', ROOT / 'pipeline/fetch_dstv.py')
assert spec is not None and spec.loader is not None
dstv = importlib.util.module_from_spec(spec)
spec.loader.exec_module(dstv)


class DstvHorizonTest(TestCase):
    def test_default_fetch_requests_four_days_and_keeps_day_four_rows(self):
        today = dt.datetime.now(dt.timezone.utc).date()
        urls = []

        def response(url):
            urls.append(url)
            date = url.split('d=')[1].split('&')[0]
            start = dt.datetime.fromisoformat(date).replace(tzinfo=dt.timezone(dt.timedelta(hours=2)))
            stop = start + dt.timedelta(hours=1)
            return {'Channels': [{'Name': 'BBC Brit', 'Programmes': [{
                'StartTime': start.isoformat(), 'EndTime': stop.isoformat(),
                'Title': 'Actual programme'}]}]}

        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            out = pathlib.Path(tmp) / 'dstv.xml'
            with patch.object(sys, 'argv', ['fetch_dstv.py', str(out)]), \
                    patch.object(dstv, 'http_json', side_effect=response), \
                    patch.object(dstv.time, 'sleep'):
                dstv.main()
            days = [(today + dt.timedelta(days=n)).isoformat() for n in range(4)]
            self.assertEqual([f'd={day}&country=zaf' for day in days],
                             [url.split('?')[1] for url in urls])
            rows = ET.parse(out).findall('programme')
            self.assertEqual(4, len(rows))
            fourth_start = (dt.datetime.combine(today + dt.timedelta(days=3), dt.time(),
                                                tzinfo=dt.timezone(dt.timedelta(hours=2)))
                            .astimezone(dt.timezone.utc).strftime('%Y%m%d%H%M%S +0000'))
            self.assertEqual(fourth_start, rows[-1].get('start'))

    def test_workflow_uses_fetcher_default_without_two_day_override(self):
        workflow = (ROOT / '.github/workflows/build-epg.yml').read_text()
        self.assertIn('run_fetch dstv      1 python3 pipeline/fetch_dstv.py data/dstv.xml', workflow)
        self.assertNotIn('fetch_dstv.py data/dstv.xml --days 2', workflow)
