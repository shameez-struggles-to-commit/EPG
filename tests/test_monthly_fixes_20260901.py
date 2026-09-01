"""Regression tests for the 2026-09-01 monthly-audit fixes:

1. WRONG_FEED_DENY — identity-wrong candidates are dropped by stream_id.
2. Radio allowlist — keys must be COMPUTED norm() forms (hand-written
   'heart 80s' never equals norm output 'heart 80 s'), and categoryless
   allowlisted radio names are no longer skipped as unnamed radio.
3. PH diaspora — exact Filipino diaspora matches pass, generic brand
   names (Comedy Central, "One") do not.
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', 'pipeline'))

from matcher import norm, RADIO_LINEAR_NORMS, is_non_linear  # noqa: E402
from build_mapping import (  # noqa: E402
    WRONG_FEED_DENY,
    diaspora_allowed,
    DIASPORA_NAME_RES,
)


class WrongFeedDenyTest(unittest.TestCase):
    def test_table_covers_audited_streams(self):
        for sid in ('618063', '618068', '618097', '618051', '175549'):
            self.assertIn(sid, WRONG_FEED_DENY, sid)

    def test_entries_are_source_id_pairs(self):
        for sid, denials in WRONG_FEED_DENY.items():
            for d in denials:
                self.assertEqual(2, len(d), (sid, d))
                self.assertTrue(d[0] and d[1], (sid, d))


class RadioAllowlistTest(unittest.TestCase):
    def test_keys_are_norm_forms_not_handwritten(self):
        # norm('Heart 80s') = 'heart 80 s' (digit/letter boundary split).
        # A hand-written 'heart 80s' key can never match — the bug fixed
        # 2026-09-01.
        self.assertIn(norm('Heart 80s'), RADIO_LINEAR_NORMS)
        self.assertIn(norm('BBC Radio 6 Music'), RADIO_LINEAR_NORMS)
        self.assertIn(norm('Absolute Radio 80s'), RADIO_LINEAR_NORMS)
        self.assertNotIn('heart 80s', RADIO_LINEAR_NORMS)

    def test_radio_category_allowlist_still_linear(self):
        self.assertFalse(is_non_linear('Radio', 'Heart 80s'))
        self.assertFalse(is_non_linear('Radio', 'BBC Radio 6 Music'))

    def test_unknown_radio_stays_non_linear(self):
        self.assertTrue(is_non_linear('Radio', 'Some Random FM'))


class PHDiasporaTest(unittest.TestCase):
    def test_distinctive_filipino_names_pass(self):
        self.assertTrue(diaspora_allowed('epgshare01:US2', 'PH', 'GMA Pinoy TV'))
        self.assertTrue(diaspora_allowed('epgshare01:US2', 'PH', 'ABS CBN NEWS'))
        self.assertTrue(diaspora_allowed('epgshare01:CA2', 'PH', 'GMA Pinoy'))

    def test_generic_brands_blocked(self):
        self.assertFalse(diaspora_allowed('epgshare01:US2', 'PH', 'Comedy Central'))
        self.assertFalse(diaspora_allowed('epgshare01:US2', 'PH', 'One'))
        self.assertFalse(diaspora_allowed('epgshare01:US2', 'PH', 'Disney Channel'))

    def test_ungated_countries_unaffected(self):
        # PK diaspora has no name gate: legacy behaviour preserved.
        self.assertTrue(diaspora_allowed('epgshare01:UK1', 'PK', 'Samaa TV'))
        self.assertFalse(diaspora_allowed('epgshare01:UK1', 'PH', 'GMA Pinoy TV')
                         or 'epgshare01:UK1' in ())


if __name__ == '__main__':
    unittest.main()
