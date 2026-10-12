import contextlib
import io
import json
import pathlib
import sys
import unittest
import urllib.error


ROOT = pathlib.Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures" / "epg_watchdog"
sys.path.insert(0, str(ROOT / "pipeline"))

import release_order_guard as guard


class FixtureClient:
    def __init__(self, fixture):
        self.fixture = fixture
        self.calls = []

    def get(self, path, params=None):
        self.calls.append((path, params))
        if path.endswith("/actions/workflows/build-epg.yml"):
            return self.fixture["workflow"]
        if path.endswith("/actions/runs/100"):
            return self.fixture["current_run"]
        if "/actions/workflows/42/runs" in path:
            return self.fixture["runs"]
        raise AssertionError(f"unexpected GET {path}")


class ErrorClient:
    def __init__(self, message):
        self.message = message

    def get(self, path, params=None):
        raise guard.ReleaseOrderError(self.message)


def fixture(name):
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


class ReleaseOrderGuardTest(unittest.TestCase):
    def test_current_run_is_newest_allows_deploy(self):
        client = FixtureClient(fixture("release-current-newest.json"))

        allowed = guard.check_release_order("owner/repo", "build-epg.yml", 100, client)

        self.assertTrue(allowed)
        self.assertEqual(
            [path for path, _ in client.calls],
            [
                "/repos/owner/repo/actions/workflows/build-epg.yml",
                "/repos/owner/repo/actions/runs/100",
                "/repos/owner/repo/actions/workflows/42/runs",
            ],
        )
        self.assertEqual(
            client.calls[2][1],
            {
                "branch": "main",
                "created": ">=2026-09-04T20:00:00Z",
                "per_page": 100,
            },
        )

    def test_current_run_is_newest_returns_zero_exit(self):
        client = FixtureClient(fixture("release-current-newest.json"))

        exit_code = guard.main(
            ["--repository", "owner/repo", "--workflow", "build-epg.yml", "--run-id", "100"],
            client_factory=lambda token: client,
        )

        self.assertEqual(exit_code, 0)

    def test_newer_queued_run_blocks_deploy(self):
        client = FixtureClient(fixture("release-newer-queued.json"))

        allowed = guard.check_release_order("owner/repo", "build-epg.yml", 100, client)

        self.assertFalse(allowed)

    def test_newer_run_returns_nonzero_exit(self):
        client = FixtureClient(fixture("release-newer-queued.json"))

        with contextlib.redirect_stderr(io.StringIO()):
            exit_code = guard.main(
                ["--repository", "owner/repo", "--workflow", "build-epg.yml", "--run-id", "100"],
                client_factory=lambda token: client,
            )

        self.assertEqual(exit_code, 1)

    def test_newer_active_run_blocks_deploy(self):
        client = FixtureClient(fixture("release-newer-active.json"))

        allowed = guard.check_release_order("owner/repo", "build-epg.yml", 100, client)

        self.assertFalse(allowed)

    def test_newer_succeeded_run_blocks_deploy(self):
        client = FixtureClient(fixture("release-newer-succeeded.json"))

        allowed = guard.check_release_order("owner/repo", "build-epg.yml", 100, client)

        self.assertFalse(allowed)

    def test_newer_failed_run_blocks_deploy(self):
        client = FixtureClient(fixture("release-newer-failed.json"))

        allowed = guard.check_release_order("owner/repo", "build-epg.yml", 100, client)

        self.assertFalse(allowed)

    def test_same_timestamp_with_larger_run_id_blocks_deploy(self):
        client = FixtureClient(fixture("release-same-time-larger-id.json"))

        allowed = guard.check_release_order("owner/repo", "build-epg.yml", 100, client)

        self.assertFalse(allowed)

    def test_wrong_branch_blocks_deploy(self):
        client = FixtureClient(fixture("release-wrong-branch.json"))

        with self.assertRaises(guard.ReleaseOrderError):
            guard.check_release_order("owner/repo", "build-epg.yml", 100, client)

    def test_wrong_workflow_blocks_deploy(self):
        client = FixtureClient(fixture("release-wrong-workflow.json"))

        with self.assertRaises(guard.ReleaseOrderError):
            guard.check_release_order("owner/repo", "build-epg.yml", 100, client)

    def test_filtered_result_truncated_fails_closed(self):
        data = fixture("release-current-newest.json")
        data["runs"] = {
            "total_count": 100,
            "workflow_runs": [data["current_run"]],
        }
        client = FixtureClient(data)

        with self.assertRaisesRegex(guard.ReleaseOrderError, "incomplete"):
            guard.check_release_order("owner/repo", "build-epg.yml", 100, client)

        self.assertEqual(len(client.calls), 3)

    def test_filtered_result_with_next_link_fails_closed(self):
        data = fixture("release-current-newest.json")
        current = data["current_run"]
        older = {
            "id": 1,
            "workflow_id": 42,
            "head_branch": "main",
            "created_at": "2026-09-04T20:00:00Z",
        }
        data["runs"] = {
            "total_count": 100,
            "workflow_runs": [current] + [dict(older, id=run_id) for run_id in range(1, 100)],
        }

        class LinkedClient(FixtureClient):
            def get(self, path, params=None):
                response = super().get(path, params)
                if "/actions/workflows/42/runs" in path:
                    self.last_link_header = '<https://api.github.com/?page=2>; rel="next"'
                return response

        client = LinkedClient(data)

        with self.assertRaisesRegex(guard.ReleaseOrderError, "pagination"):
            guard.check_release_order("owner/repo", "build-epg.yml", 100, client)

        self.assertEqual(len(client.calls), 3)

    def test_more_than_one_hundred_filtered_runs_fails_closed_without_extra_read(self):
        data = fixture("release-current-newest.json")
        data["runs"] = {
            "total_count": 101,
            "workflow_runs": [data["current_run"]],
        }
        client = FixtureClient(data)

        with self.assertRaisesRegex(guard.ReleaseOrderError, "100"):
            guard.check_release_order("owner/repo", "build-epg.yml", 100, client)

        self.assertEqual(len(client.calls), 3)

    def test_filtered_result_count_mismatch_fails_closed(self):
        data = fixture("release-current-newest.json")
        data["runs"] = {
            "total_count": 2,
            "workflow_runs": [data["current_run"]],
        }
        client = FixtureClient(data)

        with self.assertRaisesRegex(guard.ReleaseOrderError, "incomplete"):
            guard.check_release_order("owner/repo", "build-epg.yml", 100, client)

    def test_filtered_result_with_malformed_next_link_fails_closed(self):
        data = fixture("release-current-newest.json")
        current = data["current_run"]
        older = {
            "id": 1,
            "workflow_id": 42,
            "head_branch": "main",
            "created_at": "2026-09-04T20:00:00Z",
        }
        data["runs"] = {
            "total_count": 100,
            "workflow_runs": [current] + [dict(older, id=run_id) for run_id in range(1, 100)],
        }

        class LinkedClient(FixtureClient):
            def get(self, path, params=None):
                response = super().get(path, params)
                if "/actions/workflows/42/runs" in path:
                    self.last_link_header = '<https://api.github.com/?page=0>; rel="next"'
                return response

        client = LinkedClient(data)

        with self.assertRaisesRegex(guard.ReleaseOrderError, "invalid run-list next page"):
            guard.check_release_order("owner/repo", "build-epg.yml", 100, client)

    def test_filtered_result_over_bound_fails_closed(self):
        client = FixtureClient(fixture("release-more-than-one-page.json"))

        with self.assertRaisesRegex(guard.ReleaseOrderError, "100"):
            guard.check_release_order("owner/repo", "build-epg.yml", 100, client)

    def test_truncated_api_result_blocks_deploy(self):
        client = FixtureClient(fixture("release-truncated-page.json"))

        with self.assertRaises(guard.ReleaseOrderError):
            guard.check_release_order("owner/repo", "build-epg.yml", 100, client)

    def test_malformed_response_blocks_with_schema_error(self):
        client = FixtureClient(fixture("release-malformed.json"))

        with self.assertRaisesRegex(guard.ReleaseOrderError, "invalid run-list total_count"):
            guard.check_release_order("owner/repo", "build-epg.yml", 100, client)


    def test_github_error_blocks_with_nonzero_exit(self):
        error = fixture("release-github-error.json")
        client = ErrorClient(error["error"])

        with contextlib.redirect_stderr(io.StringIO()):
            exit_code = guard.main(
                ["--repository", "owner/repo", "--workflow", "build-epg.yml", "--run-id", "100"],
                client_factory=lambda token: client,
            )

        self.assertEqual(exit_code, 1)

    def test_github_adapter_uses_only_get_requests(self):
        requests = []

        class Response:
            status = 200

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def read(self):
                return b"{}"

        def opener(request, timeout):
            requests.append((request, timeout))
            return Response()

        client = guard.GitHubClient(token="test-token", opener=opener)
        client.get(
            "/repos/owner/repo/actions/workflows/build-epg.yml",
            params={
                "branch": "main",
                "created": ">=2026-09-04T20:00:00Z",
                "per_page": 100,
            },
        )

        self.assertEqual(len(requests), 1)
        self.assertEqual(requests[0][0].get_method(), "GET")
        self.assertEqual(requests[0][1], guard.HTTP_TIMEOUT_SECONDS)
        self.assertEqual(
            requests[0][0].full_url,
            "https://api.github.com/repos/owner/repo/actions/workflows/build-epg.yml"
            "?branch=main&created=%3E%3D2026-09-04T20%3A00%3A00Z&per_page=100",
        )
        self.assertNotIn("dispatches", requests[0][0].full_url)

    def test_transient_transport_error_is_retried_then_recovers(self):
        calls = []
        sleeps = []

        class Response:
            status = 200
            headers = {}

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def read(self):
                return b'{"ok": true}'

        def opener(request, timeout):
            calls.append((request, timeout))
            if len(calls) == 1:
                raise urllib.error.URLError("connection reset")
            return Response()

        client = guard.GitHubClient(token="test-token", opener=opener, sleep=sleeps.append)

        result = client.get("/repos/owner/repo/actions/runs/100")

        self.assertEqual(result, {"ok": True})
        self.assertEqual(len(calls), 2)
        self.assertEqual(sleeps, [2.0])

    def test_permanent_http_error_is_not_retried_and_names_endpoint(self):
        for status in (403, 404):
            with self.subTest(status=status):
                calls = []
                sleeps = []

                def opener(request, timeout):
                    calls.append(request)
                    raise urllib.error.HTTPError(request.full_url, status, "denied", {}, None)

                client = guard.GitHubClient(token="test-token", opener=opener, sleep=sleeps.append)

                with self.assertRaisesRegex(guard.ReleaseOrderError, str(status)) as caught:
                    client.get("/repos/owner/repo/actions/runs/100")

                self.assertIn("/repos/owner/repo/actions/runs/100", str(caught.exception))
                self.assertEqual(len(calls), 1)
                self.assertEqual(sleeps, [])

    def test_server_error_is_retried_then_recovers(self):
        calls = []
        sleeps = []

        class Response:
            status = 200
            headers = {}

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def read(self):
                return b'{"ok": true}'

        def opener(request, timeout):
            calls.append(request)
            if len(calls) == 1:
                raise urllib.error.HTTPError(request.full_url, 503, "unavailable", {}, None)
            return Response()

        client = guard.GitHubClient(token="test-token", opener=opener, sleep=sleeps.append)

        result = client.get("/repos/owner/repo/actions/runs/100")

        self.assertEqual(result, {"ok": True})
        self.assertEqual(len(calls), 2)
        self.assertEqual(sleeps, [2.0])

    def test_exhausted_server_errors_fail_closed_with_bounded_attempts(self):
        calls = []
        sleeps = []

        def opener(request, timeout):
            calls.append(request)
            raise urllib.error.HTTPError(request.full_url, 503, "unavailable", {}, None)

        client = guard.GitHubClient(token="test-token", opener=opener, sleep=sleeps.append)

        with self.assertRaisesRegex(guard.ReleaseOrderError, "503") as caught:
            client.get("/repos/owner/repo/actions/workflows/42/runs")

        message = str(caught.exception)
        self.assertIn("after 3 attempts", message)
        self.assertIn("/repos/owner/repo/actions/workflows/42/runs", message)
        self.assertEqual(len(calls), 3)
        self.assertEqual(sleeps, [2.0, 4.0])

    def test_rate_limit_honors_bounded_retry_after(self):
        for retry_after, expected_sleeps in (("5", [5.0]), ("60", [60.0]), ("soon", [2.0])):
            with self.subTest(retry_after=retry_after):
                calls = []
                sleeps = []

                class Response:
                    status = 200
                    headers = {}

                    def __enter__(self):
                        return self

                    def __exit__(self, *args):
                        return False

                    def read(self):
                        return b'{"ok": true}'

                def opener(request, timeout):
                    calls.append(request)
                    if len(calls) == 1:
                        raise urllib.error.HTTPError(
                            request.full_url, 429, "rate limited",
                            {"Retry-After": retry_after}, None,
                        )
                    return Response()

                client = guard.GitHubClient(token="test-token", opener=opener, sleep=sleeps.append)

                result = client.get("/repos/owner/repo/actions/runs/100")

                self.assertEqual(result, {"ok": True})
                self.assertEqual(sleeps, expected_sleeps)
                self.assertEqual(len(calls), 2)

    def test_rate_limit_retry_after_beyond_bound_fails_closed(self):
        calls = []
        sleeps = []

        def opener(request, timeout):
            calls.append(request)
            raise urllib.error.HTTPError(
                request.full_url, 429, "rate limited", {"Retry-After": "61"}, None,
            )

        client = guard.GitHubClient(token="test-token", opener=opener, sleep=sleeps.append)

        with self.assertRaisesRegex(guard.ReleaseOrderError, "61") as caught:
            client.get("/repos/owner/repo/actions/runs/100")

        self.assertIn("60", str(caught.exception))
        self.assertEqual(len(calls), 1)
        self.assertEqual(sleeps, [])

    def test_invalid_json_is_not_retried_and_names_endpoint(self):
        calls = []
        sleeps = []

        class Response:
            status = 200
            headers = {}

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def read(self):
                return b"not json"

        def opener(request, timeout):
            calls.append(request)
            return Response()

        client = guard.GitHubClient(token="test-token", opener=opener, sleep=sleeps.append)

        with self.assertRaisesRegex(guard.ReleaseOrderError, "invalid JSON") as caught:
            client.get("/repos/owner/repo/actions/runs/100")

        self.assertIn("/repos/owner/repo/actions/runs/100", str(caught.exception))
        self.assertEqual(len(calls), 1)
        self.assertEqual(sleeps, [])

    def test_main_recovers_from_one_transient_guard_failure(self):
        data = fixture("release-current-newest.json")
        payloads = {
            "/actions/workflows/build-epg.yml": data["workflow"],
            "/actions/runs/100": data["current_run"],
            "/actions/workflows/42/runs": data["runs"],
        }
        calls = []
        sleeps = []

        class Response:
            def __init__(self, payload):
                self.status = 200
                self.headers = {}
                self.payload = payload

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def read(self):
                return json.dumps(self.payload).encode()

        def opener(request, timeout):
            calls.append(request.full_url)
            if len(calls) == 1:
                raise urllib.error.URLError("temporary failure")
            for fragment, payload in payloads.items():
                if fragment in request.full_url:
                    return Response(payload)
            raise AssertionError("unexpected URL " + request.full_url)

        client = guard.GitHubClient(token="test-token", opener=opener, sleep=sleeps.append)

        exit_code = guard.main(
            ["--repository", "owner/repo", "--workflow", "build-epg.yml", "--run-id", "100"],
            client_factory=lambda token: client,
        )

        self.assertEqual(exit_code, 0)
        self.assertEqual(len(calls), 4)
        self.assertEqual(sleeps, [2.0])


if __name__ == "__main__":
    unittest.main()
