import contextlib
import datetime
import gzip
import hashlib
import http.client
import io
import json
import pathlib
import sys
import unittest
import urllib.error
from unittest.mock import patch


ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "pipeline"))

import verify_release_health as health


EXPECTED_RUN_ID = "38159214832"
REVISION = "e3bb3a70c70543f9dfd555ce055faac4db6c203b"
TODAY = "20261012"

_FRESH_GUIDE = None


def make_guide(channels=1500, programmes=100000, stop="20261016"):
    """A minimal guide that satisfies every published minimum."""
    rows = ['<?xml version="1.0"?><tv>']
    for index in range(channels):
        rows.append('<channel id="ch%d"/>' % index)
    for index in range(programmes):
        token = hashlib.sha1(str(index).encode("utf-8")).hexdigest()[:16]
        rows.append(
            '<programme channel="ch%d" start="20261012120000 +0000" '
            'stop="%s120000 +0000"><title>%s</title></programme>'
            % (index % channels, stop, token)
        )
    rows.append("</tv>")
    return gzip.compress("".join(rows).encode("utf-8"))


def fresh_guide():
    global _FRESH_GUIDE
    if _FRESH_GUIDE is None:
        _FRESH_GUIDE = make_guide()
    return _FRESH_GUIDE


def release_bytes(run_id, revision, guide, schema=1):
    return json.dumps({
        "schema": schema,
        "guide_sha256": hashlib.sha256(guide).hexdigest(),
        "revision": revision,
        "run_id": str(run_id),
    }).encode("utf-8")


class FakeClock:
    def __init__(self, start=0.0):
        self.now = start

    def __call__(self):
        return self.now


def make_sleep(clock, sleeps):
    def sleep(seconds):
        sleeps.append(seconds)
        clock.now += seconds
    return sleep


