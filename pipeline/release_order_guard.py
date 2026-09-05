#!/usr/bin/env python3
"""Fail closed when a newer EPG workflow run could supersede this release."""

from __future__ import annotations

import argparse
import datetime as _datetime
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request


API_ROOT = "https://api.github.com"
HTTP_TIMEOUT_SECONDS = 30


class ReleaseOrderError(Exception):
    """Raised when GitHub data cannot prove release ordering safely."""


class GitHubClient:
    """Small read-only GitHub API client used by the release guard."""

    def __init__(self, token=None, opener=None):
        self.token = token if token is not None else os.environ.get("GH_TOKEN", "")
        self._opener = opener or urllib.request.urlopen

    def get(self, path, params=None):
        query = urllib.parse.urlencode(params or {})
        url = API_ROOT + path + ("?" + query if query else "")
        headers = {
            "Accept": "application/vnd.github+json",
            "User-Agent": "epg-release-order-guard",
        }
        if self.token:
            headers["Authorization"] = "Bearer " + self.token
        request = urllib.request.Request(url, headers=headers, method="GET")
        try:
            response = self._opener(request, timeout=HTTP_TIMEOUT_SECONDS)
            with response:
                status = getattr(response, "status", 200)
                if status < 200 or status >= 300:
                    raise ReleaseOrderError("GitHub GET returned HTTP %s" % status)
                try:
                    body = response.read()
                    return json.loads(body.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                    raise ReleaseOrderError("GitHub GET returned invalid JSON") from exc
        except ReleaseOrderError:
            raise
        except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, OSError) as exc:
            raise ReleaseOrderError("GitHub GET failed") from exc


def _positive_integer(value, field):
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ReleaseOrderError("invalid %s" % field)
    return value


def _created_at(value):
    if not isinstance(value, str) or not value:
        raise ReleaseOrderError("invalid created_at")
    try:
        parsed = _datetime.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ReleaseOrderError("invalid created_at") from exc
    if parsed.tzinfo is None:
        raise ReleaseOrderError("created_at must include a timezone")
    return parsed.astimezone(_datetime.timezone.utc)


def _run_key(run):
    if not isinstance(run, dict):
        raise ReleaseOrderError("run is not an object")
    run_id = _positive_integer(run.get("id"), "run id")
    if not isinstance(run.get("workflow_id"), int) or isinstance(run.get("workflow_id"), bool):
        raise ReleaseOrderError("invalid run workflow_id")
    if run.get("head_branch") != "main":
        raise ReleaseOrderError("run is not on main")
    return (_created_at(run.get("created_at")), run_id)


def check_release_order(repository, workflow, run_id, client):
    """Return True only when the current run is the newest complete run."""
    if workflow != "build-epg.yml":
        raise ReleaseOrderError("unexpected workflow identity")
    if not isinstance(repository, str) or not repository or "/" not in repository:
        raise ReleaseOrderError("invalid repository")
    try:
        current_id = int(run_id)
    except (TypeError, ValueError) as exc:
        raise ReleaseOrderError("invalid current run id") from exc
    _positive_integer(current_id, "current run id")

    metadata_path = "/repos/%s/actions/workflows/%s" % (repository, workflow)
    metadata = client.get(metadata_path)
    if not isinstance(metadata, dict):
        raise ReleaseOrderError("workflow metadata is not an object")
    workflow_id = _positive_integer(metadata.get("id"), "workflow id")

    current_path = "/repos/%s/actions/runs/%d" % (repository, current_id)
    current = client.get(current_path)
    if not isinstance(current, dict):
        raise ReleaseOrderError("current run is not an object")
    if current.get("id") != current_id:
        raise ReleaseOrderError("current run id mismatch")
    if current.get("workflow_id") != workflow_id:
        raise ReleaseOrderError("current run workflow mismatch")
    current_key = _run_key(current)

    runs_path = "/repos/%s/actions/workflows/%d/runs" % (repository, workflow_id)
    response = client.get(runs_path, params={"branch": "main", "per_page": 100})
    if not isinstance(response, dict):
        raise ReleaseOrderError("run list is not an object")
    total_count = response.get("total_count")
    runs = response.get("workflow_runs")
    if isinstance(total_count, bool) or not isinstance(total_count, int) or total_count < 0:
        raise ReleaseOrderError("invalid run-list total_count")
    if not isinstance(runs, list) or len(runs) != total_count or total_count > 100:
        raise ReleaseOrderError("run list is incomplete or exceeds one page")
    saw_current = False
    for run in runs:
        if not isinstance(run, dict):
            raise ReleaseOrderError("run is not an object")
        if run.get("id") == current_id:
            saw_current = True
        if run.get("workflow_id") != workflow_id:
            raise ReleaseOrderError("run workflow mismatch")
        key = _run_key(run)
        if key > current_key:
            return False
    if not saw_current:
        raise ReleaseOrderError("current run missing from complete run list")
    return True


def main(argv=None, client_factory=GitHubClient):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository", required=True)
    parser.add_argument("--workflow", required=True)
    parser.add_argument("--run-id", required=True, type=int)
    args = parser.parse_args(argv)
    try:
        allowed = check_release_order(
            args.repository,
            args.workflow,
            args.run_id,
            client_factory(os.environ.get("GH_TOKEN", "")),
        )
    except ReleaseOrderError as exc:
        print("release-order guard blocked: %s" % exc, file=sys.stderr)
        return 1
    if not allowed:
        print("release-order guard blocked: newer workflow run exists", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
