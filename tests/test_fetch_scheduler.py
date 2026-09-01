import pathlib
import unittest


WORKFLOW = pathlib.Path(__file__).resolve().parents[1] / '.github/workflows/build-epg.yml'


class FetchSchedulerTest(unittest.TestCase):
    def test_schedule_avoids_start_of_hour(self):
        text = WORKFLOW.read_text(encoding='utf-8')
        self.assertIn("cron: '17 4 * * *'", text)
        self.assertNotIn("cron: '0 4 * * *'", text)

    def test_custom_fetchers_start_before_iptv_waits(self):
        text = WORKFLOW.read_text(encoding='utf-8')
        custom = text.index('run_fetch skyhawk')
        final_wait = text.index('while [ "${#IO_PIDS[@]}" -gt 0 ]')
        self.assertLess(custom, final_wait)

    def test_scheduler_waits_for_any_completed_pid(self):
        workflow = WORKFLOW.read_text(encoding='utf-8')
        helper = (WORKFLOW.parents[2] / 'pipeline/fetch_scheduler.sh').read_text(encoding='utf-8')
        self.assertIn('source "$WS/pipeline/fetch_scheduler.sh"', workflow)
        self.assertIn('wait -n -p', helper)
        self.assertNotIn('reap_first()', workflow)


if __name__ == '__main__':
    unittest.main()
