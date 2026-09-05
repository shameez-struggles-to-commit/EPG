import contextlib
import io
import json
import pathlib
import sys
import unittest


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
            if "runs_pages" in self.fixture:
                page = (params or {}).get("page", 1)
                return self.fixture["runs_pages"][page - 1]
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
        self.assertEqual(client.calls[2][1], {"branch": "main", "per_page": 100})

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

    def test_complete_newest_page_allows_deploy_when_history_exceeds_one_page(self):
        data = fixture("release-current-newest.json")
        current = data["current_run"]
        older = {
            "id": 1,
            "workflow_id": 42,
            "head_branch": "main",
            "created_at": "2026-09-03T20:00:00Z",
        }
        data["runs_pages"] = [
            {
                "total_count": 101,
                "workflow_runs": [current] + [dict(older, id=run_id) for run_id in range(1, 100)],
            },
            {
                "total_count": 101,
                "workflow_runs": [dict(older, id=200)],
            },
        ]
        data.pop("runs")
        client = FixtureClient(data)

        allowed = guard.check_release_order("owner/repo", "build-epg.yml", 100, client)

        self.assertTrue(allowed)

    def test_equal_timestamp_newer_run_on_second_page_blocks_deploy(self):
        data = fixture("release-current-newest.json")
        current = data["current_run"]
        older = {
            "id": 1,
            "workflow_id": 42,
            "head_branch": "main",
            "created_at": "2026-09-03T20:00:00Z",
        }
        data["runs_pages"] = [
            {
                "total_count": 101,
                "workflow_runs": [current] + [dict(older, id=run_id) for run_id in range(1, 100)],
            },
            {
                "total_count": 101,
                "workflow_runs": [dict(current, id=101)],
            },
        ]
        data.pop("runs")
        client = FixtureClient(data)

        allowed = guard.check_release_order("owner/repo", "build-epg.yml", 100, client)

        self.assertFalse(allowed)
        self.assertEqual(client.calls[3][1], {"branch": "main", "per_page": 100, "page": 2})

    def test_more_than_two_hundred_runs_fails_before_fetching_page_two(self):
        data = fixture("release-current-newest.json")
        current = data["current_run"]
        older = {
            "id": 1,
            "workflow_id": 42,
            "head_branch": "main",
            "created_at": "2026-09-03T20:00:00Z",
        }
        data["runs_pages"] = [
            {
                "total_count": 201,
                "workflow_runs": [current] + [dict(older, id=run_id) for run_id in range(1, 100)],
            },
            {
                "total_count": 201,
                "workflow_runs": [dict(older, id=run_id) for run_id in range(200, 300)],
            },
        ]
        data.pop("runs")
        client = FixtureClient(data)

        with self.assertRaisesRegex(guard.ReleaseOrderError, "200"):
            guard.check_release_order("owner/repo", "build-epg.yml", 100, client)

        self.assertEqual(len(client.calls), 3)

    def test_second_page_reported_count_must_match_first_page(self):
        data = fixture("release-current-newest.json")
        current = data["current_run"]
        older = {
            "id": 1,
            "workflow_id": 42,
            "head_branch": "main",
            "created_at": "2026-09-03T20:00:00Z",
        }
        data["runs_pages"] = [
            {
                "total_count": 101,
                "workflow_runs": [current] + [dict(older, id=run_id) for run_id in range(1, 100)],
            },
            {
                "total_count": 102,
                "workflow_runs": [dict(older, id=200)],
            },
        ]
        data.pop("runs")
        client = FixtureClient(data)

        with self.assertRaisesRegex(guard.ReleaseOrderError, "count"):
            guard.check_release_order("owner/repo", "build-epg.yml", 100, client)

    def test_unexpected_next_page_fails_closed_when_count_says_one_page(self):
        data = fixture("release-current-newest.json")
        current = data["current_run"]
        older = {
            "id": 1,
            "workflow_id": 42,
            "head_branch": "main",
            "created_at": "2026-09-03T20:00:00Z",
        }
        data["runs_pages"] = [
            {
                "total_count": 100,
                "workflow_runs": [current] + [dict(older, id=run_id) for run_id in range(1, 100)],
            },
        ]
        data.pop("runs")

        class LinkedClient(FixtureClient):
            def get(self, path, params=None):
                response = super().get(path, params)
                if "/actions/workflows/42/runs" in path:
                    self.last_link_header = '<https://api.github.com/?page=2>; rel="next"'
                return response

        client = LinkedClient(data)

        with self.assertRaisesRegex(guard.ReleaseOrderError, "next"):
            guard.check_release_order("owner/repo", "build-epg.yml", 100, client)

    def test_second_page_next_link_beyond_bound_fails_closed(self):
        data = fixture("release-current-newest.json")
        current = data["current_run"]
        older = {
            "id": 1,
            "workflow_id": 42,
            "head_branch": "main",
            "created_at": "2026-09-03T20:00:00Z",
        }
        data["runs_pages"] = [
            {
                "total_count": 101,
                "workflow_runs": [current] + [dict(older, id=run_id) for run_id in range(1, 100)],
            },
            {
                "total_count": 101,
                "workflow_runs": [dict(older, id=200)],
            },
        ]
        data.pop("runs")

        class LinkedClient(FixtureClient):
            def get(self, path, params=None):
                response = super().get(path, params)
                if "/actions/workflows/42/runs" in path:
                    page = (params or {}).get("page", 1)
                    target = 2 if page == 1 else 3
                    self.last_link_header = '<https://api.github.com/?page=%d>; rel="next"' % target
                return response

        client = LinkedClient(data)

        with self.assertRaisesRegex(guard.ReleaseOrderError, "next"):
            guard.check_release_order("owner/repo", "build-epg.yml", 100, client)

    def test_more_than_one_api_page_blocks_deploy_when_newest_page_is_truncated(self):
        client = FixtureClient(fixture("release-more-than-one-page.json"))

        with self.assertRaises(guard.ReleaseOrderError):
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
        client.get("/repos/owner/repo/actions/workflows/build-epg.yml")

        self.assertEqual(len(requests), 1)
        self.assertEqual(requests[0][0].get_method(), "GET")
        self.assertEqual(requests[0][1], guard.HTTP_TIMEOUT_SECONDS)
        self.assertNotIn("dispatches", requests[0][0].full_url)


if __name__ == "__main__":
    unittest.main()