class HealthRetryTest(unittest.TestCase):
    def test_stale_release_recovers_within_budget(self):
        fresh = fresh_guide()
        stale = b"previous release"
        attempts_data = [
            (release_bytes("111", "f" * 40, stale), stale),
            (release_bytes("111", "f" * 40, stale), stale),
            (release_bytes(EXPECTED_RUN_ID, REVISION, fresh), fresh),
        ]
        calls = []
        clock = FakeClock()
        sleeps = []

        def fetch(url, timeout):
            release, guide = attempts_data[len(calls) // 2]
            calls.append((url, timeout))
            return (200, release if "release.json" in url else guide)

        ok, attempts, failures = health.run_verified_health_check(
            "https://pages.test/EPG",
            EXPECTED_RUN_ID,
            REVISION,
            fetch=fetch,
            clock=clock,
            sleep=make_sleep(clock, sleeps),
            today=TODAY,
        )

        self.assertTrue(ok)
        self.assertEqual(attempts, 3)
        self.assertEqual(failures, [])
        self.assertEqual(sleeps, [15.0, 15.0])
        urls = [url for url, _ in calls]
        self.assertEqual(len(urls), 6)
        self.assertTrue(all(url.startswith("https://pages.test/EPG/") for url in urls))
        self.assertTrue(any("/release.json?verify=" in url for url in urls))
        self.assertTrue(any("/guide.xml.gz?verify=" in url for url in urls))
        self.assertEqual(len({url.split("verify=")[1] for url in urls}), 3)

    def test_permanent_mismatch_fails_closed_at_budget(self):
        stale = b"previous release"
        mismatch = (release_bytes("111", "f" * 40, stale), stale)
        calls = []
        clock = FakeClock()
        sleeps = []

        def fetch(url, timeout):
            calls.append((url, timeout))
            release, guide = mismatch
            return (200, release if "release.json" in url else guide)

        ok, attempts, failures = health.run_verified_health_check(
            "https://pages.test/EPG",
            EXPECTED_RUN_ID,
            REVISION,
            fetch=fetch,
            clock=clock,
            sleep=make_sleep(clock, sleeps),
            total_budget=30.0,
            attempt_interval=15.0,
            today=TODAY,
        )

        self.assertFalse(ok)
        self.assertEqual(attempts, 2)
        self.assertEqual(sleeps, [15.0, 15.0])
        self.assertTrue(any("run_id" in failure for failure in failures), failures)
        self.assertTrue(any("111" in failure for failure in failures), failures)
        self.assertEqual(len(calls), 4)

    def test_http_work_consumes_total_budget(self):
        stale = b"previous release"
        mismatch = (release_bytes("111", "f" * 40, stale), stale)
        clock = FakeClock()
        sleeps = []

        def fetch(url, timeout):
            clock.now += 20.0
            release, guide = mismatch
            return (200, release if "release.json" in url else guide)

        ok, attempts, failures = health.run_verified_health_check(
            "https://pages.test/EPG",
            EXPECTED_RUN_ID,
            REVISION,
            fetch=fetch,
            clock=clock,
            sleep=make_sleep(clock, sleeps),
            total_budget=50.0,
            attempt_interval=15.0,
            today=TODAY,
        )

        self.assertFalse(ok)
        self.assertEqual(attempts, 1)
        self.assertEqual(sleeps, [10.0])

    def test_request_timeout_is_clamped_to_remaining_budget(self):
        stale = b"previous release"
        mismatch = (release_bytes("111", "f" * 40, stale), stale)
        timeouts = []
        clock = FakeClock()
        sleeps = []

        def fetch(url, timeout):
            timeouts.append(timeout)
            clock.now += 5.0
            release, guide = mismatch
            return (200, release if "release.json" in url else guide)

        ok, attempts, failures = health.run_verified_health_check(
            "https://pages.test/EPG",
            EXPECTED_RUN_ID,
            REVISION,
            fetch=fetch,
            clock=clock,
            sleep=make_sleep(clock, sleeps),
            total_budget=40.0,
            attempt_interval=15.0,
            request_timeout=30.0,
            today=TODAY,
        )

        self.assertFalse(ok)
        self.assertEqual(attempts, 2)
        self.assertEqual(timeouts, [30.0, 30.0, 15.0, 10.0])
        self.assertEqual(sleeps, [15.0, 5.0])

    def test_transport_and_http_failures_are_retried(self):
        fresh = fresh_guide()
        manifest = release_bytes(EXPECTED_RUN_ID, REVISION, fresh)
        for scenario in ("transport", "http-503"):
            with self.subTest(scenario=scenario):
                clock = FakeClock()
                sleeps = []
                calls = []

                def fetch(url, timeout):
                    calls.append((url, timeout))
                    if len(calls) == 1:
                        if scenario == "transport":
                            raise urllib.error.URLError("temporary TLS failure")
                        return (503, b"")
                    return (200, manifest if "release.json" in url else fresh)

                ok, attempts, failures = health.run_verified_health_check(
                    "https://pages.test/EPG",
                    EXPECTED_RUN_ID,
                    REVISION,
                    fetch=fetch,
                    clock=clock,
                    sleep=make_sleep(clock, sleeps),
                    today=TODAY,
                )

                self.assertTrue(ok)
                self.assertEqual(attempts, 2)
                self.assertEqual(failures, [])
                self.assertEqual(sleeps, [15.0])
                self.assertEqual(len(calls), 3)

    def test_body_transport_faults_are_retried_then_recover(self):
        fresh = fresh_guide()
        manifest = release_bytes(EXPECTED_RUN_ID, REVISION, fresh)
        faults = (
            ("IncompleteRead", http.client.IncompleteRead(b"partial body")),
            ("BadStatusLine", http.client.BadStatusLine("garbage")),
        )
        for label, fault in faults:
            with self.subTest(fault=label):
                calls = []
                clock = FakeClock()
                sleeps = []

                def fetch(url, timeout, fault=fault):
                    calls.append((url, timeout))
                    if len(calls) == 1:
                        raise fault
                    return (200, manifest if "release.json" in url else fresh)

                ok, attempts, failures = health.run_verified_health_check(
                    "https://pages.test/EPG",
                    EXPECTED_RUN_ID,
                    REVISION,
                    fetch=fetch,
                    clock=clock,
                    sleep=make_sleep(clock, sleeps),
                    today=TODAY,
                )

                self.assertTrue(ok)
                self.assertEqual(attempts, 2)
                self.assertEqual(failures, [])
                self.assertEqual(len(calls), 3)
                self.assertEqual(sleeps, [15.0])

    def test_persistent_body_faults_fail_closed_with_retry_evidence(self):
        calls = []
        clock = FakeClock()
        sleeps = []

        def fetch(url, timeout):
            calls.append(url)
            raise http.client.IncompleteRead(b"partial body")

        ok, attempts, failures = health.run_verified_health_check(
            "https://pages.test/EPG",
            EXPECTED_RUN_ID,
            REVISION,
            fetch=fetch,
            clock=clock,
            sleep=make_sleep(clock, sleeps),
            total_budget=30.0,
            attempt_interval=15.0,
            today=TODAY,
        )

        self.assertFalse(ok)
        self.assertEqual(attempts, 2)
        self.assertTrue(any("IncompleteRead" in failure for failure in failures), failures)
        self.assertEqual(sleeps, [15.0, 15.0])

    def test_truncated_public_body_is_retried_end_to_end(self):
        fresh = fresh_guide()
        manifest = release_bytes(EXPECTED_RUN_ID, REVISION, fresh)
        calls = []
        clock = FakeClock()
        sleeps = []

        class TruncatedBody:
            status = 200

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def read(self):
                raise http.client.IncompleteRead(b"partial body")

        class Body:
            def __init__(self, payload):
                self.status = 200
                self.payload = payload

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def read(self):
                return self.payload

        def fake_urlopen(request, timeout):
            payload = manifest if "release.json" in request.full_url else fresh
            calls.append(request.full_url)
            if len(calls) == 1:
                return TruncatedBody()
            return Body(payload)

        with patch.object(health.urllib.request, "urlopen", fake_urlopen):
            ok, attempts, failures = health.run_verified_health_check(
                "https://pages.test/EPG",
                EXPECTED_RUN_ID,
                REVISION,
                clock=clock,
                sleep=make_sleep(clock, sleeps),
                today=TODAY,
            )

        self.assertTrue(ok)
        self.assertEqual(attempts, 2)
        self.assertEqual(failures, [])
        self.assertEqual(len(calls), 3)

    def test_no_success_after_fetch_phase_exceeds_deadline(self):
        fresh = fresh_guide()
        manifest = release_bytes(EXPECTED_RUN_ID, REVISION, fresh)
        clock = FakeClock()
        sleeps = []

        def fetch(url, timeout):
            clock.now += 6.0
            return (200, manifest if "release.json" in url else fresh)

        ok, attempts, failures = health.run_verified_health_check(
            "https://pages.test/EPG",
            EXPECTED_RUN_ID,
            REVISION,
            fetch=fetch,
            clock=clock,
            sleep=make_sleep(clock, sleeps),
            total_budget=10.0,
            attempt_interval=15.0,
            today=TODAY,
        )

        self.assertFalse(ok)
        self.assertEqual(attempts, 1)
        self.assertEqual(sleeps, [])
        self.assertTrue(
            any("deadline exhausted after fetching" in failure for failure in failures), failures
        )

    def test_no_success_after_validation_exceeds_deadline(self):
        fresh = fresh_guide()
        manifest = release_bytes(EXPECTED_RUN_ID, REVISION, fresh)
        clock = FakeClock()
        sleeps = []
        real_check = health.check_deployed_release

        def slow_check(*args, **kwargs):
            clock.now += 20.0
            return real_check(*args, **kwargs)

        def fetch(url, timeout):
            clock.now += 2.0
            return (200, manifest if "release.json" in url else fresh)

        with patch.object(health, "check_deployed_release", slow_check):
            ok, attempts, failures = health.run_verified_health_check(
                "https://pages.test/EPG",
                EXPECTED_RUN_ID,
                REVISION,
                fetch=fetch,
                clock=clock,
                sleep=make_sleep(clock, sleeps),
                total_budget=10.0,
                attempt_interval=15.0,
                today=TODAY,
            )

        self.assertFalse(ok)
        self.assertEqual(attempts, 1)
        self.assertEqual(sleeps, [])
        self.assertTrue(
            any("deadline exhausted after validation" in failure for failure in failures), failures
        )

    def test_deadline_exhausted_before_second_fetch_is_reported(self):
        clock = FakeClock()
        sleeps = []
        fetched = []

        def fetch(url, timeout):
            fetched.append(url)
            clock.now += 20.0
            return (200, b"{}")

        ok, attempts, failures = health.run_verified_health_check(
            "https://pages.test/EPG",
            EXPECTED_RUN_ID,
            REVISION,
            fetch=fetch,
            clock=clock,
            sleep=make_sleep(clock, sleeps),
            total_budget=15.0,
            attempt_interval=15.0,
            today=TODAY,
        )

        self.assertFalse(ok)
        self.assertEqual(attempts, 1)
        self.assertEqual(len(fetched), 1)
        self.assertTrue(
            any("deadline exhausted before fetching guide.xml.gz" in failure for failure in failures),
            failures,
        )


class DeployedReleaseGuardTest(unittest.TestCase):
    def test_full_match_passes_with_no_failures(self):
        fresh = fresh_guide()
        manifest = release_bytes(EXPECTED_RUN_ID, REVISION, fresh)

        failures = health.check_deployed_release(
            manifest, fresh, EXPECTED_RUN_ID, REVISION, TODAY
        )

        self.assertEqual(failures, [])

    def test_published_minimums_are_pinned(self):
        self.assertEqual(health.MIN_GUIDE_BYTES, 1000000)
        self.assertEqual(health.MIN_CHANNELS, 1500)
        self.assertEqual(health.MIN_PROGRAMMES, 100000)

    def test_schema_mismatch_fails(self):
        fresh = fresh_guide()
        manifest = release_bytes(EXPECTED_RUN_ID, REVISION, fresh, schema=2)

        failures = health.check_deployed_release(
            manifest, fresh, EXPECTED_RUN_ID, REVISION, TODAY
        )

        self.assertTrue(any("schema" in failure for failure in failures), failures)

    def test_revision_mismatch_fails(self):
        fresh = fresh_guide()
        manifest = release_bytes(EXPECTED_RUN_ID, "9" * 40, fresh)

        failures = health.check_deployed_release(
            manifest, fresh, EXPECTED_RUN_ID, REVISION, TODAY
        )

        self.assertTrue(any("revision" in failure for failure in failures), failures)

    def test_guide_hash_mismatch_fails(self):
        fresh = fresh_guide()
        manifest = release_bytes(EXPECTED_RUN_ID, REVISION, b"a different guide")

        failures = health.check_deployed_release(
            manifest, fresh, EXPECTED_RUN_ID, REVISION, TODAY
        )

        self.assertTrue(any("sha256" in failure for failure in failures), failures)

    def test_implausibly_small_guide_fails(self):
        tiny = b"tiny"
        manifest = release_bytes(EXPECTED_RUN_ID, REVISION, tiny)

        failures = health.check_deployed_release(
            manifest, tiny, EXPECTED_RUN_ID, REVISION, TODAY
        )

        self.assertTrue(
            any("implausibly small" in failure for failure in failures), failures
        )

    def test_guide_below_minimums_fails(self):
        guide = make_guide(programmes=99000)
        manifest = release_bytes(EXPECTED_RUN_ID, REVISION, guide)

        failures = health.check_deployed_release(
            manifest, guide, EXPECTED_RUN_ID, REVISION, TODAY
        )

        self.assertTrue(any("below minimums" in failure for failure in failures), failures)
        self.assertFalse(any("implausibly small" in failure for failure in failures), failures)

    def test_stale_programmes_fail_current_data_check(self):
        guide = make_guide(stop="20261001")
        manifest = release_bytes(EXPECTED_RUN_ID, REVISION, guide)

        failures = health.check_deployed_release(
            manifest, guide, EXPECTED_RUN_ID, REVISION, TODAY
        )

        self.assertTrue(
            any("no current programmes" in failure for failure in failures), failures
        )

    def test_invalid_gzip_fails(self):
        blob = b"x" * 1500000
        manifest = release_bytes(EXPECTED_RUN_ID, REVISION, blob)

        failures = health.check_deployed_release(
            manifest, blob, EXPECTED_RUN_ID, REVISION, TODAY
        )

        self.assertTrue(any("gzip" in failure for failure in failures), failures)

    def test_invalid_json_fails(self):
        failures = health.check_deployed_release(
            b"not json", b"", EXPECTED_RUN_ID, REVISION, TODAY
        )

        self.assertEqual(failures, ["release.json is not valid JSON"])


class HealthMainTest(unittest.TestCase):
    def test_main_exits_zero_after_successful_verification(self):
        fresh = fresh_guide()
        manifest = release_bytes(EXPECTED_RUN_ID, REVISION, fresh)
        clock = FakeClock()

        def fetch(url, timeout):
            return (200, manifest if "release.json" in url else fresh)

        with contextlib.redirect_stdout(io.StringIO()) as output:
            exit_code = health.main(
                ["--base-url", "https://pages.test/EPG",
                 "--expected-run-id", EXPECTED_RUN_ID,
                 "--expected-revision", REVISION],
                fetch=fetch,
                clock=clock,
                sleep=make_sleep(clock, []),
                today=TODAY,
            )

        self.assertEqual(exit_code, 0)
        self.assertIn("verified", output.getvalue())

    def test_main_exits_nonzero_after_budget_exhaustion(self):
        stale = b"previous release"
        mismatch = (release_bytes("111", "f" * 40, stale), stale)
        clock = FakeClock()

        def fetch(url, timeout):
            return (200, mismatch[0] if "release.json" in url else mismatch[1])

        with contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(io.StringIO()):
            exit_code = health.main(
                ["--base-url", "https://pages.test/EPG",
                 "--expected-run-id", EXPECTED_RUN_ID,
                 "--expected-revision", REVISION,
                 "--total-budget-seconds", "30",
                 "--attempt-interval-seconds", "15"],
                fetch=fetch,
                clock=clock,
                sleep=make_sleep(clock, []),
            )

        self.assertEqual(exit_code, 1)

    def test_main_prints_recovered_attempt_mismatch_evidence(self):
        fresh = fresh_guide()
        stale = b"previous release"
        first_attempt = (release_bytes("111", "f" * 40, stale), stale)
        final_attempt = (release_bytes(EXPECTED_RUN_ID, REVISION, fresh), fresh)
        calls = []
        clock = FakeClock()

        def fetch(url, timeout):
            attempt = (len(calls) // 2) + 1
            calls.append((url, timeout))
            release, guide = first_attempt if attempt == 1 else final_attempt
            return (200, release if "release.json" in url else guide)

        with contextlib.redirect_stdout(io.StringIO()) as output, \
                contextlib.redirect_stderr(io.StringIO()) as errors:
            exit_code = health.main(
                ["--base-url", "https://pages.test/EPG",
                 "--expected-run-id", EXPECTED_RUN_ID,
                 "--expected-revision", REVISION],
                fetch=fetch,
                clock=clock,
                sleep=make_sleep(clock, []),
                today=TODAY,
            )

        self.assertEqual(exit_code, 0)
        self.assertIn("OK:", output.getvalue())
        self.assertIn("attempt 1 mismatch:", errors.getvalue())
        self.assertIn("run_id '111'", errors.getvalue())
        # Only public metadata values are printed, never response bodies.
        self.assertNotIn("previous release", errors.getvalue())

    def test_main_success_verification_is_not_wall_clock_coupled(self):
        fresh = fresh_guide()
        manifest = release_bytes(EXPECTED_RUN_ID, REVISION, fresh)
        clock = FakeClock()
        bootstrap = ["--base-url", "https://pages.test/EPG",
                     "--expected-run-id", EXPECTED_RUN_ID,
                     "--expected-revision", REVISION]

        def fetch(url, timeout):
            return (200, manifest if "release.json" in url else fresh)

        class FrozenDate(datetime.date):
            @classmethod
            def today(cls):
                return datetime.date(2026, 10, 17)

        class FrozenDatetime:
            date = FrozenDate

        with patch.object(health, "datetime", FrozenDatetime):
            # Control: with the wall clock past the fixture guide's max stop
            # (20261016) the check legitimately fails closed. This is the
            # scheduled failure the regression pins: the success path must not
            # consult the real clock.
            control_clock = FakeClock()
            with contextlib.redirect_stdout(io.StringIO()), \
                    contextlib.redirect_stderr(io.StringIO()):
                control_exit = health.main(
                    bootstrap + ["--total-budget-seconds", "30",
                                 "--attempt-interval-seconds", "15"],
                    fetch=fetch,
                    clock=control_clock,
                    sleep=make_sleep(control_clock, []),
                )
            self.assertEqual(control_exit, 1)

            # Injected date: the CLI success path is stable regardless of the
            # real clock.
            with contextlib.redirect_stdout(io.StringIO()) as output:
                exit_code = health.main(
                    bootstrap,
                    fetch=fetch,
                    clock=clock,
                    sleep=make_sleep(clock, []),
                    today=TODAY,
                )
            self.assertEqual(exit_code, 0)
            self.assertIn("verified", output.getvalue())


if __name__ == "__main__":
    unittest.main()
