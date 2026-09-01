import unittest
from unittest import mock

from pipeline import fetch_provider


class ProviderFetchRetryTest(unittest.TestCase):
    def test_retries_transient_failure_then_returns_data(self):
        operation = mock.Mock(side_effect=[TimeoutError("slow"), b"<tv></tv>"])
        result = fetch_provider.fetch_with_retry(
            "https://example/xmltv.php", timeout=1, attempts=3,
            delay=0, fetcher=operation,
        )
        self.assertEqual(b"<tv></tv>", result)
        self.assertEqual(2, operation.call_count)

    def test_raises_after_attempt_limit(self):
        operation = mock.Mock(side_effect=TimeoutError("slow"))
        with self.assertRaises(TimeoutError):
            fetch_provider.fetch_with_retry(
                "https://example/xmltv.php", timeout=1, attempts=3,
                delay=0, fetcher=operation,
            )
        self.assertEqual(3, operation.call_count)


if __name__ == "__main__":
    unittest.main()
