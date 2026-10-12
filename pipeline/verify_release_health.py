#!/usr/bin/env python3
"""Verify the deployed Pages release matches this workflow build, with bounded retries.

Fails closed: unless one attempt observes the full release match within the
total deadline, the command exits non-zero. Every attempt re-fetches both
release.json and guide.xml.gz with a fresh nonce (best-effort cache busting;
the retry loop, not the nonce, is what absorbs CDN propagation lag) and
re-runs every published check.

Deadline semantics are bounded, not a hard per-transfer wall: the budget is
checked before each fetch, after the fetch phase, and again before reporting
success, so once the clock has passed the deadline no attempt can succeed.
Socket timeouts apply per operation rather than per transfer, so one
trickling read can still run past the remaining budget before a check
notices; the workflow health job's own timeout-minutes is the external wall
for that case.
"""

from __future__ import annotations

import argparse
import datetime
import gzip
import hashlib
import http.client
import json
import re
import sys
import time
import urllib.error
import urllib.request
import uuid


DEFAULT_TOTAL_BUDGET_SECONDS = 180.0
DEFAULT_ATTEMPT_INTERVAL_SECONDS = 15.0
REQUEST_TIMEOUT_SECONDS = 30.0
MIN_GUIDE_BYTES = 1000000
MIN_CHANNELS = 1500
MIN_PROGRAMMES = 100000


def fetch_public(url, timeout):
    """GET a public Pages object; HTTP status is data, transport failures raise."""
    request = urllib.request.Request(
        url,
        headers={"User-Agent": "epg-release-health", "Cache-Control": "no-cache"},
        method="GET",
    )
    try:
        response = urllib.request.urlopen(request, timeout=timeout)
    except urllib.error.HTTPError as exc:
        return exc.code, b""
    with response:
        return getattr(response, "status", 200), response.read()


