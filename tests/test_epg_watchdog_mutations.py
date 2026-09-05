import unittest

from tests import run_epg_watchdog_mutations as mutation_runner


class MutationSelectorValidationTest(unittest.TestCase):
    def test_real_selector_loads_as_exactly_one_test(self):
        selector = (
            "tests.test_epg_watchdog_mutations."
            "MutationSelectorValidationTest."
            "test_real_selector_loads_as_exactly_one_test"
        )
        self.assertTrue(mutation_runner._selector_loads_exactly_one_test(selector))

    def test_stale_selector_is_rejected_even_when_unittest_creates_failed_test(self):
        stale_selector = (
            "tests.test_epg_github_watchdog.ControllerEndToEndTest."
            "test_ntfy_error_leaves_terminal_message_for_the_next_tick"
        )
        loader = unittest.TestLoader()
        suite = loader.loadTestsFromName(stale_selector)
        self.assertEqual(suite.countTestCases(), 1)
        self.assertTrue(loader.errors)
        self.assertFalse(
            mutation_runner._selector_loads_exactly_one_test(stale_selector)
        )


if __name__ == "__main__":
    unittest.main()