def check_deployed_release(release_bytes, guide_bytes, expected_run_id, expected_revision, today):
    """Return the failure list for one attempt; an empty list means verified."""
    try:
        release = json.loads(release_bytes.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return ["release.json is not valid JSON"]
    if not isinstance(release, dict):
        return ["release.json is not a JSON object"]
    failures = []
    if release.get("schema") != 1:
        failures.append("release.json schema %r expected 1" % (release.get("schema"),))
    if release.get("run_id") != expected_run_id:
        failures.append(
            "release.json run_id %r expected %r" % (release.get("run_id"), expected_run_id)
        )
    if release.get("revision") != expected_revision:
        failures.append(
            "release.json revision %r expected %r"
            % (release.get("revision"), expected_revision)
        )
    digest = hashlib.sha256(guide_bytes).hexdigest()
    if release.get("guide_sha256") != digest:
        failures.append(
            "guide sha256 %s expected release guide_sha256 %s"
            % (digest[:12], str(release.get("guide_sha256"))[:12])
        )
    if len(guide_bytes) < MIN_GUIDE_BYTES:
        failures.append(
            "guide implausibly small (%d bytes < %d)" % (len(guide_bytes), MIN_GUIDE_BYTES)
        )
    try:
        text = gzip.decompress(guide_bytes).decode("utf-8", errors="ignore")
    except (OSError, EOFError, ValueError) as exc:
        failures.append("guide is not valid gzip (%s)" % type(exc).__name__)
        return failures
    channels = text.count("<channel ")
    programmes = text.count("<programme")
    if channels < MIN_CHANNELS or programmes < MIN_PROGRAMMES:
        failures.append(
            "deployed guide below minimums (%d channels, %d programmes)"
            % (channels, programmes)
        )
    stops = re.findall(r'stop="(\d{8})', text)
    if not stops or max(stops) < today:
        failures.append(
            "no current programmes (max stop %s < %s)"
            % (max(stops) if stops else "none", today)
        )
    return failures


def run_verified_health_check(base_url, expected_run_id, expected_revision, fetch=None, clock=None,
                              sleep=None, total_budget=DEFAULT_TOTAL_BUDGET_SECONDS,
                              attempt_interval=DEFAULT_ATTEMPT_INTERVAL_SECONDS,
                              request_timeout=REQUEST_TIMEOUT_SECONDS, today=None,
                              failure_history=None):
    """Retry until the deployed release matches, or the total budget expires.

    Returns (succeeded, attempts, failures); failures describes the last attempt.
    No success is reported once the deadline has passed: the attempt path
    checks the deadline after the fetch phase, and this loop checks it again
    after validation. failure_history, when provided, receives (attempt,
    failures) for every non-matching attempt so callers can report recovered
    mismatches without losing evidence.
    """
    fetch = fetch or fetch_public
    clock = clock or time.monotonic
    sleep = sleep or time.sleep
    if today is None:
        today = datetime.date.today().strftime("%Y%m%d")
    base = base_url.rstrip("/")
    deadline = clock() + total_budget
    attempts = 0
    failures = ["no attempt completed"]
    while True:
        if deadline - clock() <= 0:
            return False, attempts, failures
        attempts += 1
        failures = _attempt_once(
            fetch, clock, base, expected_run_id, expected_revision, today,
            deadline, request_timeout,
        )
        if not failures and deadline - clock() <= 0:
            failures = ["deadline exhausted after validation"]
        if not failures:
            return True, attempts, []
        if failure_history is not None:
            failure_history.append((attempts, list(failures)))
        remaining = deadline - clock()
        if remaining <= 0:
            return False, attempts, failures
        sleep(min(attempt_interval, remaining))


def _attempt_once(fetch, clock, base, expected_run_id, expected_revision, today, deadline,
                  request_timeout):
    nonce = uuid.uuid4().hex
    bodies = {}
    for name, url in (
        ("release.json", "%s/release.json?verify=%s" % (base, nonce)),
        ("guide.xml.gz", "%s/guide.xml.gz?verify=%s" % (base, nonce)),
    ):
        remaining = deadline - clock()
        if remaining <= 0:
            return ["deadline exhausted before fetching %s" % name]
        try:
            status, body = fetch(url, min(request_timeout, remaining))
        except (
            urllib.error.URLError,
            TimeoutError,
            OSError,
            http.client.HTTPException,
        ) as exc:
            return ["%s fetch failed (%s)" % (name, type(exc).__name__)]
        if status != 200:
            return ["%s HTTP %s" % (name, status)]
        bodies[name] = body
    if deadline - clock() <= 0:
        return ["deadline exhausted after fetching"]
    return check_deployed_release(
        bodies["release.json"], bodies["guide.xml.gz"],
        expected_run_id, expected_revision, today,
    )


def main(argv=None, fetch=None, clock=None, sleep=None, today=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-url", required=True, help="Pages base URL, e.g. https://owner.github.io/EPG")
    parser.add_argument("--expected-run-id", required=True)
    parser.add_argument("--expected-revision", required=True)
    parser.add_argument("--total-budget-seconds", type=float, default=DEFAULT_TOTAL_BUDGET_SECONDS)
    parser.add_argument("--attempt-interval-seconds", type=float, default=DEFAULT_ATTEMPT_INTERVAL_SECONDS)
    args = parser.parse_args(argv)
    failure_history = []
    succeeded, attempts, failures = run_verified_health_check(
        args.base_url,
        args.expected_run_id,
        args.expected_revision,
        fetch=fetch,
        clock=clock,
        sleep=sleep,
        total_budget=args.total_budget_seconds,
        attempt_interval=args.attempt_interval_seconds,
        today=today,
        failure_history=failure_history,
    )
    # Report mismatches from attempts that were retried before the final
    # outcome, so recovered evidence is not lost. Only public metadata values
    # (field names, expected/observed values, counts) appear here.
    earlier_attempts = failure_history if succeeded else failure_history[:-1]
    for attempt, attempt_failures in earlier_attempts:
        for failure in attempt_failures:
            print("attempt %d mismatch: %s" % (attempt, failure), file=sys.stderr)
    if succeeded:
        print(
            "OK: deployed release verified after %d attempt(s): run_id=%s revision=%s"
            % (attempts, args.expected_run_id, args.expected_revision)
        )
        return 0
    print(
        "FAIL: deployed release not verified after %d attempt(s) (budget %.0fs)"
        % (attempts, args.total_budget_seconds)
    )
    for failure in failures:
        print("  - %s" % failure, file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
