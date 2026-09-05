import argparse
import base64
import contextlib
import datetime as dt
import io
import json
import os
import pathlib
import sys
import tempfile
import time
import unittest
import warnings
import zipfile
import zlib
from unittest.mock import patch

ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from ops.epg_github_watchdog import (
    DiagnosticError,
    DiagnosticReader,
    DuplicateTick,
    DeadlineRunner,
    FINAL_NOTIFICATION_SECONDS,
    LOCAL_HEADROOM_SECONDS,
    NORMAL_WORK_SECONDS,
    PENDING_DIAGNOSTIC_ADMISSION_SECONDS,
    PROCESS_CLEANUP_SECONDS,
    TICK_LIMIT_SECONDS,
    GitHubAdapter,
    GitHubError,
    HermesNotifier,
    NotificationError,
    StateStore,
    StateError,
    SubprocessAdapter,
    TickResult,
    TickTimeoutError,
    WatchdogController,
    WatchdogSchemaError,
    bind_recovery_run,
    classify_day,
    default_day_record,
    main,
)


UTC = dt.timezone.utc


def run(run_id, *, event="schedule", status="completed", conclusion="success",
        created="2026-09-04T04:17:01Z", branch="main", run_attempt=1):
    return {
        "id": run_id,
        "event": event,
        "status": status,
        "conclusion": conclusion,
        "created_at": created,
        "run_attempt": run_attempt,
        "head_branch": branch,
    }


def make_zip(name, data):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr(name, data)
    return buffer.getvalue()


def make_duplicate_zip(name, first, second):
    buffer = io.BytesIO()
    with warnings.catch_warnings():
        warnings.simplefilter("ignore", UserWarning)
        with zipfile.ZipFile(buffer, "w") as archive:
            archive.writestr(name, first)
            archive.writestr(name, second)
    return buffer.getvalue()


def artifact_for(run_id=101, run_attempt=1):
    return {
        "id": 88,
        "name": "epg-diagnostics-%d" % run_attempt,
        "expired": False,
        "expires_at": "2099-01-01T00:00:00Z",
        "size_in_bytes": 256,
        "workflow_run": {"id": run_id, "head_branch": "main"},
    }


def pending_for(run_id=101, run_attempt=1,
                first="2026-09-04T18:00:00Z",
                deadline="2026-09-18T18:00:00Z"):
    return {
        "run_id": run_id,
        "run_attempt": run_attempt,
        "first_observed_at": first,
        "deadline_at": deadline,
    }


class RecordingRunner:
    def __init__(self, payload, http_status=None):
        self.payload = payload
        self.http_status = http_status
        self.calls = []

    def run(self, args, timeout, input_data=None):
        self.calls.append((list(args), timeout, input_data))
        result = type("Result", (), {
            "returncode": 0,
            "stdout": b"" if self.payload is None else json.dumps(self.payload).encode("utf-8"),
            "stderr": b"",
        })()
        if self.http_status is not None:
            result.http_status = self.http_status
        return result


class SequenceRunner(RecordingRunner):
    def __init__(self, payloads):
        super().__init__(None)
        self.payloads = list(payloads)

    def run(self, args, timeout, input_data=None):
        self.calls.append((list(args), timeout, input_data))
        payload = self.payloads.pop(0)
        return type("Result", (), {
            "returncode": 0,
            "stdout": json.dumps(payload).encode("utf-8"),
            "stderr": b"",
        })()


class BinaryRunner(RecordingRunner):
    def __init__(self, payload, http_status=None):
        super().__init__(payload, http_status)

    def run(self, args, timeout, input_data=None):
        self.calls.append((list(args), timeout, input_data))
        return type("Result", (), {
            "returncode": 0,
            "stdout": self.payload,
            "stderr": b"",
        })()


class FailingRunner(RecordingRunner):
    def run(self, args, timeout, input_data=None):
        self.calls.append((list(args), timeout, input_data))
        return type("Result", (), {
            "returncode": 1,
            "stdout": b"",
            "stderr": b"failure",
        })()


class FakeGitHub:
    def __init__(self, runs, artifacts=None, archive=None):
        self.runs = list(runs) if runs and isinstance(runs[0], list) else [runs]
        self.artifacts = artifacts or []
        self.archive = archive or b""
        self.workflow_calls = 0
        self.workflow_error = False
        self.run_calls = []
        self.dispatches = []

    def workflow_state(self):
        self.workflow_calls += 1
        if self.workflow_error:
            raise GitHubError("simulated GitHub error")
        return {"id": 334673316, "state": "active"}

    def list_runs(self, created_after):
        self.run_calls.append(created_after)
        if len(self.runs) > 1:
            return self.runs.pop(0)
        return self.runs[0]

    def list_artifacts(self, run_id):
        return self.artifacts

    def download_artifact(self, artifact_id):
        return self.archive

    def dispatch_recovery(self, watchdog_id):
        self.dispatches.append(watchdog_id)
        return type("Dispatch", (), {"http_status": 204, "workflow_run_id": None, "workflow_run_url": None})()


class RecordingNotifier:
    def __init__(self, fail=False):
        self.calls = []
        self.fail = fail

    def send(self, target, message):
        self.calls.append((target, message))
        if self.fail:
            raise NotificationError("simulated ntfy error")
        return True


class ClassifierTest(unittest.TestCase):
    def test_successful_scheduled_run_is_healthy(self):
        slot = dt.datetime(2026, 9, 4, 4, 17, tzinfo=UTC)
        decision = classify_day(
            now=dt.datetime(2026, 9, 4, 17, 20, tzinfo=UTC),
            slot=slot,
            runs=[run(101)],
        )
        self.assertEqual(decision.kind, "healthy")
        self.assertFalse(decision.should_dispatch)
        self.assertEqual(decision.production_run["id"], 101)
    def test_scheduled_run_queued_after_deadline_is_overdue_without_dispatch(self):
        slot = dt.datetime(2026, 9, 4, 4, 17, tzinfo=UTC)
        decision = classify_day(
            now=dt.datetime(2026, 9, 4, 17, 20, tzinfo=UTC),
            slot=slot,
            runs=[run(102, status="queued", conclusion=None)],
        )
        self.assertEqual(decision.kind, "scheduled-overdue")
        self.assertEqual(decision.alert_type, "scheduled-run-overdue")
        self.assertFalse(decision.should_dispatch)
    def test_unknown_status_fails_closed(self):
        slot = dt.datetime(2026, 9, 4, 4, 17, tzinfo=UTC)
        with self.assertRaises(Exception) as raised:
            classify_day(
                now=dt.datetime(2026, 9, 4, 17, 20, tzinfo=UTC),
                slot=slot,
                runs=[run(103, status="mysterious", conclusion=None)],
            )
        self.assertIn("schema", str(raised.exception).lower())

    def test_non_string_status_is_rejected_as_schema_error(self):
        slot = dt.datetime(2026, 9, 4, 4, 17, tzinfo=UTC)
        malformed = run(407)
        malformed["status"] = []
        with self.assertRaises(WatchdogSchemaError):
            classify_day(
                now=dt.datetime(2026, 9, 4, 17, 20, tzinfo=UTC),
                slot=slot,
                runs=[malformed],
            )

    def test_non_string_conclusion_is_rejected_as_schema_error(self):
        slot = dt.datetime(2026, 9, 4, 4, 17, tzinfo=UTC)
        malformed = run(408)
        malformed["conclusion"] = []
        with self.assertRaises(WatchdogSchemaError):
            classify_day(
                now=dt.datetime(2026, 9, 4, 17, 20, tzinfo=UTC),
                slot=slot,
                runs=[malformed],
            )

    def test_boolean_run_id_is_rejected_as_schema_error(self):
        slot = dt.datetime(2026, 9, 4, 4, 17, tzinfo=UTC)
        malformed = run(103)
        malformed["id"] = True
        with self.assertRaises(WatchdogSchemaError):
            classify_day(
                now=dt.datetime(2026, 9, 4, 17, 20, tzinfo=UTC),
                slot=slot,
                runs=[malformed],
            )
    def test_missing_event_is_rejected_as_schema_error(self):
        slot = dt.datetime(2026, 9, 4, 4, 17, tzinfo=UTC)
        malformed = run(403)
        malformed.pop("event")
        with self.assertRaises(WatchdogSchemaError):
            classify_day(
                now=dt.datetime(2026, 9, 4, 17, 20, tzinfo=UTC),
                slot=slot,
                runs=[malformed],
            )

    def test_non_string_head_branch_is_rejected_as_schema_error(self):
        slot = dt.datetime(2026, 9, 4, 4, 17, tzinfo=UTC)
        malformed = run(404)
        malformed["head_branch"] = None
        with self.assertRaises(WatchdogSchemaError):
            classify_day(
                now=dt.datetime(2026, 9, 4, 17, 20, tzinfo=UTC),
                slot=slot,
                runs=[malformed],
            )

    def test_naive_created_at_is_rejected_as_schema_error(self):
        slot = dt.datetime(2026, 9, 4, 4, 17, tzinfo=UTC)
        malformed = run(405, created="2026-09-04T04:17:01")
        with self.assertRaises(WatchdogSchemaError):
            classify_day(
                now=dt.datetime(2026, 9, 4, 17, 20, tzinfo=UTC),
                slot=slot,
                runs=[malformed],
            )

    def test_missing_run_attempt_is_rejected_as_schema_error(self):
        slot = dt.datetime(2026, 9, 4, 4, 17, tzinfo=UTC)
        malformed = run(406)
        malformed.pop("run_attempt")
        with self.assertRaises(WatchdogSchemaError):
            classify_day(
                now=dt.datetime(2026, 9, 4, 17, 20, tzinfo=UTC),
                slot=slot,
                runs=[malformed],
            )

    def test_failed_scheduled_run_after_deadline_requests_recovery(self):
        slot = dt.datetime(2026, 9, 4, 4, 17, tzinfo=UTC)
        decision = classify_day(
            now=dt.datetime(2026, 9, 4, 17, 20, tzinfo=UTC),
            slot=slot,
            runs=[run(104, status="completed", conclusion="failure")],
        )
        self.assertEqual(decision.kind, "failed")
        self.assertTrue(decision.should_dispatch)
        self.assertEqual(decision.alert_type, "recovery-start")
    def test_unknown_conclusion_fails_closed(self):
        slot = dt.datetime(2026, 9, 4, 4, 17, tzinfo=UTC)
        with self.assertRaises(Exception) as raised:
            classify_day(
                now=dt.datetime(2026, 9, 4, 17, 20, tzinfo=UTC),
                slot=slot,
                runs=[run(105, status="queued", conclusion="mystery")],
            )
        self.assertIn("schema", str(raised.exception).lower())
    def test_active_push_or_manual_run_blocks_recovery_dispatch(self):
        slot = dt.datetime(2026, 9, 4, 4, 17, tzinfo=UTC)
        decision = classify_day(
            now=dt.datetime(2026, 9, 4, 17, 20, tzinfo=UTC),
            slot=slot,
            runs=[
                run(106, status="completed", conclusion="failure"),
                run(
                    107,
                    event="push",
                    status="in_progress",
                    conclusion=None,
                    created="2026-09-04T17:19:00Z",
                ),
            ],
        )
        self.assertEqual(decision.kind, "active-run")
        self.assertFalse(decision.should_dispatch)
        self.assertEqual(decision.alert_type, "")

    def test_active_scheduled_run_after_deadline_is_left_alone_and_alerted(self):
        slot = dt.datetime(2026, 9, 4, 4, 17, tzinfo=UTC)
        decision = classify_day(
            now=dt.datetime(2026, 9, 4, 17, 20, tzinfo=UTC),
            slot=slot,
            runs=[run(108, status="in_progress", conclusion=None)],
        )
        self.assertEqual(decision.kind, "scheduled-overdue")
        self.assertFalse(decision.should_dispatch)
        self.assertEqual(decision.alert_type, "scheduled-run-overdue")

    def test_missing_scheduled_run_after_deadline_requests_recovery(self):
        slot = dt.datetime(2026, 9, 4, 4, 17, tzinfo=UTC)
        decision = classify_day(
            now=dt.datetime(2026, 9, 4, 17, 20, tzinfo=UTC),
            slot=slot,
            runs=[],
        )
        self.assertEqual(decision.kind, "missing")
        self.assertTrue(decision.should_dispatch)

    def test_newer_successful_manual_run_suppresses_failed_schedule_recovery(self):
        slot = dt.datetime(2026, 9, 4, 4, 17, tzinfo=UTC)
        decision = classify_day(
            now=dt.datetime(2026, 9, 4, 17, 20, tzinfo=UTC),
            slot=slot,
            runs=[
                run(107, status="completed", conclusion="failure"),
                run(108, event="workflow_dispatch", created="2026-09-04T05:00:00Z"),
            ],
        )
        self.assertEqual(decision.kind, "newer-success")
        self.assertFalse(decision.should_dispatch)
        self.assertEqual(decision.production_run["id"], 108)
    def test_lock_rejects_an_overlapping_tick(self):
        with tempfile.TemporaryDirectory() as td:
            store = StateStore(pathlib.Path(td) / "state.json", pathlib.Path(td) / "watchdog.lock")
            first = store.acquire()
            self.assertTrue(first)
            try:
                with self.assertRaises(DuplicateTick):
                    store.acquire()
            finally:
                store.release()
            self.assertTrue(store.acquire())
            store.release()
    def test_two_unresolved_days_coexist_without_overwrite(self):
        with tempfile.TemporaryDirectory() as td:
            store = StateStore(pathlib.Path(td) / "state.json", pathlib.Path(td) / "watchdog.lock")
            state = store.load()
            store.ensure_day(state, "2026-09-03")["final_outcome"] = "older"
            store.ensure_day(state, "2026-09-04")["final_outcome"] = "newer"
            store.save(state)
            loaded = store.load()
            self.assertEqual(loaded["days"]["2026-09-03"]["final_outcome"], "older")
            self.assertEqual(loaded["days"]["2026-09-04"]["final_outcome"], "newer")
    def test_corrupt_or_unknown_state_is_rejected_without_replacement(self):
        with tempfile.TemporaryDirectory() as td:
            path = pathlib.Path(td) / "state.json"
            lock = pathlib.Path(td) / "watchdog.lock"
            path.write_text("{not-json", encoding="utf-8")
            store = StateStore(path, lock)
            with self.assertRaises(StateError):
                store.load()
            self.assertEqual(path.read_text(encoding="utf-8"), "{not-json")
            path.write_text('{"schema_version": 99, "days": {}}', encoding="utf-8")
            with self.assertRaises(StateError):
                store.load()
    def test_state_rejects_incomplete_day_records(self):
        with tempfile.TemporaryDirectory() as td:
            path = pathlib.Path(td) / "state.json"
            lock = pathlib.Path(td) / "watchdog.lock"
            path.write_text(
                json.dumps({
                    "schema_version": 1,
                    "days": {"2026-09-04": {"scheduled_day": "2026-09-04"}},
                }),
                encoding="utf-8",
            )
            with self.assertRaises(StateError):
                StateStore(path, lock).load()

    def test_schema_one_is_not_migrated_or_dispatchable(self):
        with tempfile.TemporaryDirectory() as td:
            path = pathlib.Path(td) / "state.json"
            lock = pathlib.Path(td) / "watchdog.lock"
            original = '{"schema_version":1,"days":{}}'
            path.write_text(original, encoding="utf-8")
            github = FakeGitHub([])
            notifier = RecordingNotifier()
            result = WatchdogController(
                repository="acme/epg", github=github, notifier=notifier,
                store=StateStore(path, lock),
                now=lambda: dt.datetime(2026, 9, 4, 18, 0, tzinfo=UTC),
            ).tick()
            self.assertEqual(result.exit_code, 0)
            self.assertEqual(github.dispatches, [])
            self.assertEqual(github.workflow_calls, 0)
            self.assertEqual(path.read_text(encoding="utf-8"), original)
            self.assertEqual(notifier.calls, [("ntfy:alerts", "event=state-error state=unreadable")])

    def test_schema_two_default_record_has_nullable_pending_diagnostic(self):
        record = default_day_record("2026-09-04")
        self.assertEqual(record["pending_diagnostic"], None)
        self.assertEqual(record["production_run_attempt"], None)

    def test_schema_two_valid_pending_diagnostic_survives_save_and_load(self):
        with tempfile.TemporaryDirectory() as td:
            store = StateStore(pathlib.Path(td) / "state.json", pathlib.Path(td) / "watchdog.lock")
            state = store.load()
            record = store.ensure_day(state, "2026-09-04")
            record["production_run_id"] = 501
            record["production_run_attempt"] = 2
            record["pending_diagnostic"] = pending_for(
                501, 2, "2026-09-04T18:00:00Z", "2026-09-18T18:00:00Z"
            )
            store.save(state)
            loaded = store.load()
            self.assertEqual(loaded["schema_version"], 2)
            self.assertEqual(loaded["days"]["2026-09-04"]["pending_diagnostic"],
                             pending_for(501, 2, "2026-09-04T18:00:00Z", "2026-09-18T18:00:00Z"))

    def test_pending_diagnostic_rejects_invalid_ids_attempts_and_timestamps(self):
        invalids = [
            dict(run_id=True),
            dict(run_attempt=0),
            dict(run_attempt=True),
            dict(first_observed_at="2026-09-04T18:00:00+00:00"),
            dict(first_observed_at="2026-02-30T18:00:00Z"),
            dict(deadline="2026-09-04T17:59:59Z"),
            dict(deadline="2026-09-19T18:00:00Z"),
        ]
        with tempfile.TemporaryDirectory() as td:
            store = StateStore(pathlib.Path(td) / "state.json", pathlib.Path(td) / "watchdog.lock")
            for changes in invalids:
                state = store.load()
                record = store.ensure_day(state, "2026-09-04")
                record["production_run_id"] = 501
                record["production_run_attempt"] = 2
                value = pending_for(501, 2)
                value.update(changes)
                record["pending_diagnostic"] = value
                with self.assertRaises(StateError):
                    store.save(state)

    def test_pending_diagnostic_rejects_non_object_values(self):
        with tempfile.TemporaryDirectory() as td:
            store = StateStore(pathlib.Path(td) / "state.json", pathlib.Path(td) / "watchdog.lock")
            for value in ({}, [], "bad", True):
                state = store.load()
                record = store.ensure_day(state, "2026-09-04")
                record["production_run_id"] = 501
                record["production_run_attempt"] = 1
                record["pending_diagnostic"] = value
                with self.assertRaises(StateError):
                    store.save(state)


    def test_pending_diagnostic_requires_matching_production_identity_and_nonterminal_state(self):
        with tempfile.TemporaryDirectory() as td:
            store = StateStore(pathlib.Path(td) / "state.json", pathlib.Path(td) / "watchdog.lock")
            cases = [
                {"production_run_id": None, "production_run_attempt": None},
                {"production_run_id": 501, "production_run_attempt": 2,
                 "pending_diagnostic": pending_for(502, 2)},
                {"production_run_id": 501, "production_run_attempt": 2,
                 "pending_diagnostic": pending_for(501, 1)},
                {"production_run_id": 501, "production_run_attempt": 2,
                 "terminal": True},
                {"production_run_id": 501, "production_run_attempt": 2,
                 "final_outcome": "healthy"},
            ]
            for changes in cases:
                state = store.load()
                record = store.ensure_day(state, "2026-09-04")
                record["production_run_id"] = 501
                record["production_run_attempt"] = 2
                record["pending_diagnostic"] = pending_for(501, 2)
                record.update(changes)
                with self.assertRaises(StateError):
                    store.save(state)

    def test_schema_two_dispatch_api_result_accepts_only_redacted_contract(self):
        valid = [
            {"status": "accepted", "http_status": 204},
            {"status": "accepted", "http_status": 200, "workflow_run_id": 777},
            {"status": "uncertain"},
        ]
        invalid = [
            {"http_status": 204},
            {"status": "accepted", "http_status": 201},
            {"status": "accepted", "http_status": 200},
            {"status": "accepted", "http_status": 200, "workflow_run_id": True},
            {"status": "uncertain", "error": "raw stderr"},
        ]
        with tempfile.TemporaryDirectory() as td:
            store = StateStore(pathlib.Path(td) / "state.json", pathlib.Path(td) / "watchdog.lock")
            for result in valid:
                state = store.load()
                record = store.ensure_day(state, "2026-09-04")
                record["dispatch_attempted"] = True
                record["dispatch_requested_at"] = "2026-09-04T18:00:00Z"
                record["watchdog_id"] = "2026-09-04.0123456789abcdef0123456789abcdef"
                record["dispatch_api_result"] = result
                store.save(state)
            for result in invalid:
                state = store.load()
                record = state["days"]["2026-09-04"]
                record["dispatch_api_result"] = result
                with self.assertRaises(StateError):
                    store.save(state)

    def test_pending_diagnostic_cannot_be_compacted_to_tombstone(self):
        with tempfile.TemporaryDirectory() as td:
            store = StateStore(pathlib.Path(td) / "state.json", pathlib.Path(td) / "watchdog.lock")
            state = store.load()
            record = store.ensure_day(state, "2026-09-04")
            record["production_run_id"] = 501
            record["production_run_attempt"] = 2
            record["pending_diagnostic"] = pending_for(501, 2)
            self.assertFalse(store.finalize_day(state, "2026-09-04"))
            self.assertFalse(record["tombstone"])

    def test_dispatch_reservation_is_durable_before_result(self):
        with tempfile.TemporaryDirectory() as td:
            store = StateStore(pathlib.Path(td) / "state.json", pathlib.Path(td) / "watchdog.lock")
            state = store.load()
            store.ensure_day(state, "2026-09-04")
            watchdog_id = "2026-09-04.0123456789abcdef0123456789abcdef"
            store.reserve_dispatch(state, "2026-09-04", "2026-09-04T17:20:00Z", watchdog_id)
            loaded = store.load()
            day = loaded["days"]["2026-09-04"]
            self.assertTrue(day["dispatch_attempted"])
            self.assertEqual(day["watchdog_id"], watchdog_id)
            store.record_dispatch(loaded, "2026-09-04", {"status": "accepted", "http_status": 204})
            final = store.load()["days"]["2026-09-04"]
            self.assertTrue(final["dispatch_attempted"])
            self.assertEqual(final["dispatch_api_result"], {"status": "accepted", "http_status": 204})
    def test_state_requires_complete_reservation_when_dispatch_attempted(self):
        with tempfile.TemporaryDirectory() as td:
            store = StateStore(pathlib.Path(td) / "state.json", pathlib.Path(td) / "watchdog.lock")
            state = store.load()
            record = store.ensure_day(state, "2026-09-04")
            record["dispatch_attempted"] = True
            with self.assertRaises(StateError):
                store.save(state)

    def test_state_rejects_dispatch_fields_without_attempt_marker(self):
        with tempfile.TemporaryDirectory() as td:
            store = StateStore(pathlib.Path(td) / "state.json", pathlib.Path(td) / "watchdog.lock")
            state = store.load()
            record = store.ensure_day(state, "2026-09-04")
            record["dispatch_requested_at"] = "2026-09-04T17:20:00Z"
            record["watchdog_id"] = "2026-09-04.0123456789abcdef0123456789abcdef"
            with self.assertRaises(StateError):
                store.save(state)

    def test_state_rejects_false_sent_markers_in_days_and_tombstones(self):
        with tempfile.TemporaryDirectory() as td:
            store = StateStore(pathlib.Path(td) / "state.json", pathlib.Path(td) / "watchdog.lock")
            state = store.load()
            record = store.ensure_day(state, "2026-09-04")
            record["alerts_sent"]["2026-09-04:alert"] = False
            with self.assertRaises(StateError):
                store.save(state)

            class RawStateStore(StateStore):
                @staticmethod
                def ensure_day(raw_state, day):
                    return raw_state["days"][day]

            raw_state = {
                "schema_version": 1,
                "days": {"2026-09-04": default_day_record("2026-09-04")},
            }
            raw_state["days"]["2026-09-04"]["alerts_sent"]["2026-09-04:alert"] = False
            RawStateStore(pathlib.Path(td) / "raw-state.json", pathlib.Path(td) / "raw.lock").queue_message(
                raw_state,
                "2026-09-04",
                "2026-09-04:alert",
                "ntfy:alerts",
                "retry body",
                save=False,
            )
            self.assertEqual(
                raw_state["days"]["2026-09-04"]["pending_messages"]["2026-09-04:alert"],
                {"target": "ntfy:alerts", "body": "retry body"},
            )

            state = store.load()
            record = store.ensure_day(state, "2026-09-04")
            record["dispatch_attempted"] = True
            record["dispatch_requested_at"] = "2026-09-04T17:20:00Z"
            record["watchdog_id"] = "2026-09-04.0123456789abcdef0123456789abcdef"
            record["terminal"] = True
            record["final_outcome"] = "recovery-failed"
            record["alerts_sent"]["2026-09-04:alert"] = True
            store.finalize_day(state, "2026-09-04")
            tombstone = store.load()
            tombstone["days"]["2026-09-04"]["alerts_sent"]["2026-09-04:bad"] = False
            with self.assertRaises(StateError):
                store.save(tombstone)

    def test_state_rejects_non_string_recovery_identity_as_state_error(self):
        with tempfile.TemporaryDirectory() as td:
            store = StateStore(pathlib.Path(td) / "state.json", pathlib.Path(td) / "watchdog.lock")
            state = store.load()
            record = store.ensure_day(state, "2026-09-04")
            record["dispatch_attempted"] = True
            record["dispatch_requested_at"] = "2026-09-04T17:20:00Z"
            record["watchdog_id"] = 123
            with self.assertRaises(StateError):
                store.save(state)

    def test_pending_message_is_persisted_until_successful_delivery(self):
        with tempfile.TemporaryDirectory() as td:
            store = StateStore(pathlib.Path(td) / "state.json", pathlib.Path(td) / "watchdog.lock")
            state = store.load()
            store.ensure_day(state, "2026-09-04")
            store.queue_message(state, "2026-09-04", "2026-09-04:alert", "ntfy:alerts", "redacted body")
            pending = store.load()["days"]["2026-09-04"]["pending_messages"]["2026-09-04:alert"]
            self.assertEqual(pending, {"target": "ntfy:alerts", "body": "redacted body"})
            store.mark_message_sent(state, "2026-09-04", "2026-09-04:alert")
            sent = store.load()["days"]["2026-09-04"]
            self.assertNotIn("2026-09-04:alert", sent["pending_messages"])
            self.assertIn("2026-09-04:alert", sent["alerts_sent"])

    def test_completed_day_compacts_only_after_pending_messages_are_sent(self):
        with tempfile.TemporaryDirectory() as td:
            store = StateStore(pathlib.Path(td) / "state.json", pathlib.Path(td) / "watchdog.lock")
            state = store.load()
            record = store.ensure_day(state, "2026-09-04")
            record["dispatch_attempted"] = True
            record["dispatch_requested_at"] = "2026-09-04T17:20:00Z"
            record["watchdog_id"] = "2026-09-04.0123456789abcdef0123456789abcdef"
            record["terminal"] = True
            record["final_outcome"] = "recovery-failed"
            store.queue_message(state, "2026-09-04", "2026-09-04:failed", "ntfy:alerts", "body")
            store.finalize_day(state, "2026-09-04")
            self.assertFalse(store.load()["days"]["2026-09-04"]["tombstone"])
            store.mark_message_sent(state, "2026-09-04", "2026-09-04:failed")
            store.finalize_day(state, "2026-09-04")
            tombstone = store.load()["days"]["2026-09-04"]
            self.assertTrue(tombstone["tombstone"])
            self.assertEqual(tombstone["final_outcome"], "recovery-failed")
            self.assertNotIn("pending_messages", tombstone)
    def test_workflow_state_uses_fixed_get_endpoint_and_timeout(self):
        runner = RecordingRunner({"id": 334673316, "state": "active"})
        adapter = GitHubAdapter("acme/epg", runner=runner)
        self.assertEqual(adapter.workflow_state(), {"id": 334673316, "state": "active"})
        self.assertEqual(len(runner.calls), 1)
        args, timeout, input_data = runner.calls[0]
        self.assertEqual(timeout, 30)
        self.assertIsNone(input_data)
        self.assertEqual(args, [
            "/opt/homebrew/bin/gh", "api", "--method", "GET",
            "repos/acme/epg/actions/workflows/build-epg.yml",
        ])
    def test_workflow_state_rejects_a_timed_out_command_even_with_output(self):
        class TimedOutRunner(RecordingRunner):
            def run(self, args, timeout, input_data=None):
                self.calls.append((list(args), timeout, input_data))
                return type("Result", (), {
                    "returncode": 0,
                    "stdout": json.dumps({"id": 334673316, "state": "active"}).encode("utf-8"),
                    "stderr": b"",
                    "timed_out": True,
                })()

        with self.assertRaises(GitHubError):
            GitHubAdapter("acme/epg", runner=TimedOutRunner(None)).workflow_state()

    def test_run_list_uses_bounded_main_query_and_page_timeout(self):
        payload = {"total_count": 1, "workflow_runs": [run(109)]}
        runner = RecordingRunner(payload)
        adapter = GitHubAdapter("acme/epg", runner=runner)
        result = adapter.list_runs("2026-09-04T04:17:00Z")
        self.assertEqual(result, payload["workflow_runs"])
        args, timeout, input_data = runner.calls[0]
        self.assertEqual(timeout, 30)
        self.assertIsNone(input_data)
        self.assertEqual(args[:4], ["/opt/homebrew/bin/gh", "api", "--method", "GET"])
        self.assertIn("branch=main", args[4])
        self.assertIn("per_page=100", args[4])
        self.assertIn("created=%3E%3D2026-09-04T04%3A17%3A00Z", args[4])
        self.assertIn("page=1", args[4])
    def test_run_list_rejects_boolean_reported_count(self):
        runner = RecordingRunner({"total_count": False, "workflow_runs": []})
        with self.assertRaises(GitHubError):
            GitHubAdapter("acme/epg", runner=runner).list_runs("2026-09-04T04:17:00Z")

    def test_run_list_reads_a_complete_second_page(self):
        first = [run(1000 + index) for index in range(100)]
        second = [run(1100)]
        runner = SequenceRunner([
            {"total_count": 101, "workflow_runs": first},
            {"total_count": 101, "workflow_runs": second},
        ])
        adapter = GitHubAdapter("acme/epg", runner=runner)
        self.assertEqual(len(adapter.list_runs("2026-09-04T04:17:00Z")), 101)
        self.assertEqual(len(runner.calls), 2)
        self.assertIn("page=2", runner.calls[1][0][4])
    def test_dispatch_accepts_legacy_empty_204_response_and_exact_payload(self):
        runner = RecordingRunner(None)
        adapter = GitHubAdapter("acme/epg", runner=runner)
        result = adapter.dispatch_recovery("2026-09-04.0123456789abcdef0123456789abcdef")
        self.assertEqual(result.http_status, 204)
        self.assertIsNone(result.workflow_run_id)
        args, timeout, body = runner.calls[0]
        self.assertEqual(timeout, 30)
        self.assertEqual(args, [
            "/opt/homebrew/bin/gh", "api", "--method", "POST",
            "repos/acme/epg/actions/workflows/build-epg.yml/dispatches", "--input", "-",
        ])
        self.assertEqual(json.loads(body.decode("utf-8")), {
            "ref": "main",
            "inputs": {"watchdog_id": "2026-09-04.0123456789abcdef0123456789abcdef"},
        })
    def test_dispatch_accepts_valid_200_response_and_stores_binding_fields(self):
        runner = RecordingRunner({
            "workflow_run_id": 777,
            "workflow_run_url": "https://github.com/acme/epg/actions/runs/777",
        }, http_status=200)
        result = GitHubAdapter("acme/epg", runner=runner).dispatch_recovery(
            "2026-09-04.0123456789abcdef0123456789abcdef"
        )
        self.assertEqual(result.http_status, 200)
        self.assertEqual(result.workflow_run_id, 777)
        self.assertEqual(result.workflow_run_url, "https://github.com/acme/epg/actions/runs/777")

    def test_dispatch_rejects_malformed_success_body(self):
        runner = RecordingRunner({"workflow_run_id": "777", "workflow_run_url": ""}, http_status=200)
        with self.assertRaises(Exception):
            GitHubAdapter("acme/epg", runner=runner).dispatch_recovery(
                "2026-09-04.0123456789abcdef0123456789abcdef"
            )
    def test_dispatch_rejects_a_success_body_with_unexpected_http_status(self):
        runner = RecordingRunner({
            "workflow_run_id": 777,
            "workflow_run_url": "https://github.com/acme/epg/actions/runs/777",
        }, http_status=201)
        with self.assertRaises(Exception):
            GitHubAdapter("acme/epg", runner=runner).dispatch_recovery(
                "2026-09-04.0123456789abcdef0123456789abcdef"
            )
    def test_recovery_binding_requires_exact_id_event_branch_and_title(self):
        watchdog_id = "2026-09-04.0123456789abcdef0123456789abcdef"
        exact = run(121, event="workflow_dispatch", created="2026-09-04T17:30:00Z")
        exact["display_title"] = "EPG watchdog recovery " + watchdog_id
        manual = run(122, event="workflow_dispatch", created="2026-09-04T17:31:00Z")
        manual["display_title"] = "Build EPG Guide (workflow_dispatch)"
        self.assertEqual(bind_recovery_run([manual, exact], watchdog_id)["id"], 121)
    def test_recovery_binding_rejects_name_without_exact_display_title(self):
        watchdog_id = "2026-09-04.0123456789abcdef0123456789abcdef"
        manual = run(123, event="workflow_dispatch", created="2026-09-04T17:31:00Z")
        manual["name"] = "EPG watchdog recovery " + watchdog_id
        self.assertIsNone(bind_recovery_run([manual], watchdog_id))

    def test_recovery_binding_rejects_non_string_identity_as_schema_error(self):
        with self.assertRaises(WatchdogSchemaError):
            bind_recovery_run([], 123)

    def test_artifact_list_and_download_have_fixed_endpoints_and_limits(self):
        runner = RecordingRunner({"total_count": 1, "artifacts": [{"id": 88}]})
        adapter = GitHubAdapter("acme/epg", runner=runner)
        self.assertEqual(adapter.list_artifacts(121), [{"id": 88}])
        args, timeout, _ = runner.calls[0]
        self.assertEqual(timeout, 30)
        self.assertEqual(args, [
            "/opt/homebrew/bin/gh", "api", "--method", "GET",
            "repos/acme/epg/actions/runs/121/artifacts?per_page=100",
        ])
        binary = BinaryRunner(b"zip-bytes")
        self.assertEqual(GitHubAdapter("acme/epg", runner=binary).download_artifact(88), b"zip-bytes")
        self.assertEqual(binary.calls[0][1], 60)
        self.assertEqual(binary.calls[0][0], [
            "/opt/homebrew/bin/gh", "api", "--method", "GET",
            "repos/acme/epg/actions/artifacts/88/zip",
        ])

    def test_artifact_list_rejects_reported_count_mismatch(self):
        runner = RecordingRunner({"total_count": 2, "artifacts": [{"id": 88}]})
        with self.assertRaises(GitHubError):
            GitHubAdapter("acme/epg", runner=runner).list_artifacts(121)

    def test_artifact_list_invalid_utf8_is_retryable_github_error(self):
        class InvalidUtf8Runner:
            def run(self, args, timeout, input_data=None):
                return type("Result", (), {
                    "returncode": 0,
                    "stdout": b"\xff",
                    "stderr": b"",
                })()

        with self.assertRaises(GitHubError):
            GitHubAdapter("acme/epg", runner=InvalidUtf8Runner()).list_artifacts(121)

    def test_invalid_utf8_artifact_list_keeps_pending_diagnostic(self):
        class InvalidUtf8ArtifactGitHub(FakeGitHub):
            def __init__(self):
                super().__init__([])
                self.adapter = GitHubAdapter(
                    "acme/epg",
                    runner=type("InvalidUtf8Runner", (), {
                        "run": lambda self, args, timeout, input_data=None: type(
                            "Result", (), {
                                "returncode": 0,
                                "stdout": b"\xff",
                                "stderr": b"",
                            }
                        )(),
                    })(),
                )

            def list_artifacts(self, run_id):
                return self.adapter.list_artifacts(run_id)

        with tempfile.TemporaryDirectory() as td:
            store = StateStore(pathlib.Path(td) / "state.json", pathlib.Path(td) / "watchdog.lock")
            state = store.load()
            record = store.ensure_day(state, "2026-09-04")
            record["production_run_id"] = 720
            record["production_run_attempt"] = 1
            record["pending_diagnostic"] = pending_for(720, 1)
            due = store.ensure_day(state, "2026-09-05")
            due["terminal"] = True
            due["final_outcome"] = "already-complete"
            store.save(state)
            notifier = RecordingNotifier()
            result = WatchdogController(
                repository="acme/epg", github=InvalidUtf8ArtifactGitHub(),
                notifier=notifier, store=store,
                now=lambda: dt.datetime(2026, 9, 5, 18, 0, tzinfo=UTC),
                monotonic=lambda: 0.0,
            ).tick()
            pending = store.load()["days"]["2026-09-04"]
            self.assertEqual(result.exit_code, 0)
            self.assertFalse(pending["terminal"])
            self.assertIsNotNone(pending["pending_diagnostic"])
            self.assertIn("diagnostic-unavailable", notifier.calls[0][1])

    def test_artifact_list_rejects_more_than_one_bounded_page(self):
        runner = RecordingRunner({
            "total_count": 101,
            "artifacts": [{"id": index} for index in range(1, 102)],
        })
        with self.assertRaises(GitHubError):
            GitHubAdapter("acme/epg", runner=runner).list_artifacts(121)

    def test_timeout_terminates_process_group_and_child(self):
        with tempfile.TemporaryDirectory() as td:
            pid_path = pathlib.Path(td) / "child.pid"
            child_code = "import time; time.sleep(30)"
            parent_code = (
                "import pathlib, signal, subprocess, sys, time; "
                "child=subprocess.Popen([sys.executable, '-c', sys.argv[2]]); "
                "signal.signal(signal.SIGTERM, lambda *_: (child.wait(timeout=2), sys.exit(0))); "
                "pathlib.Path(sys.argv[1]).write_text(str(child.pid)); time.sleep(30)"
            )
            result = SubprocessAdapter().run(
                [sys.executable, "-c", parent_code, str(pid_path), child_code],
                timeout=0.2,
            )
            self.assertTrue(result.timed_out)
            child_pid = int(pid_path.read_text(encoding="utf-8"))
            try:
                os.kill(child_pid, 0)
            except ProcessLookupError:
                pass
            else:
                self.fail("child process survived timeout cleanup")

    def test_deadline_runner_shortens_external_timeout_to_remaining_budget(self):
        class Underlying:
            def __init__(self):
                self.calls = []

            def run(self, args, timeout, input_data=None):
                self.calls.append((args, timeout, input_data))
                return type("Result", (), {
                    "returncode": 0, "stdout": b"", "stderr": b""
                })()

        underlying = Underlying()
        clock = iter((299.5,))
        runner = DeadlineRunner(underlying, monotonic=lambda: next(clock))
        runner.deadline = 300.0
        runner.run(["fixed-command"], 60, input_data=None)
        self.assertEqual(underlying.calls[0][1], 0.5)
    def test_diagnostic_reader_requires_run_attempt(self):
        successful = run(407)
        successful.pop("run_attempt")
        with self.assertRaises(DiagnosticError):
            DiagnosticReader().read(
                successful,
                [artifact_for(407)],
                (ROOT / "tests/fixtures/epg_watchdog/diagnostics-healthy.zip").read_bytes(),
            )

    def test_diagnostic_reader_requires_boolean_expired_field(self):
        successful = run(408)
        artifact = artifact_for(408)
        artifact.pop("expired")
        with self.assertRaises(DiagnosticError):
            DiagnosticReader().read(
                successful,
                [artifact],
                (ROOT / "tests/fixtures/epg_watchdog/diagnostics-healthy.zip").read_bytes(),
            )

    def test_diagnostic_reader_requires_aware_expiry_timestamp(self):
        successful = run(409)
        artifact = artifact_for(409)
        artifact.pop("expires_at")
        with self.assertRaises(DiagnosticError):
            DiagnosticReader().read(
                successful,
                [artifact],
                (ROOT / "tests/fixtures/epg_watchdog/diagnostics-healthy.zip").read_bytes(),
            )

    def test_diagnostic_reader_requires_nested_workflow_run_metadata(self):
        successful = run(410)
        artifact = artifact_for(410)
        artifact.pop("workflow_run")
        artifact["workflow_run_id"] = 410
        artifact["workflow_run_head_branch"] = "main"
        with self.assertRaises(DiagnosticError):
            DiagnosticReader().read(
                successful,
                [artifact],
                (ROOT / "tests/fixtures/epg_watchdog/diagnostics-healthy.zip").read_bytes(),
            )

    def test_healthy_diagnostic_artifact_is_read_in_memory(self):
        artifact = json.loads((ROOT / "tests/fixtures/epg_watchdog/artifacts-healthy.json").read_text())
        successful = run(101)
        successful["run_attempt"] = 1
        zip_bytes = (ROOT / "tests/fixtures/epg_watchdog/diagnostics-healthy.zip").read_bytes()
        result = DiagnosticReader().read(successful, artifact["artifacts"], zip_bytes)
        self.assertTrue(result.healthy)
        self.assertEqual(result.scraper_count, 2)
        self.assertEqual(result.degraded, {})
    def test_healthy_artifact_accepts_github_nested_workflow_run_metadata(self):
        successful = run(101)
        nested = artifact_for()
        result = DiagnosticReader().read(
            successful,
            [nested],
            (ROOT / "tests/fixtures/epg_watchdog/diagnostics-healthy.zip").read_bytes(),
        )
        self.assertTrue(result.healthy)

    def test_degraded_diagnostic_reports_exact_failed_entries(self):
        successful = run(101)
        successful["run_attempt"] = 1
        result = DiagnosticReader().read(
            successful,
            [artifact_for()],
            (ROOT / "tests/fixtures/epg_watchdog/diagnostics-degraded.zip").read_bytes(),
        )
        self.assertFalse(result.healthy)
        self.assertEqual(result.scraper_count, 2)
        self.assertEqual(result.degraded["alpha"]["count"], 0)

    def test_controller_does_not_download_oversized_artifact(self):
        class TrackingGitHub(FakeGitHub):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                self.download_calls = []

            def download_artifact(self, artifact_id):
                self.download_calls.append(artifact_id)
                return super().download_artifact(artifact_id)

        with tempfile.TemporaryDirectory() as td:
            store = StateStore(pathlib.Path(td) / "state.json", pathlib.Path(td) / "watchdog.lock")
            github = TrackingGitHub(
                [run(301)],
                [dict(artifact_for(), size_in_bytes=1024 * 1024 + 1)],
                (ROOT / "tests/fixtures/epg_watchdog/diagnostics-healthy.zip").read_bytes(),
            )
            result = WatchdogController(
                repository="acme/epg", github=github, notifier=RecordingNotifier(), store=store,
                now=lambda: dt.datetime(2026, 9, 4, 17, 20, tzinfo=UTC),
            ).tick()
            self.assertEqual(result.exit_code, 0)
            self.assertEqual(github.download_calls, [])

    def test_controller_does_not_download_duplicate_matching_artifacts(self):
        class TrackingGitHub(FakeGitHub):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                self.download_calls = []

            def download_artifact(self, artifact_id):
                self.download_calls.append(artifact_id)
                return super().download_artifact(artifact_id)

        with tempfile.TemporaryDirectory() as td:
            store = StateStore(pathlib.Path(td) / "state.json", pathlib.Path(td) / "watchdog.lock")
            artifact = artifact_for()
            github = TrackingGitHub(
                [run(302)], [artifact, dict(artifact)],
                (ROOT / "tests/fixtures/epg_watchdog/diagnostics-healthy.zip").read_bytes(),
            )
            result = WatchdogController(
                repository="acme/epg", github=github, notifier=RecordingNotifier(), store=store,
                now=lambda: dt.datetime(2026, 9, 4, 17, 20, tzinfo=UTC),
            ).tick()
            self.assertEqual(result.exit_code, 0)
            self.assertEqual(github.download_calls, [])

    def test_controller_does_not_download_wrong_run_artifact(self):
        class TrackingGitHub(FakeGitHub):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                self.download_calls = []

            def download_artifact(self, artifact_id):
                self.download_calls.append(artifact_id)
                return super().download_artifact(artifact_id)

        with tempfile.TemporaryDirectory() as td:
            store = StateStore(pathlib.Path(td) / "state.json", pathlib.Path(td) / "watchdog.lock")
            wrong = artifact_for()
            wrong["workflow_run"]["id"] = 999
            github = TrackingGitHub(
                [run(303)], [wrong],
                (ROOT / "tests/fixtures/epg_watchdog/diagnostics-healthy.zip").read_bytes(),
            )
            result = WatchdogController(
                repository="acme/epg", github=github, notifier=RecordingNotifier(), store=store,
                now=lambda: dt.datetime(2026, 9, 4, 17, 20, tzinfo=UTC),
            ).tick()
            self.assertEqual(result.exit_code, 0)
            self.assertEqual(github.download_calls, [])
    def test_zip_member_size_is_checked_before_read(self):
        successful = run(101)
        class Info:
            file_size = DiagnosticReader.max_artifact_bytes + 1

        class BombArchive:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def namelist(self):
                return ["pk_status.json"]

            def getinfo(self, name):
                return Info()

            def read(self, name):
                raise AssertionError("oversized member was read")

        with patch("ops.epg_github_watchdog.zipfile.ZipFile", return_value=BombArchive()):
            with self.assertRaises(DiagnosticError):
                DiagnosticReader().read(successful, [artifact_for()], b"safe-archive")

    def test_invalid_status_schema_is_rejected(self):
        successful = run(101)
        successful["run_attempt"] = 1
        archive = make_zip(
            "pk_status.json",
            b'{"alpha":{"ok":"yes","count":1,"error":null}}',
        )
        with self.assertRaises(DiagnosticError):
            DiagnosticReader().read(successful, [artifact_for()], archive)

    def test_duplicate_zip_member_is_rejected_without_emitting_fixture_warning(self):
        successful = run(101)
        successful["run_attempt"] = 1
        archive = make_duplicate_zip(
            "pk_status.json",
            b'{"alpha":{"ok":true,"count":1,"error":null}}',
            b'{"alpha":{"ok":false,"count":0,"error":"duplicate"}}',
        )
        with self.assertRaises(DiagnosticError):
            DiagnosticReader().read(successful, [artifact_for()], archive)

    def test_encrypted_status_member_becomes_terminal_artifact_error(self):
        encrypted_archive = base64.b64decode(
            "UEsDBAoACQAAAGVWJV0Qqfk2OQAAAC0AAAAOABwAcGtfc3RhdHVzLmpzb25VVAkAA41WnGqNVpxq"
            "dXgLAAEE9QEAAAQAAAAAeHgUB0TT/0K5fbT+Lq+0GqfyEv1r12KJkcyEkFpcFCx+J/e41/2M40CY"
            "710l4oV5o2G3usTAxSQhUEsHCBCp+TY5AAAALQAAAFBLAQIeAwoACQAAAGVWJV0Qqfk2OQAAAC0A"
            "AAAOABgAAAAAAAEAAACkgQAAAABwa19zdGF0dXMuanNvblVUBQADjVacanV4CwABBPUBAAAEAAAAAF"
            "BLBQYAAAAAAQABAFQAAACRAAAAAAA="
        )
        with tempfile.TemporaryDirectory() as td:
            store = StateStore(pathlib.Path(td) / "state.json", pathlib.Path(td) / "watchdog.lock")
            github = FakeGitHub([run(714)], [artifact_for(714)], encrypted_archive)
            notifier = RecordingNotifier()
            result = WatchdogController(
                repository="acme/epg", github=github, notifier=notifier, store=store,
                now=lambda: dt.datetime(2026, 9, 4, 18, 0, tzinfo=UTC),
                monotonic=lambda: 0.0,
            ).tick()
            final = store.load()["days"]["2026-09-04"]
            self.assertEqual(result.exit_code, 0)
            self.assertTrue(final["tombstone"])
            self.assertEqual(final["final_outcome"], "artifact-error")
            self.assertIn("artifact-error", notifier.calls[0][1])

    def test_corrupt_compressed_status_member_becomes_terminal_artifact_error(self):
        corrupt_archive = bytes.fromhex(
            "504b03041400000008000f5b255d8dbc979506000000640000000e000000706b5f737461747573"
            "2e6a736f6e7174a43d0000504b010214031400000008000f5b255d8dbc97950600000064000000"
            "0e0000000000000000000000800100000000706b5f7374617475732e6a736f6e504b0506000000"
            "00010001003c000000320000000000"
        )
        with self.assertRaises(zlib.error):
            with zipfile.ZipFile(io.BytesIO(corrupt_archive), "r") as archive:
                archive.read("pk_status.json")
        with tempfile.TemporaryDirectory() as td:
            store = StateStore(pathlib.Path(td) / "state.json", pathlib.Path(td) / "watchdog.lock")
            github = FakeGitHub([run(719)], [artifact_for(719)], corrupt_archive)
            notifier = RecordingNotifier()
            result = WatchdogController(
                repository="acme/epg", github=github, notifier=notifier, store=store,
                now=lambda: dt.datetime(2026, 9, 4, 18, 0, tzinfo=UTC),
                monotonic=lambda: 0.0,
            ).tick()
            final = store.load()["days"]["2026-09-04"]
            self.assertEqual(result.exit_code, 0)
            self.assertTrue(final["tombstone"])
            self.assertEqual(final["final_outcome"], "artifact-error")
            self.assertIn("artifact-error", notifier.calls[0][1])

    def test_notifier_accepts_only_fixed_targets_and_marks_success_after_exit_zero(self):
        runner = RecordingRunner(None)
        notifier = HermesNotifier(runner=runner)
        self.assertTrue(notifier.send("ntfy:reports", "event=2026-09-04:report EPG healthy"))
        args, timeout, body = runner.calls[0]
        self.assertEqual(timeout, 30)
        self.assertIsNone(body)
        self.assertEqual(args, [
            "/Users/shameez/.local/bin/hermes", "send", "--quiet", "--to",
            "ntfy:reports", "event=2026-09-04:report EPG healthy",
        ])
        with self.assertRaises(NotificationError):
            notifier.send("ntfy:other", "body")

    def test_notifier_failure_leaves_delivery_unsuccessful(self):
        with self.assertRaises(NotificationError):
            HermesNotifier(runner=FailingRunner(None)).send("ntfy:alerts", "event=failed")
    def test_notifier_rejects_a_timed_out_command_even_with_zero_exit(self):
        class TimedOutRunner(RecordingRunner):
            def run(self, args, timeout, input_data=None):
                self.calls.append((list(args), timeout, input_data))
                return type("Result", (), {
                    "returncode": 0,
                    "stdout": b"sent",
                    "stderr": b"",
                    "timed_out": True,
                })()

        with self.assertRaises(NotificationError):
            HermesNotifier(runner=TimedOutRunner(None)).send("ntfy:alerts", "event=failed")

    def test_hard_tick_budget_stops_work_before_any_github_call(self):
        with tempfile.TemporaryDirectory() as td:
            store = StateStore(pathlib.Path(td) / "state.json", pathlib.Path(td) / "watchdog.lock")
            github = FakeGitHub([])
            notifier = RecordingNotifier()
            ticks = iter((0.0, 301.0))
            controller = WatchdogController(
                repository="acme/epg", github=github, notifier=notifier, store=store,
                now=lambda: dt.datetime(2026, 9, 4, 17, 20, tzinfo=UTC),
                monotonic=lambda: next(ticks),
            )
            result = controller.tick()
            self.assertEqual(result.exit_code, 1)
            self.assertEqual(github.workflow_calls, 0)
            self.assertTrue(store.acquire())
            store.release()

    def test_dispatch_timeout_persists_dependency_alert_and_reservation(self):
        class TimedOutDispatchGitHub(FakeGitHub):
            def dispatch_recovery(self, watchdog_id):
                self.dispatches.append(watchdog_id)
                raise TickTimeoutError("tick exceeded its time budget")

        with tempfile.TemporaryDirectory() as td:
            store = StateStore(pathlib.Path(td) / "state.json", pathlib.Path(td) / "watchdog.lock")
            github = TimedOutDispatchGitHub([])
            notifier = RecordingNotifier(fail=True)
            controller = WatchdogController(
                repository="acme/epg", github=github, notifier=notifier, store=store,
                now=lambda: dt.datetime(2026, 9, 4, 17, 20, tzinfo=UTC),
            )

            result = controller.tick()

            self.assertEqual(result.exit_code, 1)
            self.assertEqual(len(github.dispatches), 1)
            day = store.load()["days"]["2026-09-04"]
            self.assertTrue(day["dispatch_attempted"])
            self.assertIsNone(day["dispatch_api_result"])
            pending = day["pending_messages"]["2026-09-04:dependency-error"]
            self.assertEqual(pending["target"], "ntfy:alerts")
            self.assertIn("event=2026-09-04:dependency-error", pending["body"])

    def test_dispatch_timeout_exits_zero_after_dependency_alert_is_sent(self):
        class TimedOutDispatchGitHub(FakeGitHub):
            def dispatch_recovery(self, watchdog_id):
                self.dispatches.append(watchdog_id)
                raise TickTimeoutError("tick exceeded its time budget")

        with tempfile.TemporaryDirectory() as td:
            store = StateStore(pathlib.Path(td) / "state.json", pathlib.Path(td) / "watchdog.lock")
            github = TimedOutDispatchGitHub([])
            notifier = RecordingNotifier()
            controller = WatchdogController(
                repository="acme/epg", github=github, notifier=notifier, store=store,
                now=lambda: dt.datetime(2026, 9, 4, 17, 20, tzinfo=UTC),
            )

            result = controller.tick()

            self.assertEqual(result.exit_code, 0)
            self.assertEqual(len(notifier.calls), 1)
            day = store.load()["days"]["2026-09-04"]
            self.assertEqual(day["pending_messages"], {})
            self.assertTrue(day["alerts_sent"]["2026-09-04:dependency-error"])
            self.assertTrue(day["dispatch_attempted"])
            self.assertIsNone(day["dispatch_api_result"])

    def test_hard_tick_budget_is_checked_after_a_slow_external_call(self):
        class SlowGitHub(FakeGitHub):
            def workflow_state(self):
                time.sleep(0.03)
                return super().workflow_state()

        with tempfile.TemporaryDirectory() as td:
            store = StateStore(pathlib.Path(td) / "state.json", pathlib.Path(td) / "watchdog.lock")
            github = SlowGitHub([])
            notifier = RecordingNotifier()
            controller = WatchdogController(
                repository="acme/epg", github=github, notifier=notifier, store=store,
                now=lambda: dt.datetime(2026, 9, 4, 17, 20, tzinfo=UTC),
                tick_limit_seconds=0.001,
            )
            result = controller.tick()
            self.assertEqual(result.exit_code, 0)
            self.assertEqual(github.dispatches, [])
            self.assertEqual(len(notifier.calls), 1)
            self.assertIn("dependency-error", notifier.calls[0][1])

    def test_controller_sends_one_healthy_report_and_finishes_day(self):
        with tempfile.TemporaryDirectory() as td:
            store = StateStore(pathlib.Path(td) / "state.json", pathlib.Path(td) / "watchdog.lock")
            github = FakeGitHub(
                [run(101)],
                [artifact_for()],
                (ROOT / "tests/fixtures/epg_watchdog/diagnostics-healthy.zip").read_bytes(),
            )
            notifier = RecordingNotifier()
            controller = WatchdogController(
                repository="acme/epg",
                github=github,
                notifier=notifier,
                store=store,
                now=lambda: dt.datetime(2026, 9, 4, 17, 20, tzinfo=UTC),
            )
            result = controller.tick()
            self.assertEqual(result.exit_code, 0)
            self.assertEqual(result.stdout, "")
            self.assertEqual(len(notifier.calls), 1)
            self.assertEqual(notifier.calls[0][0], "ntfy:reports")
            self.assertIn("EPG healthy", notifier.calls[0][1])
            self.assertTrue(store.load()["days"]["2026-09-04"]["tombstone"])
            self.assertEqual(github.dispatches, [])
    def test_controller_leaves_queued_schedule_alone_and_sends_one_overdue_alert(self):
        with tempfile.TemporaryDirectory() as td:
            store = StateStore(pathlib.Path(td) / "state.json", pathlib.Path(td) / "watchdog.lock")
            github = FakeGitHub([run(102, status="queued", conclusion=None)])
            notifier = RecordingNotifier()
            result = WatchdogController(
                repository="acme/epg", github=github, notifier=notifier, store=store,
                now=lambda: dt.datetime(2026, 9, 4, 17, 20, tzinfo=UTC),
            ).tick()
            self.assertEqual(result.exit_code, 0)
            self.assertEqual(github.dispatches, [])
            self.assertEqual([target for target, _ in notifier.calls], ["ntfy:alerts"])
            self.assertIn("scheduled-run-overdue", notifier.calls[0][1])


class ControllerEndToEndTest(unittest.TestCase):
    def _controller(self, td, runs, *, now=None, notifier=None):
        store = StateStore(pathlib.Path(td) / "state.json", pathlib.Path(td) / "watchdog.lock")
        github = FakeGitHub(runs)
        notifier = notifier or RecordingNotifier()
        controller = WatchdogController(
            repository="acme/epg",
            github=github,
            notifier=notifier,
            store=store,
            now=now or (lambda: dt.datetime(2026, 9, 4, 17, 20, tzinfo=UTC)),
        )
        return controller, store, github, notifier

    def test_controller_processes_yesterday_before_today_deadline(self):
        with tempfile.TemporaryDirectory() as td:
            controller, store, github, notifier = self._controller(
                td,
                [[], []],
                now=lambda: dt.datetime(2026, 9, 5, 10, 0, tzinfo=UTC),
            )
            result = controller.tick()
            self.assertEqual(result.exit_code, 0)
            self.assertEqual(github.workflow_calls, 2)
            self.assertEqual(len(github.run_calls), 2)
            self.assertEqual(len(github.dispatches), 1)
            self.assertTrue(github.dispatches[0].startswith("2026-09-04."))
            self.assertEqual([target for target, _ in notifier.calls], ["ntfy:alerts"])
            self.assertIn("recovery-start", notifier.calls[0][1])
            self.assertIn("2026-09-04", store.load()["days"])
            self.assertNotIn("2026-09-05", store.load()["days"])

    def test_controller_revisits_older_no_dispatch_day_without_duplicate_due_work(self):
        runs = [
            run(401, conclusion="failure", created="2026-09-04T04:17:01Z"),
            run(402, conclusion="failure", created="2026-09-05T04:17:01Z"),
        ]
        with tempfile.TemporaryDirectory() as td:
            controller, store, github, notifier = self._controller(
                td,
                [runs, runs],
                now=lambda: dt.datetime(2026, 9, 5, 17, 20, tzinfo=UTC),
            )
            state = store.load()
            store.ensure_day(state, "2026-09-04")
            store.save(state)

            result = controller.tick()

            self.assertEqual(result.exit_code, 0)
            self.assertEqual(len(github.dispatches), 2)
            self.assertTrue(github.dispatches[0].startswith("2026-09-04."))
            self.assertTrue(github.dispatches[1].startswith("2026-09-05."))
            self.assertEqual(len(notifier.calls), 2)
            self.assertEqual(
                {day for day in store.load()["days"]},
                {"2026-09-04", "2026-09-05"},
            )

    def test_controller_leaves_active_scheduled_run_alone_and_alerts_once(self):
        with tempfile.TemporaryDirectory() as td:
            controller, store, github, notifier = self._controller(
                td, [run(310, status="in_progress", conclusion=None)]
            )
            result = controller.tick()
            self.assertEqual(result.exit_code, 0)
            self.assertEqual(github.dispatches, [])
            self.assertEqual(len(notifier.calls), 1)
            self.assertIn("scheduled-run-overdue", notifier.calls[0][1])
            day = store.load()["days"]["2026-09-04"]
            self.assertEqual(day["scheduled_run_id"], 310)
            self.assertFalse(day["dispatch_attempted"])

    def test_recovery_running_is_bound_and_left_alone(self):
        watchdog_id = "2026-09-04.0123456789abcdef0123456789abcdef"
        recovery = run(
            311,
            event="workflow_dispatch",
            status="in_progress",
            conclusion=None,
            created="2026-09-04T17:30:00Z",
        )
        recovery["display_title"] = "EPG watchdog recovery " + watchdog_id
        with tempfile.TemporaryDirectory() as td:
            controller, store, github, notifier = self._controller(
                td,
                [recovery],
                now=lambda: dt.datetime(2026, 9, 4, 18, 0, tzinfo=UTC),
            )
            state = store.load()
            record = store.ensure_day(state, "2026-09-04")
            record["dispatch_attempted"] = True
            record["dispatch_requested_at"] = "2026-09-04T17:20:00Z"
            record["watchdog_id"] = watchdog_id
            store.save(state)

            result = controller.tick()

            self.assertEqual(result.exit_code, 0)
            self.assertEqual(notifier.calls, [])
            self.assertEqual(github.dispatches, [])
            day = store.load()["days"]["2026-09-04"]
            self.assertEqual(day["recovery_run_id"], 311)
            self.assertEqual(day["recovery_status"], "in_progress")
            self.assertFalse(day["terminal"])

    def test_recovery_success_is_read_reported_and_tombstoned(self):
        watchdog_id = "2026-09-04.0123456789abcdef0123456789abcdef"
        recovery = run(
            312,
            event="workflow_dispatch",
            created="2026-09-04T17:30:00Z",
        )
        recovery["display_title"] = "EPG watchdog recovery " + watchdog_id
        with tempfile.TemporaryDirectory() as td:
            archive = (ROOT / "tests/fixtures/epg_watchdog/diagnostics-healthy.zip").read_bytes()
            controller, store, github, notifier = self._controller(
                td,
                [recovery],
                now=lambda: dt.datetime(2026, 9, 4, 18, 0, tzinfo=UTC),
            )
            github.artifacts = [artifact_for(312)]
            github.archive = archive
            state = store.load()
            record = store.ensure_day(state, "2026-09-04")
            record["dispatch_attempted"] = True
            record["dispatch_requested_at"] = "2026-09-04T17:20:00Z"
            record["watchdog_id"] = watchdog_id
            store.save(state)

            result = controller.tick()

            self.assertEqual(result.exit_code, 0)
            self.assertEqual([target for target, _ in notifier.calls], ["ntfy:reports"])
            self.assertIn("EPG healthy", notifier.calls[0][1])
            day = store.load()["days"]["2026-09-04"]
            self.assertTrue(day["tombstone"])
            self.assertEqual(day["final_outcome"], "healthy")

    def test_recovery_failure_is_alerted_and_tombstoned(self):
        watchdog_id = "2026-09-04.0123456789abcdef0123456789abcdef"
        recovery = run(
            313,
            event="workflow_dispatch",
            status="completed",
            conclusion="failure",
            created="2026-09-04T17:30:00Z",
        )
        recovery["display_title"] = "EPG watchdog recovery " + watchdog_id
        with tempfile.TemporaryDirectory() as td:
            controller, store, github, notifier = self._controller(
                td,
                [recovery],
                now=lambda: dt.datetime(2026, 9, 4, 18, 0, tzinfo=UTC),
            )
            state = store.load()
            record = store.ensure_day(state, "2026-09-04")
            record["dispatch_attempted"] = True
            record["dispatch_requested_at"] = "2026-09-04T17:20:00Z"
            record["watchdog_id"] = watchdog_id
            store.save(state)

            result = controller.tick()

            self.assertEqual(result.exit_code, 0)
            self.assertEqual(len(notifier.calls), 1)
            self.assertIn("recovery-failed", notifier.calls[0][1])
            day = store.load()["days"]["2026-09-04"]
            self.assertTrue(day["tombstone"])
            self.assertEqual(day["final_outcome"], "recovery-failed")

    def test_failed_bound_recovery_prefers_newer_successful_production_run(self):
        watchdog_id = "2026-09-04.0123456789abcdef0123456789abcdef"
        recovery = run(
            401,
            event="workflow_dispatch",
            conclusion="failure",
            created="2026-09-04T17:30:00Z",
        )
        recovery["display_title"] = "EPG watchdog recovery " + watchdog_id
        newer = run(402, event="push", created="2026-09-04T18:00:00Z")
        with tempfile.TemporaryDirectory() as td:
            archive = (ROOT / "tests/fixtures/epg_watchdog/diagnostics-healthy.zip").read_bytes()
            controller, store, github, notifier = self._controller(
                td,
                [recovery, newer],
                now=lambda: dt.datetime(2026, 9, 4, 18, 10, tzinfo=UTC),
            )
            github.artifacts = [artifact_for(402)]
            github.archive = archive
            state = store.load()
            record = store.ensure_day(state, "2026-09-04")
            record["dispatch_attempted"] = True
            record["dispatch_requested_at"] = "2026-09-04T17:20:00Z"
            record["watchdog_id"] = watchdog_id
            store.save(state)

            result = controller.tick()

            self.assertEqual(result.exit_code, 0)
            self.assertEqual([target for target, _ in notifier.calls], ["ntfy:reports"])
            self.assertIn("EPG healthy", notifier.calls[0][1])
            day = store.load()["days"]["2026-09-04"]
            self.assertTrue(day["tombstone"])
            self.assertEqual(day["final_outcome"], "healthy")
            self.assertIn("runs/402", notifier.calls[0][1])

    def test_newer_success_after_dispatch_satisfies_recovery_day(self):
        watchdog_id = "2026-09-04.0123456789abcdef0123456789abcdef"
        newer = run(
            316,
            event="push",
            created="2026-09-04T18:00:00Z",
        )
        with tempfile.TemporaryDirectory() as td:
            controller, store, github, notifier = self._controller(
                td,
                [newer],
                now=lambda: dt.datetime(2026, 9, 4, 18, 10, tzinfo=UTC),
            )
            github.artifacts = [artifact_for(316)]
            github.archive = (ROOT / "tests/fixtures/epg_watchdog/diagnostics-healthy.zip").read_bytes()
            state = store.load()
            record = store.ensure_day(state, "2026-09-04")
            record["dispatch_attempted"] = True
            record["dispatch_requested_at"] = "2026-09-04T17:20:00Z"
            record["watchdog_id"] = watchdog_id
            store.save(state)

            result = controller.tick()

            self.assertEqual(result.exit_code, 0)
            self.assertEqual([target for target, _ in notifier.calls], ["ntfy:reports"])
            self.assertIn("EPG healthy", notifier.calls[0][1])
            day = store.load()["days"]["2026-09-04"]
            self.assertTrue(day["tombstone"])
            self.assertEqual(day["final_outcome"], "healthy")

    def test_artifact_timeout_queues_alert_and_preserves_dispatch_reservation(self):
        watchdog_id = "2026-09-04.0123456789abcdef0123456789abcdef"
        recovery = run(
            414,
            event="workflow_dispatch",
            created="2026-09-04T17:30:00Z",
        )
        recovery["display_title"] = "EPG watchdog recovery " + watchdog_id

        class ArtifactTimeoutGitHub(FakeGitHub):
            def list_artifacts(self, run_id):
                raise TickTimeoutError("artifact list timed out")

        with tempfile.TemporaryDirectory() as td:
            store = StateStore(pathlib.Path(td) / "state.json", pathlib.Path(td) / "watchdog.lock")
            github = ArtifactTimeoutGitHub([recovery])
            notifier = RecordingNotifier()
            state = store.load()
            record = store.ensure_day(state, "2026-09-04")
            record["dispatch_attempted"] = True
            record["dispatch_requested_at"] = "2026-09-04T17:20:00Z"
            record["watchdog_id"] = watchdog_id
            store.save(state)

            result = WatchdogController(
                repository="acme/epg", github=github, notifier=notifier, store=store,
                now=lambda: dt.datetime(2026, 9, 4, 18, 0, tzinfo=UTC),
            ).tick()

            self.assertEqual(result.exit_code, 0)
            self.assertEqual(len(notifier.calls), 1)
            self.assertEqual(notifier.calls[0][0], "ntfy:alerts")
            self.assertIn("diagnostic-unavailable", notifier.calls[0][1])
            day = store.load()["days"]["2026-09-04"]
            self.assertFalse(day["tombstone"])
            self.assertTrue(day["dispatch_attempted"])
            self.assertEqual(day["watchdog_id"], watchdog_id)
            self.assertFalse(day["terminal"])
            self.assertIsNone(day["final_outcome"])
            self.assertIsNotNone(day["pending_diagnostic"])

    def test_pending_diagnostic_oserror_queues_unavailable_and_remains_pending(self):
        class OSErrorGitHub(FakeGitHub):
            def list_artifacts(self, run_id):
                raise OSError("temporary artifact read failure")

        with tempfile.TemporaryDirectory() as td:
            store = StateStore(pathlib.Path(td) / "state.json", pathlib.Path(td) / "watchdog.lock")
            state = store.load()
            record = store.ensure_day(state, "2026-09-04")
            record["production_run_id"] = 615
            record["production_run_attempt"] = 1
            record["pending_diagnostic"] = pending_for(615, 1)
            store.save(state)
            github = OSErrorGitHub([])
            notifier = RecordingNotifier()

            result = WatchdogController(
                repository="acme/epg", github=github, notifier=notifier, store=store,
                now=lambda: dt.datetime(2026, 9, 4, 19, 0, tzinfo=UTC),
            ).tick()

            persisted = store.load()["days"]["2026-09-04"]
            self.assertEqual(result.exit_code, 0)
            self.assertEqual(github.workflow_calls, 0)
            self.assertEqual(len(notifier.calls), 1)
            self.assertEqual(notifier.calls[0][0], "ntfy:alerts")
            self.assertIn("diagnostic-unavailable", notifier.calls[0][1])
            self.assertNotIn("dependency-error", notifier.calls[0][1])
            self.assertFalse(persisted["terminal"])
            self.assertIsNone(persisted["final_outcome"])
            self.assertIsNotNone(persisted["pending_diagnostic"])
            self.assertIn("2026-09-04:diagnostic-unavailable", persisted["alerts_sent"])

    def test_terminal_alert_remains_pending_if_send_commit_crashes(self):
        class CrashStore(StateStore):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                self.save_count = 0

            def save(self, state):
                self.save_count += 1
                if self.save_count == 4:
                    raise RuntimeError("crash while queuing alert")
                return super().save(state)

        with tempfile.TemporaryDirectory() as td:
            store = CrashStore(pathlib.Path(td) / "state.json", pathlib.Path(td) / "watchdog.lock")
            bad_artifact = dict(artifact_for(304), size_in_bytes=DiagnosticReader.max_artifact_bytes + 1)
            github = FakeGitHub([run(304)], [bad_artifact], b"")
            controller = WatchdogController(
                repository="acme/epg", github=github, notifier=RecordingNotifier(), store=store,
                now=lambda: dt.datetime(2026, 9, 4, 17, 20, tzinfo=UTC),
            )
            with self.assertRaises(RuntimeError):
                controller.tick()
            persisted = store.load()["days"]["2026-09-04"]
            self.assertTrue(persisted["terminal"])
            self.assertEqual(
                persisted["pending_messages"],
                {
                    "2026-09-04:artifact-error": {
                        "target": "ntfy:alerts",
                        "body": "event=2026-09-04:artifact-error day=2026-09-04 run=304",
                    }
                },
            )

    def test_controller_ignores_active_push_without_false_scheduled_overdue_alert(self):
        with tempfile.TemporaryDirectory() as td:
            controller, store, github, notifier = self._controller(
                td,
                [run(
                    305,
                    event="push",
                    status="in_progress",
                    conclusion=None,
                    created="2026-09-04T17:19:00Z",
                )],
            )
            result = controller.tick()
            self.assertEqual(result.exit_code, 0)
            self.assertEqual(result.decision.kind, "active-run")
            self.assertEqual(github.dispatches, [])
            self.assertEqual(notifier.calls, [])
            self.assertFalse(store.load()["days"]["2026-09-04"]["terminal"])

    def test_expired_unresolved_recovery_avoids_github_reads_when_newest_due_is_tombstoned(self):
        with tempfile.TemporaryDirectory() as td:
            controller, store, github, notifier = self._controller(
                td, [], now=lambda: dt.datetime(2026, 9, 19, 10, 0, tzinfo=UTC)
            )
            state = store.load()
            record = store.ensure_day(state, "2026-09-04")
            record["dispatch_attempted"] = True
            record["dispatch_requested_at"] = "2026-09-04T09:00:00Z"
            record["watchdog_id"] = "2026-09-04.0123456789abcdef0123456789abcdef"
            store.save(state)
            newest = store.ensure_day(state, "2026-09-18")
            newest["terminal"] = True
            newest["final_outcome"] = "already-complete"
            store.save(state)
            store.finalize_day(state, "2026-09-18")

            result = controller.tick()

            self.assertEqual(result.exit_code, 0)
            self.assertEqual(github.workflow_calls, 0)
            self.assertEqual(github.run_calls, [])
            self.assertEqual(len(notifier.calls), 1)
            self.assertIn("expired-unresolved", notifier.calls[0][1])
            final = store.load()["days"]["2026-09-04"]
            self.assertTrue(final["tombstone"])
            self.assertEqual(final["final_outcome"], "expired-unresolved")
            self.assertTrue(store.load()["days"]["2026-09-18"]["tombstone"])

    def test_pending_diagnostic_crossing_recovery_expiry_closes_before_github_reads(self):
        archive = (ROOT / "tests/fixtures/epg_watchdog/diagnostics-healthy.zip").read_bytes()

        class WallClock:
            def __init__(self):
                self.value = dt.datetime(2026, 9, 18, 17, 59, 59, tzinfo=UTC)

            def __call__(self):
                return self.value

        class AdvancingGitHub(FakeGitHub):
            def __init__(self, clock, *args, **kwargs):
                super().__init__(*args, **kwargs)
                self.clock = clock

            def list_artifacts(self, run_id):
                self.clock.value = dt.datetime(2026, 9, 18, 18, 0, 1, tzinfo=UTC)
                return super().list_artifacts(run_id)

        with tempfile.TemporaryDirectory() as td:
            store = StateStore(pathlib.Path(td) / "state.json", pathlib.Path(td) / "watchdog.lock")
            state = store.load()
            recovery = store.ensure_day(state, "2026-09-04")
            recovery["dispatch_attempted"] = True
            recovery["dispatch_requested_at"] = "2026-09-04T18:00:00Z"
            recovery["dispatch_api_result"] = {"status": "uncertain"}
            recovery["watchdog_id"] = "2026-09-04.0123456789abcdef0123456789abcdef"
            diagnostic = store.ensure_day(state, "2026-09-18")
            diagnostic["production_run_id"] = 715
            diagnostic["production_run_attempt"] = 1
            diagnostic["pending_diagnostic"] = pending_for(
                715, 1, "2026-09-18T17:59:00Z", "2026-10-02T17:59:00Z"
            )
            store.save(state)
            clock = WallClock()
            github = AdvancingGitHub(clock, [], [artifact_for(715)], archive)
            notifier = RecordingNotifier()
            result = WatchdogController(
                repository="acme/epg", github=github, notifier=notifier, store=store,
                now=clock, monotonic=lambda: 0.0,
            ).tick()
            final = store.load()["days"]["2026-09-04"]
            self.assertEqual(result.exit_code, 0)
            self.assertEqual(github.workflow_calls, 0)
            self.assertEqual(github.run_calls, [])
            self.assertTrue(final["tombstone"])
            self.assertEqual(final["final_outcome"], "expired-unresolved")
            self.assertIn("expired-unresolved", " ".join(message for _, message in notifier.calls))

    def test_workflow_state_crossing_recovery_expiry_prevents_run_list_read(self):
        class WallClock:
            def __init__(self):
                self.value = dt.datetime(2026, 9, 18, 17, 59, 59, tzinfo=UTC)

            def __call__(self):
                return self.value

        class AdvancingGitHub(FakeGitHub):
            def __init__(self, clock, *args, **kwargs):
                super().__init__(*args, **kwargs)
                self.clock = clock

            def workflow_state(self):
                result = super().workflow_state()
                self.clock.value = dt.datetime(2026, 9, 18, 18, 0, 1, tzinfo=UTC)
                return result

        with tempfile.TemporaryDirectory() as td:
            store = StateStore(pathlib.Path(td) / "state.json", pathlib.Path(td) / "watchdog.lock")
            state = store.load()
            recovery = store.ensure_day(state, "2026-09-04")
            recovery["dispatch_attempted"] = True
            recovery["dispatch_requested_at"] = "2026-09-04T18:00:00Z"
            recovery["dispatch_api_result"] = {"status": "uncertain"}
            recovery["watchdog_id"] = "2026-09-04.0123456789abcdef0123456789abcdef"
            due = store.ensure_day(state, "2026-09-18")
            due["terminal"] = True
            due["final_outcome"] = "already-complete"
            store.save(state)
            store.finalize_day(state, "2026-09-18")
            clock = WallClock()
            github = AdvancingGitHub(clock, [])
            notifier = RecordingNotifier()
            result = WatchdogController(
                repository="acme/epg", github=github, notifier=notifier, store=store,
                now=clock, monotonic=lambda: 0.0,
            ).tick()
            final = store.load()["days"]["2026-09-04"]
            self.assertEqual(result.exit_code, 0)
            self.assertEqual(github.workflow_calls, 1)
            self.assertEqual(github.run_calls, [])
            self.assertTrue(final["tombstone"])
            self.assertEqual(final["final_outcome"], "expired-unresolved")
            self.assertIn("expired-unresolved", notifier.calls[0][1])

    def test_workflow_list_crossing_recovery_expiry_closes_before_binding(self):
        class WallClock:
            def __init__(self):
                self.value = dt.datetime(2026, 9, 18, 17, 59, 59, tzinfo=UTC)

            def __call__(self):
                return self.value

        class AdvancingGitHub(FakeGitHub):
            def __init__(self, clock, *args, **kwargs):
                super().__init__(*args, **kwargs)
                self.clock = clock

            def list_runs(self, created_after):
                result = super().list_runs(created_after)
                self.clock.value = dt.datetime(2026, 9, 18, 18, 0, 1, tzinfo=UTC)
                return result

        with tempfile.TemporaryDirectory() as td:
            store = StateStore(pathlib.Path(td) / "state.json", pathlib.Path(td) / "watchdog.lock")
            state = store.load()
            recovery = store.ensure_day(state, "2026-09-04")
            recovery["dispatch_attempted"] = True
            recovery["dispatch_requested_at"] = "2026-09-04T18:00:00Z"
            recovery["dispatch_api_result"] = {"status": "uncertain"}
            recovery["watchdog_id"] = "2026-09-04.0123456789abcdef0123456789abcdef"
            due = store.ensure_day(state, "2026-09-18")
            due["terminal"] = True
            due["final_outcome"] = "already-complete"
            store.save(state)
            store.finalize_day(state, "2026-09-18")
            clock = WallClock()
            github = AdvancingGitHub(clock, [])
            notifier = RecordingNotifier()
            result = WatchdogController(
                repository="acme/epg", github=github, notifier=notifier, store=store,
                now=clock, monotonic=lambda: 0.0,
            ).tick()
            final = store.load()["days"]["2026-09-04"]
            self.assertEqual(result.exit_code, 0)
            self.assertEqual(github.workflow_calls, 1)
            self.assertEqual(github.run_calls, ["2026-09-04T04:17:00Z"])
            self.assertTrue(final["tombstone"])
            self.assertEqual(final["final_outcome"], "expired-unresolved")
            self.assertIn("expired-unresolved", notifier.calls[0][1])

    def test_duplicate_recovery_identity_is_terminal_and_distinct_from_schema_error(self):
        watchdog_id = "2026-09-04.0123456789abcdef0123456789abcdef"
        exact_title = "EPG watchdog recovery " + watchdog_id
        exact_one = run(306, event="workflow_dispatch", created="2026-09-04T18:00:00Z")
        exact_one["display_title"] = exact_title
        exact_two = run(307, event="workflow_dispatch", created="2026-09-04T18:01:00Z")
        exact_two["display_title"] = exact_title
        with tempfile.TemporaryDirectory() as td:
            controller, store, github, notifier = self._controller(
                td,
                [exact_one, exact_two],
                now=lambda: dt.datetime(2026, 9, 5, 10, 0, tzinfo=UTC),
            )
            state = store.load()
            record = store.ensure_day(state, "2026-09-04")
            record["dispatch_attempted"] = True
            record["dispatch_requested_at"] = "2026-09-04T17:20:00Z"
            record["watchdog_id"] = watchdog_id
            store.save(state)

            result = controller.tick()

            self.assertEqual(result.exit_code, 0)
            self.assertEqual(len(notifier.calls), 1)
            self.assertIn("ambiguous-recovery", notifier.calls[0][1])
            self.assertEqual(store.load()["days"]["2026-09-04"]["final_outcome"], "ambiguous-recovery")
            self.assertEqual(github.dispatches, [])

    def test_recovery_schema_error_is_dependency_alert_and_not_ambiguous(self):
        malformed = run(308, event="workflow_dispatch", created="2026-09-04T18:00:00Z")
        malformed["display_title"] = "EPG watchdog recovery 2026-09-04.0123456789abcdef0123456789abcdef"
        malformed["status"] = "mysterious"
        with tempfile.TemporaryDirectory() as td:
            controller, store, github, notifier = self._controller(
                td,
                [malformed],
                now=lambda: dt.datetime(2026, 9, 5, 10, 0, tzinfo=UTC),
            )
            state = store.load()
            record = store.ensure_day(state, "2026-09-04")
            record["dispatch_attempted"] = True
            record["dispatch_requested_at"] = "2026-09-04T17:20:00Z"
            record["watchdog_id"] = "2026-09-04.0123456789abcdef0123456789abcdef"
            store.save(state)

            result = controller.tick()

            self.assertEqual(result.exit_code, 0)
            self.assertEqual(len(notifier.calls), 1)
            self.assertIn("dependency-error", notifier.calls[0][1])
            day = store.load()["days"]["2026-09-04"]
            self.assertFalse(day["terminal"])
            self.assertIsNone(day["final_outcome"])
            self.assertNotIn("ambiguous-recovery", day["alerts_sent"])

    def test_schema_error_on_older_recovery_alerts_the_older_day(self):
        malformed = run(317, event="workflow_dispatch", created="2026-09-04T18:00:00Z")
        malformed["display_title"] = "EPG watchdog recovery 2026-09-04.0123456789abcdef0123456789abcdef"
        malformed["status"] = "mysterious"
        with tempfile.TemporaryDirectory() as td:
            controller, store, github, notifier = self._controller(
                td,
                [malformed],
                now=lambda: dt.datetime(2026, 9, 5, 17, 20, tzinfo=UTC),
            )
            state = store.load()
            record = store.ensure_day(state, "2026-09-04")
            record["dispatch_attempted"] = True
            record["dispatch_requested_at"] = "2026-09-04T17:20:00Z"
            record["watchdog_id"] = "2026-09-04.0123456789abcdef0123456789abcdef"
            store.save(state)

            result = controller.tick()

            self.assertEqual(result.exit_code, 0)
            self.assertEqual(len(notifier.calls), 1)
            self.assertIn("day=2026-09-04", notifier.calls[0][1])
            self.assertIn("2026-09-04:dependency-error", store.load()["days"]["2026-09-04"]["alerts_sent"])
            self.assertNotIn("2026-09-05", store.load()["days"])

    def test_github_error_with_older_pending_recovery_alerts_that_recovery(self):
        with tempfile.TemporaryDirectory() as td:
            controller, store, github, notifier = self._controller(
                td, [], now=lambda: dt.datetime(2026, 9, 5, 10, 0, tzinfo=UTC)
            )
            state = store.load()
            record = store.ensure_day(state, "2026-09-04")
            record["dispatch_attempted"] = True
            record["dispatch_requested_at"] = "2026-09-04T17:20:00Z"
            record["watchdog_id"] = "2026-09-04.0123456789abcdef0123456789abcdef"
            store.save(state)
            github.workflow_error = True
            result = controller.tick()
            self.assertEqual(result.exit_code, 0)
            self.assertEqual(len(notifier.calls), 1)
            self.assertIn("2026-09-04", notifier.calls[0][1])
            self.assertEqual(notifier.calls[0][0], "ntfy:alerts")
    def test_failed_schedule_reads_twice_reserves_then_dispatches_once_without_cancel(self):
        with tempfile.TemporaryDirectory() as td:
            controller, store, github, notifier = self._controller(
                td, [run(201, conclusion="failure"), run(201, conclusion="failure")]
            )
            result = controller.tick()
            self.assertEqual(result.exit_code, 0)
            self.assertEqual(github.workflow_calls, 2)
            self.assertEqual(len(github.run_calls), 2)
            self.assertEqual(len(github.dispatches), 1)
            self.assertEqual(len(notifier.calls), 1)
            self.assertEqual(notifier.calls[0][0], "ntfy:alerts")
            self.assertTrue(store.load()["days"]["2026-09-04"]["dispatch_attempted"])

    def test_missing_schedule_reads_twice_reserves_then_dispatches_once_without_cancel(self):
        with tempfile.TemporaryDirectory() as td:
            controller, store, github, notifier = self._controller(td, [[], []])
            result = controller.tick()
            self.assertEqual(result.exit_code, 0)
            self.assertEqual(github.workflow_calls, 2)
            self.assertEqual(len(github.run_calls), 2)
            self.assertEqual(len(github.dispatches), 1)
            self.assertEqual(len(notifier.calls), 1)
            self.assertEqual(notifier.calls[0][0], "ntfy:alerts")
            self.assertTrue(store.load()["days"]["2026-09-04"]["dispatch_attempted"])
    def test_dispatch_sees_durable_reservation_before_api_call(self):
        class ReservationCheckingGitHub(FakeGitHub):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                self.store = None
                self.reservation = None

            def dispatch_recovery(self, watchdog_id):
                day = self.store.load()["days"]["2026-09-04"]
                self.reservation = {
                    "dispatch_attempted": day["dispatch_attempted"],
                    "watchdog_id": day["watchdog_id"],
                }
                return super().dispatch_recovery(watchdog_id)

        with tempfile.TemporaryDirectory() as td:
            store = StateStore(pathlib.Path(td) / "state.json", pathlib.Path(td) / "watchdog.lock")
            github = ReservationCheckingGitHub([run(315, conclusion="failure"), run(315, conclusion="failure")])
            github.store = store
            notifier = RecordingNotifier()
            result = WatchdogController(
                repository="acme/epg", github=github, notifier=notifier, store=store,
                now=lambda: dt.datetime(2026, 9, 4, 17, 20, tzinfo=UTC),
            ).tick()
            self.assertEqual(result.exit_code, 0)
            self.assertTrue(github.reservation["dispatch_attempted"])
            self.assertEqual(github.reservation["watchdog_id"], github.dispatches[0])

    def test_uncertain_dispatch_is_observed_on_later_tick_without_redispatch(self):
        class AcceptedButUncertainGitHub(FakeGitHub):
            def dispatch_recovery(self, watchdog_id):
                self.dispatches.append(watchdog_id)
                recovery = run(
                    319,
                    event="workflow_dispatch",
                    created="2026-09-04T17:30:00Z",
                )
                recovery["display_title"] = "EPG watchdog recovery " + watchdog_id
                self.runs = [[recovery]]
                raise GitHubError("simulated adapter failure after POST")

        archive = (ROOT / "tests/fixtures/epg_watchdog/diagnostics-healthy.zip").read_bytes()
        with tempfile.TemporaryDirectory() as td:
            store = StateStore(pathlib.Path(td) / "state.json", pathlib.Path(td) / "watchdog.lock")
            failed = run(318, conclusion="failure")
            github = AcceptedButUncertainGitHub([[failed], [failed]], [artifact_for(319)], archive)
            notifier = RecordingNotifier()
            controller = WatchdogController(
                repository="acme/epg", github=github, notifier=notifier, store=store,
                now=lambda: dt.datetime(2026, 9, 4, 18, 0, tzinfo=UTC),
            )

            first = controller.tick()

            self.assertEqual(first.exit_code, 0)
            self.assertEqual(len(github.dispatches), 1)
            watchdog_id = github.dispatches[0]
            pending_or_sent = store.load()["days"]["2026-09-04"]
            self.assertFalse(pending_or_sent["tombstone"])
            self.assertEqual(pending_or_sent["watchdog_id"], watchdog_id)
            self.assertEqual(pending_or_sent["dispatch_api_result"], {"status": "uncertain"})
            self.assertFalse(pending_or_sent["terminal"])
            self.assertIsNone(pending_or_sent["final_outcome"])
            self.assertEqual(len(notifier.calls), 1)
            self.assertEqual(notifier.calls[0][0], "ntfy:alerts")
            self.assertIn("dispatch-uncertain", notifier.calls[0][1])
            self.assertIn("may have been accepted", notifier.calls[0][1])

            second = controller.tick()

            self.assertEqual(second.exit_code, 0)
            self.assertEqual(len(github.dispatches), 1)
            self.assertEqual([target for target, _ in notifier.calls], ["ntfy:alerts", "ntfy:reports"])
            self.assertIn("EPG healthy", notifier.calls[1][1])
            final = store.load()["days"]["2026-09-04"]
            self.assertTrue(final["tombstone"])
            self.assertEqual(final["final_outcome"], "healthy")

    def test_timed_out_dispatch_command_is_observed_on_later_tick_without_redispatch(self):
        class TimedOutRunner:
            def run(self, args, timeout, input_data=None):
                return type("Result", (), {
                    "returncode": 0,
                    "stdout": b"",
                    "stderr": b"",
                    "timed_out": True,
                })()

        class TimedOutDispatchGitHub(FakeGitHub):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                self.dispatch_adapter = GitHubAdapter("acme/epg", runner=TimedOutRunner())

            def dispatch_recovery(self, watchdog_id):
                self.dispatches.append(watchdog_id)
                recovery = run(
                    321,
                    event="workflow_dispatch",
                    created="2026-09-04T17:30:00Z",
                )
                recovery["display_title"] = "EPG watchdog recovery " + watchdog_id
                self.runs = [[recovery]]
                return self.dispatch_adapter.dispatch_recovery(watchdog_id)

        archive = (ROOT / "tests/fixtures/epg_watchdog/diagnostics-healthy.zip").read_bytes()
        with tempfile.TemporaryDirectory() as td:
            store = StateStore(pathlib.Path(td) / "state.json", pathlib.Path(td) / "watchdog.lock")
            failed = run(320, conclusion="failure")
            github = TimedOutDispatchGitHub([[failed]], [artifact_for(321)], archive)
            notifier = RecordingNotifier()
            controller = WatchdogController(
                repository="acme/epg", github=github, notifier=notifier, store=store,
                now=lambda: dt.datetime(2026, 9, 4, 18, 0, tzinfo=UTC),
            )

            first = controller.tick()
            first_record = store.load()["days"]["2026-09-04"]
            self.assertEqual(first.exit_code, 0)
            self.assertEqual(first_record["dispatch_api_result"], {"status": "uncertain"})
            self.assertFalse(first_record["terminal"])
            self.assertIn("may have been accepted", notifier.calls[0][1])

            second = controller.tick()

            self.assertEqual(second.exit_code, 0)
            self.assertEqual(len(github.dispatches), 1)
            self.assertEqual([target for target, _ in notifier.calls], ["ntfy:alerts", "ntfy:reports"])
            self.assertIn("EPG healthy", notifier.calls[1][1])
            self.assertEqual(store.load()["days"]["2026-09-04"]["final_outcome"], "healthy")

    def test_malformed_dispatch_success_body_is_observed_on_later_tick_without_redispatch(self):
        class MalformedDispatchGitHub(FakeGitHub):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                self.dispatch_adapter = GitHubAdapter(
                    "acme/epg",
                    runner=RecordingRunner(
                        {"workflow_run_id": "not-an-id", "workflow_run_url": ""},
                        http_status=200,
                    ),
                )

            def dispatch_recovery(self, watchdog_id):
                self.dispatches.append(watchdog_id)
                recovery = run(
                    323,
                    event="workflow_dispatch",
                    created="2026-09-04T17:30:00Z",
                )
                recovery["display_title"] = "EPG watchdog recovery " + watchdog_id
                self.runs = [[recovery]]
                return self.dispatch_adapter.dispatch_recovery(watchdog_id)

        archive = (ROOT / "tests/fixtures/epg_watchdog/diagnostics-healthy.zip").read_bytes()
        with tempfile.TemporaryDirectory() as td:
            store = StateStore(pathlib.Path(td) / "state.json", pathlib.Path(td) / "watchdog.lock")
            failed = run(322, conclusion="failure")
            github = MalformedDispatchGitHub([[failed]], [artifact_for(323)], archive)
            notifier = RecordingNotifier()
            controller = WatchdogController(
                repository="acme/epg", github=github, notifier=notifier, store=store,
                now=lambda: dt.datetime(2026, 9, 4, 18, 0, tzinfo=UTC),
            )

            first = controller.tick()
            first_record = store.load()["days"]["2026-09-04"]
            self.assertEqual(first.exit_code, 0)
            self.assertEqual(first_record["dispatch_api_result"], {"status": "uncertain"})
            self.assertFalse(first_record["terminal"])
            self.assertIn("dispatch-uncertain", notifier.calls[0][1])

            second = controller.tick()

            self.assertEqual(second.exit_code, 0)
            self.assertEqual(len(github.dispatches), 1)
            self.assertEqual([target for target, _ in notifier.calls], ["ntfy:alerts", "ntfy:reports"])
            self.assertIn("EPG healthy", notifier.calls[1][1])
            self.assertEqual(store.load()["days"]["2026-09-04"]["final_outcome"], "healthy")

    def test_uncertain_dispatch_notification_failure_preserves_reservation_and_complete_alert(self):
        class FailingDispatchGitHub(FakeGitHub):
            def dispatch_recovery(self, watchdog_id):
                self.dispatches.append(watchdog_id)
                raise GitHubError("stderr must not be persisted")

        with tempfile.TemporaryDirectory() as td:
            store = StateStore(pathlib.Path(td) / "state.json", pathlib.Path(td) / "watchdog.lock")
            failed = run(324, conclusion="failure")
            github = FailingDispatchGitHub([[failed]], [])
            notifier = RecordingNotifier(fail=True)
            controller = WatchdogController(
                repository="acme/epg", github=github, notifier=notifier, store=store,
                now=lambda: dt.datetime(2026, 9, 4, 18, 0, tzinfo=UTC),
            )

            result = controller.tick()

            self.assertEqual(result.exit_code, 1)
            day = store.load()["days"]["2026-09-04"]
            watchdog_id = github.dispatches[0]
            self.assertTrue(day["dispatch_attempted"])
            self.assertEqual(day["watchdog_id"], watchdog_id)
            self.assertEqual(day["dispatch_api_result"], {"status": "uncertain"})
            self.assertFalse(day["terminal"])
            pending = day["pending_messages"]["2026-09-04:dispatch-uncertain"]
            self.assertEqual(pending["target"], "ntfy:alerts")
            self.assertEqual(
                pending["body"],
                "event=2026-09-04:dispatch-uncertain day=2026-09-04 "
                "watchdog_id=%s request may have been accepted" % watchdog_id,
            )
            self.assertNotIn("stderr must not be persisted", pending["body"])

    def test_uncertain_dispatch_alert_does_not_close_observation_before_expiry(self):
        class FailingDispatchGitHub(FakeGitHub):
            def dispatch_recovery(self, watchdog_id):
                self.dispatches.append(watchdog_id)
                raise GitHubError("simulated dispatch failure")

        with tempfile.TemporaryDirectory() as td:
            store = StateStore(pathlib.Path(td) / "state.json", pathlib.Path(td) / "watchdog.lock")
            failed = run(325, conclusion="failure")
            github = FailingDispatchGitHub([[failed]], [])
            notifier = RecordingNotifier()
            controller = WatchdogController(
                repository="acme/epg", github=github, notifier=notifier,
                store=store, now=lambda: dt.datetime(2026, 9, 4, 18, 0, tzinfo=UTC),
            )

            first = controller.tick()
            self.assertEqual(first.exit_code, 0)
            self.assertEqual(len(github.dispatches), 1)
            self.assertFalse(store.load()["days"]["2026-09-04"]["terminal"])

            notifier.calls.clear()
            controller.clock = lambda: dt.datetime(2026, 9, 4, 20, 1, tzinfo=UTC)
            second = controller.tick()
            self.assertEqual(second.exit_code, 0)
            self.assertEqual(len(github.dispatches), 1)
            self.assertFalse(store.load()["days"]["2026-09-04"]["terminal"])
            self.assertIn("unbound-recovery", notifier.calls[0][1])

            notifier.calls.clear()
            state = store.load()
            newer = store.ensure_day(state, "2026-09-18")
            newer["terminal"] = True
            newer["final_outcome"] = "already-complete"
            store.save(state)
            store.finalize_day(state, "2026-09-18")
            controller.clock = lambda: dt.datetime(2026, 9, 18, 18, 0, tzinfo=UTC)
            third = controller.tick()
            self.assertEqual(third.exit_code, 0)
            self.assertEqual(len(github.dispatches), 1)
            self.assertIn("expired-unresolved", notifier.calls[0][1])
            self.assertTrue(store.load()["days"]["2026-09-04"]["tombstone"])

    def test_dispatch_failure_saves_nonterminal_uncertain_outcome(self):
        class FailingDispatchGitHub(FakeGitHub):
            def dispatch_recovery(self, watchdog_id):
                self.dispatches.append(watchdog_id)
                raise GitHubError("simulated dispatch failure")

        with tempfile.TemporaryDirectory() as td:
            store = StateStore(pathlib.Path(td) / "state.json", pathlib.Path(td) / "watchdog.lock")
            github = FailingDispatchGitHub([run(309, conclusion="failure")])
            notifier = RecordingNotifier()
            result = WatchdogController(
                repository="acme/epg", github=github, notifier=notifier, store=store,
                now=lambda: dt.datetime(2026, 9, 4, 17, 20, tzinfo=UTC),
            ).tick()

            self.assertEqual(result.exit_code, 0)
            self.assertEqual(len(notifier.calls), 1)
            self.assertIn("dispatch-uncertain", notifier.calls[0][1])
            day = store.load()["days"]["2026-09-04"]
            self.assertFalse(day["terminal"])
            self.assertFalse(day["tombstone"])
            self.assertEqual(day["final_outcome"], None)
            self.assertEqual(day["dispatch_api_result"], {"status": "uncertain"})

    def test_duplicate_tick_does_no_github_work_or_notification(self):
        with tempfile.TemporaryDirectory() as td:
            controller, store, github, notifier = self._controller(td, [])
            store.acquire()
            try:
                result = controller.tick()
            finally:
                store.release()
            self.assertEqual(result.exit_code, 0)
            self.assertTrue(result.duplicate)
            self.assertEqual(github.workflow_calls, 0)
            self.assertEqual(github.run_calls, [])
            self.assertEqual(notifier.calls, [])

    def test_corrupt_state_alerts_without_overwriting_or_querying_github(self):
        with tempfile.TemporaryDirectory() as td:
            controller, store, github, notifier = self._controller(td, [])
            pathlib.Path(store.path).write_text("{not-json", encoding="utf-8")
            result = controller.tick()
            self.assertEqual(result.exit_code, 0)
            self.assertEqual(github.workflow_calls, 0)
            self.assertEqual(github.run_calls, [])
            self.assertEqual(len(notifier.calls), 1)
            self.assertEqual(notifier.calls[0][0], "ntfy:alerts")
            self.assertEqual(notifier.calls[0][1], "event=state-error state=unreadable")
            self.assertEqual(pathlib.Path(store.path).read_text(encoding="utf-8"), "{not-json")

    def test_ntfy_error_leaves_terminal_message_for_the_next_tick(self):
        archive = (ROOT / "tests/fixtures/epg_watchdog/diagnostics-healthy.zip").read_bytes()
        with tempfile.TemporaryDirectory() as td:
            controller, store, github, failing = self._controller(
                td,
                [run(314)],
                notifier=RecordingNotifier(fail=True),
            )
            github.artifacts = [artifact_for(314)]
            github.archive = archive
            result = controller.tick()
            self.assertEqual(result.exit_code, 1)
            pending = store.load()["days"]["2026-09-04"]["pending_messages"]
            self.assertIn("2026-09-04:report", pending)
            self.assertEqual(len(failing.calls), 1)

            retry = RecordingNotifier()
            retry_controller = WatchdogController(
                repository="acme/epg",
                github=github,
                notifier=retry,
                store=store,
                now=lambda: dt.datetime(2026, 9, 4, 17, 21, tzinfo=UTC),
            )
            result = retry_controller.tick()
            self.assertEqual(result.exit_code, 0)
            self.assertEqual(len(retry.calls), 1)
            self.assertEqual(retry.calls[0][0], "ntfy:reports")
            self.assertTrue(store.load()["days"]["2026-09-04"]["tombstone"])

    def test_check_only_uses_temporary_state_and_emits_only_redacted_decision(self):
        with tempfile.TemporaryDirectory() as td:
            state_path = pathlib.Path(td) / "state.json"
            lock_path = pathlib.Path(td) / "watchdog.lock"
            github = FakeGitHub([run(101)])
            output = io.StringIO()
            with contextlib.redirect_stdout(output):
                code = main(
                    [
                        "--check-only",
                        "--repository", "acme/epg",
                        "--state-path", str(state_path),
                        "--lock-path", str(lock_path),
                    ],
                    github=github,
                    notifier=RecordingNotifier(),
                    now=lambda: dt.datetime(2026, 9, 4, 17, 20, tzinfo=UTC),
                )
            self.assertEqual(code, 0)
            self.assertEqual(json.loads(output.getvalue()), {
                "day": "2026-09-04", "kind": "healthy"
            })
            self.assertFalse(state_path.exists())
            self.assertFalse(lock_path.exists())
            self.assertEqual(github.dispatches, [])


    def test_missing_artifact_then_valid_artifact_retries_pinned_diagnostic_after_restart(self):
        archive = (ROOT / "tests/fixtures/epg_watchdog/diagnostics-healthy.zip").read_bytes()

        class MissingThenValidGitHub(FakeGitHub):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                self.artifact_calls = []

            def list_artifacts(self, run_id):
                self.artifact_calls.append(run_id)
                if len(self.artifact_calls) == 1:
                    return []
                return [artifact_for(run_id)]

        with tempfile.TemporaryDirectory() as td:
            store = StateStore(pathlib.Path(td) / "state.json", pathlib.Path(td) / "watchdog.lock")
            github = MissingThenValidGitHub([run(601)], archive=archive)
            notifier = RecordingNotifier()
            first = WatchdogController(
                repository="acme/epg", github=github, notifier=notifier, store=store,
                now=lambda: dt.datetime(2026, 9, 4, 18, 0, tzinfo=UTC),
            ).tick()
            pending = store.load()["days"]["2026-09-04"]
            self.assertEqual(first.exit_code, 0)
            self.assertFalse(pending["terminal"])
            self.assertEqual(pending["production_run_id"], 601)
            self.assertEqual(pending["production_run_attempt"], 1)
            self.assertEqual(pending["pending_diagnostic"]["run_id"], 601)
            self.assertIn("diagnostic-unavailable", notifier.calls[0][1])
            deadline = pending["pending_diagnostic"]["deadline_at"]

            second = WatchdogController(
                repository="acme/epg", github=github, notifier=notifier, store=store,
                now=lambda: dt.datetime(2026, 9, 4, 19, 0, tzinfo=UTC),
            ).tick()
            final = store.load()["days"]["2026-09-04"]
            self.assertEqual(second.exit_code, 0)
            self.assertEqual(github.artifact_calls, [601, 601])
            self.assertEqual(len(github.dispatches), 0)
            self.assertEqual([target for target, _ in notifier.calls], ["ntfy:alerts", "ntfy:reports"])
            self.assertEqual(final["final_outcome"], "healthy")
            self.assertTrue(final["tombstone"])
            self.assertEqual(deadline, "2026-09-18T18:00:00Z")

    def test_pending_diagnostic_observation_uses_time_after_workflow_reads(self):
        class WallClock:
            def __init__(self):
                self.value = dt.datetime(2026, 9, 4, 18, 0, tzinfo=UTC)

            def __call__(self):
                return self.value

        class AdvancingGitHub(FakeGitHub):
            def __init__(self, clock, *args, **kwargs):
                super().__init__(*args, **kwargs)
                self.clock = clock

            def list_runs(self, created_after):
                result = super().list_runs(created_after)
                self.clock.value = dt.datetime(2026, 9, 4, 19, 0, tzinfo=UTC)
                return result

        with tempfile.TemporaryDirectory() as td:
            store = StateStore(pathlib.Path(td) / "state.json", pathlib.Path(td) / "watchdog.lock")
            clock = WallClock()
            github = AdvancingGitHub(clock, [run(717)])
            result = WatchdogController(
                repository="acme/epg", github=github, notifier=RecordingNotifier(), store=store,
                now=clock, monotonic=lambda: 0.0,
            ).tick()
            pending = store.load()["days"]["2026-09-04"]["pending_diagnostic"]
            self.assertEqual(result.exit_code, 0)
            self.assertEqual(pending["first_observed_at"], "2026-09-04T19:00:00Z")
            self.assertEqual(pending["deadline_at"], "2026-09-18T19:00:00Z")

    def test_list_error_then_valid_artifact_keeps_pending_and_sends_one_temporary_alert(self):
        archive = (ROOT / "tests/fixtures/epg_watchdog/diagnostics-healthy.zip").read_bytes()

        class ListErrorThenValidGitHub(FakeGitHub):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                self.artifact_calls = 0

            def list_artifacts(self, run_id):
                self.artifact_calls += 1
                if self.artifact_calls == 1:
                    raise GitHubError("temporary list failure")
                return [artifact_for(run_id)]

        with tempfile.TemporaryDirectory() as td:
            store = StateStore(pathlib.Path(td) / "state.json", pathlib.Path(td) / "watchdog.lock")
            github = ListErrorThenValidGitHub([run(602)], archive=archive)
            notifier = RecordingNotifier()
            controller = WatchdogController(
                repository="acme/epg", github=github, notifier=notifier, store=store,
                now=lambda: dt.datetime(2026, 9, 4, 18, 0, tzinfo=UTC),
            )
            first = controller.tick()
            first_record = store.load()["days"]["2026-09-04"]
            self.assertEqual(first.exit_code, 0)
            self.assertFalse(first_record["terminal"])
            self.assertEqual(first_record["pending_diagnostic"]["run_id"], 602)
            self.assertEqual([target for target, _ in notifier.calls], ["ntfy:alerts"])

            second = controller.tick()
            final = store.load()["days"]["2026-09-04"]
            self.assertEqual(second.exit_code, 0)
            self.assertEqual(github.artifact_calls, 2)
            self.assertEqual([target for target, _ in notifier.calls], ["ntfy:alerts", "ntfy:reports"])
            self.assertTrue(final["tombstone"])
            self.assertEqual(final["final_outcome"], "healthy")

    def test_download_timeout_then_valid_artifact_retries_without_new_dispatch(self):
        archive = (ROOT / "tests/fixtures/epg_watchdog/diagnostics-healthy.zip").read_bytes()

        class DownloadTimeoutThenValidGitHub(FakeGitHub):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                self.download_calls = 0

            def download_artifact(self, artifact_id):
                self.download_calls += 1
                if self.download_calls == 1:
                    raise TickTimeoutError("download timed out")
                return super().download_artifact(artifact_id)

        with tempfile.TemporaryDirectory() as td:
            store = StateStore(pathlib.Path(td) / "state.json", pathlib.Path(td) / "watchdog.lock")
            github = DownloadTimeoutThenValidGitHub([run(603)], [artifact_for(603)], archive)
            notifier = RecordingNotifier()
            controller = WatchdogController(
                repository="acme/epg", github=github, notifier=notifier, store=store,
                now=lambda: dt.datetime(2026, 9, 4, 18, 0, tzinfo=UTC),
            )
            first = controller.tick()
            self.assertEqual(first.exit_code, 0)
            self.assertFalse(store.load()["days"]["2026-09-04"]["terminal"])
            self.assertIn("diagnostic-unavailable", notifier.calls[0][1])

            second = controller.tick()
            final = store.load()["days"]["2026-09-04"]
            self.assertEqual(second.exit_code, 0)
            self.assertEqual(github.download_calls, 2)
            self.assertEqual(github.dispatches, [])
            self.assertEqual(final["final_outcome"], "healthy")
            self.assertTrue(final["tombstone"])

    def test_expired_artifact_closes_diagnostic_observation_without_retry(self):
        artifact = artifact_for(604)
        artifact["expired"] = True
        with tempfile.TemporaryDirectory() as td:
            store = StateStore(pathlib.Path(td) / "state.json", pathlib.Path(td) / "watchdog.lock")
            github = FakeGitHub([run(604)], [artifact], b"unsafe")
            notifier = RecordingNotifier()
            controller = WatchdogController(
                repository="acme/epg", github=github, notifier=notifier, store=store,
                now=lambda: dt.datetime(2026, 9, 4, 18, 0, tzinfo=UTC),
            )
            first = controller.tick()
            record = store.load()["days"]["2026-09-04"]
            self.assertEqual(first.exit_code, 0)
            self.assertEqual(record["final_outcome"], "diagnostic-expired")
            self.assertTrue(record["tombstone"])
            self.assertIn("diagnostic-expired", notifier.calls[0][1])
            workflow_calls = github.workflow_calls
            second = controller.tick()
            self.assertEqual(second.exit_code, 0)
            self.assertEqual(github.workflow_calls, workflow_calls)

    def test_pending_deadline_expires_locally_before_github_is_available(self):
        with tempfile.TemporaryDirectory() as td:
            store = StateStore(pathlib.Path(td) / "state.json", pathlib.Path(td) / "watchdog.lock")
            state = store.load()
            record = store.ensure_day(state, "2026-09-04")
            record["production_run_id"] = 605
            record["production_run_attempt"] = 3
            record["pending_diagnostic"] = pending_for(
                605, 3, "2026-09-04T18:00:00Z", "2026-09-05T18:00:00Z"
            )
            due = store.ensure_day(state, "2026-09-05")
            due["terminal"] = True
            due["final_outcome"] = "already-complete"
            store.save(state)
            github = FakeGitHub([])
            github.workflow_error = True
            notifier = RecordingNotifier()
            result = WatchdogController(
                repository="acme/epg", github=github, notifier=notifier, store=store,
                now=lambda: dt.datetime(2026, 9, 5, 18, 0, tzinfo=UTC),
            ).tick()
            final = store.load()["days"]["2026-09-04"]
            self.assertEqual(result.exit_code, 0)
            self.assertEqual(github.workflow_calls, 0)
            self.assertEqual(github.run_calls, [])
            self.assertEqual(final["final_outcome"], "diagnostic-expired")
            self.assertTrue(final["tombstone"])
            self.assertIn("diagnostic-expired", notifier.calls[0][1])

    def test_notification_crossing_pending_deadline_expires_without_artifact_read(self):
        archive = (ROOT / "tests/fixtures/epg_watchdog/diagnostics-healthy.zip").read_bytes()

        class WallClock:
            def __init__(self):
                self.value = dt.datetime(2026, 9, 4, 18, 0, tzinfo=UTC)

            def __call__(self):
                return self.value

        class AdvancingNotifier(RecordingNotifier):
            def __init__(self, clock):
                super().__init__()
                self.clock = clock

            def send(self, target, message):
                result = super().send(target, message)
                self.clock.value = dt.datetime(2026, 9, 4, 18, 0, 2, tzinfo=UTC)
                return result

        class TrackingGitHub(FakeGitHub):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                self.artifact_calls = []
                self.download_calls = []

            def list_artifacts(self, run_id):
                self.artifact_calls.append(run_id)
                return super().list_artifacts(run_id)

            def download_artifact(self, artifact_id):
                self.download_calls.append(artifact_id)
                return super().download_artifact(artifact_id)

        with tempfile.TemporaryDirectory() as td:
            store = StateStore(pathlib.Path(td) / "state.json", pathlib.Path(td) / "watchdog.lock")
            state = store.load()
            record = store.ensure_day(state, "2026-09-04")
            record["production_run_id"] = 712
            record["production_run_attempt"] = 1
            record["pending_diagnostic"] = pending_for(
                712, 1, "2026-09-04T18:00:00Z", "2026-09-04T18:00:01Z"
            )
            store.queue_message(
                state, "2026-09-04", "2026-09-04:existing",
                "ntfy:alerts", "existing message",
            )
            github = TrackingGitHub([], [artifact_for(712)], archive)
            clock = WallClock()
            notifier = AdvancingNotifier(clock)
            result = WatchdogController(
                repository="acme/epg", github=github, notifier=notifier, store=store,
                now=clock, monotonic=lambda: 0.0,
            ).tick()
            final = store.load()["days"]["2026-09-04"]
            self.assertEqual(result.exit_code, 0)
            self.assertEqual(github.artifact_calls, [])
            self.assertEqual(github.download_calls, [])
            self.assertTrue(final["tombstone"])
            self.assertEqual(final["final_outcome"], "diagnostic-expired")

    def test_failed_notification_retains_complete_expiry_alert_until_next_tick(self):
        with tempfile.TemporaryDirectory() as td:
            store = StateStore(pathlib.Path(td) / "state.json", pathlib.Path(td) / "watchdog.lock")
            state = store.load()
            record = store.ensure_day(state, "2026-09-04")
            record["production_run_id"] = 606
            record["production_run_attempt"] = 1
            record["pending_diagnostic"] = pending_for(
                606, 1, "2026-09-04T18:00:00Z", "2026-09-05T18:00:00Z"
            )
            due = store.ensure_day(state, "2026-09-05")
            due["terminal"] = True
            due["final_outcome"] = "already-complete"
            later_due = store.ensure_day(state, "2026-09-06")
            later_due["terminal"] = True
            later_due["final_outcome"] = "already-complete"
            store.save(state)
            github = FakeGitHub([])
            failing = RecordingNotifier(fail=True)
            first = WatchdogController(
                repository="acme/epg", github=github, notifier=failing, store=store,
                now=lambda: dt.datetime(2026, 9, 5, 18, 0, tzinfo=UTC),
            ).tick()
            pending = store.load()["days"]["2026-09-04"]["pending_messages"]
            self.assertEqual(first.exit_code, 1)
            self.assertIn("2026-09-04:diagnostic-expired", pending)
            body = pending["2026-09-04:diagnostic-expired"]["body"]

            retry = RecordingNotifier()
            second = WatchdogController(
                repository="acme/epg", github=github, notifier=retry, store=store,
                now=lambda: dt.datetime(2026, 9, 6, 18, 0, tzinfo=UTC),
            ).tick()
            final = store.load()["days"]["2026-09-04"]
            self.assertEqual(second.exit_code, 0)
            self.assertEqual(retry.calls, [("ntfy:alerts", body)])
            self.assertTrue(final["tombstone"])
            self.assertEqual(final["final_outcome"], "diagnostic-expired")

    def test_two_pending_days_process_newest_first_without_erasing_older_pending_day(self):
        archive = (ROOT / "tests/fixtures/epg_watchdog/diagnostics-healthy.zip").read_bytes()

        class TwoPendingGitHub(FakeGitHub):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                self.artifact_calls = []
                self.valid_ids = {602}

            def list_artifacts(self, run_id):
                self.artifact_calls.append(run_id)
                if run_id in self.valid_ids:
                    return [artifact_for(run_id)]
                return []

        with tempfile.TemporaryDirectory() as td:
            store = StateStore(pathlib.Path(td) / "state.json", pathlib.Path(td) / "watchdog.lock")
            state = store.load()
            older = store.ensure_day(state, "2026-09-03")
            older["production_run_id"] = 601
            older["production_run_attempt"] = 1
            older["pending_diagnostic"] = pending_for(
                601, 1, "2026-09-03T18:00:00Z", "2026-09-17T18:00:00Z"
            )
            newer = store.ensure_day(state, "2026-09-04")
            newer["production_run_id"] = 602
            newer["production_run_attempt"] = 1
            newer["pending_diagnostic"] = pending_for(
                602, 1, "2026-09-04T18:00:00Z", "2026-09-18T18:00:00Z"
            )
            store.save(state)
            github = TwoPendingGitHub([])
            notifier = RecordingNotifier()
            result = WatchdogController(
                repository="acme/epg", github=github, notifier=notifier, store=store,
                now=lambda: dt.datetime(2026, 9, 4, 19, 0, tzinfo=UTC),
            ).tick()
            loaded = store.load()["days"]
            self.assertEqual(result.exit_code, 0)
            self.assertEqual(github.artifact_calls, [602, 601])
            self.assertTrue(loaded["2026-09-04"]["tombstone"])
            self.assertFalse(loaded["2026-09-03"]["terminal"])
            self.assertIsNotNone(loaded["2026-09-03"]["pending_diagnostic"])
            self.assertEqual(github.dispatches, [])


    def test_final_notification_timeout_is_unsuccessful_and_keeps_message(self):
        class TimedOutUnderlying:
            def __init__(self):
                self.calls = []

            def run(self, args, timeout, input_data=None):
                self.calls.append((args, timeout, input_data))
                return type("Result", (), {
                    "returncode": 0,
                    "stdout": b"",
                    "stderr": b"",
                    "timed_out": True,
                })()

        with tempfile.TemporaryDirectory() as td:
            store = StateStore(pathlib.Path(td) / "state.json", pathlib.Path(td) / "watchdog.lock")
            state = store.load()
            store.ensure_day(state, "2026-09-04")
            store.queue_message(state, "2026-09-04", "2026-09-04:alert", "ntfy:alerts", "complete body")
            underlying = TimedOutUnderlying()
            notifier = HermesNotifier(runner=underlying)
            clock = iter((0.0, 240.0))
            controller = WatchdogController(
                repository="acme/epg", github=FakeGitHub([]), notifier=notifier, store=store,
                monotonic=lambda: next(clock),
            )
            controller._begin_budget()
            controller._normal_network_stopped = True
            self.assertFalse(controller._deliver(state))
            self.assertEqual(len(underlying.calls), 1)
            self.assertEqual(underlying.calls[0][1], 30.0)
            self.assertIn("2026-09-04:alert", state["days"]["2026-09-04"]["pending_messages"])
            controller._clear_budget()


    def test_pending_diagnostic_admission_caps_each_tick_at_two_attempts(self):
        archive = (ROOT / "tests/fixtures/epg_watchdog/diagnostics-healthy.zip").read_bytes()

        class TrackingGitHub(FakeGitHub):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                self.artifact_calls = []

            def list_artifacts(self, run_id):
                self.artifact_calls.append(run_id)
                return [artifact_for(run_id)]

        with tempfile.TemporaryDirectory() as td:
            store = StateStore(pathlib.Path(td) / "state.json", pathlib.Path(td) / "watchdog.lock")
            state = store.load()
            for day, run_id in (("2026-09-02", 701), ("2026-09-03", 702), ("2026-09-04", 703)):
                record = store.ensure_day(state, day)
                record["production_run_id"] = run_id
                record["production_run_attempt"] = 1
                record["pending_diagnostic"] = pending_for(
                    run_id, 1, "%sT18:00:00Z" % day,
                    "%sT18:00:00Z" % (dt.date.fromisoformat(day) + dt.timedelta(days=14))
                )
            store.save(state)
            github = TrackingGitHub([], archive=archive)
            result = WatchdogController(
                repository="acme/epg", github=github, notifier=RecordingNotifier(), store=store,
                now=lambda: dt.datetime(2026, 9, 4, 19, 0, tzinfo=UTC),
                monotonic=lambda: 0.0,
            ).tick()
            loaded = store.load()["days"]
            self.assertEqual(result.exit_code, 0)
            self.assertEqual(github.artifact_calls, [703, 702])
            self.assertIsNotNone(loaded["2026-09-02"]["pending_diagnostic"])

    def test_pending_diagnostic_is_deferred_when_less_than_one_hundred_seconds_remain(self):
        class FakeClock:
            def __init__(self):
                self.calls = 0

            def __call__(self):
                self.calls += 1
                return 140.1 if self.calls >= 4 else 0.0

        class TrackingGitHub(FakeGitHub):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                self.artifact_calls = []

            def list_artifacts(self, run_id):
                self.artifact_calls.append(run_id)
                return [artifact_for(run_id)]

        with tempfile.TemporaryDirectory() as td:
            store = StateStore(pathlib.Path(td) / "state.json", pathlib.Path(td) / "watchdog.lock")
            state = store.load()
            record = store.ensure_day(state, "2026-09-04")
            record["production_run_id"] = 704
            record["production_run_attempt"] = 1
            record["pending_diagnostic"] = pending_for(704, 1)
            store.save(state)
            clock = FakeClock()
            github = TrackingGitHub([])
            result = WatchdogController(
                repository="acme/epg", github=github, notifier=RecordingNotifier(), store=store,
                now=lambda: dt.datetime(2026, 9, 4, 19, 0, tzinfo=UTC),
                monotonic=clock,
            ).tick()
            self.assertEqual(result.exit_code, 0)
            self.assertEqual(github.artifact_calls, [])
            self.assertIsNotNone(store.load()["days"]["2026-09-04"]["pending_diagnostic"])

    def test_recovery_missing_artifact_then_valid_attempt_two_is_observed_without_redispatch(self):
        archive = (ROOT / "tests/fixtures/epg_watchdog/diagnostics-healthy.zip").read_bytes()

        class RecoveryGitHub(FakeGitHub):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                self.artifact_calls = []

            def dispatch_recovery(self, watchdog_id):
                self.dispatches.append(watchdog_id)
                recovery = run(
                    705, event="workflow_dispatch", run_attempt=2,
                    created="2026-09-04T17:30:00Z",
                )
                recovery["display_title"] = "EPG watchdog recovery " + watchdog_id
                self.runs = [[recovery]]
                return type("Dispatch", (), {
                    "http_status": 204, "workflow_run_id": None, "workflow_run_url": None,
                })()

            def list_artifacts(self, run_id):
                self.artifact_calls.append(run_id)
                if len(self.artifact_calls) == 1:
                    return []
                return [artifact_for(run_id, 2)]

        with tempfile.TemporaryDirectory() as td:
            store = StateStore(pathlib.Path(td) / "state.json", pathlib.Path(td) / "watchdog.lock")
            failed = run(706, conclusion="failure")
            github = RecoveryGitHub([[failed], [failed]], archive=archive)
            notifier = RecordingNotifier()
            first = WatchdogController(
                repository="acme/epg", github=github, notifier=notifier, store=store,
                now=lambda: dt.datetime(2026, 9, 4, 18, 0, tzinfo=UTC),
                monotonic=lambda: 0.0,
            ).tick()
            self.assertEqual(first.exit_code, 0)
            self.assertEqual(len(github.dispatches), 1)
            self.assertFalse(store.load()["days"]["2026-09-04"]["terminal"])

            second = WatchdogController(
                repository="acme/epg", github=github, notifier=notifier, store=store,
                now=lambda: dt.datetime(2026, 9, 4, 19, 0, tzinfo=UTC),
                monotonic=lambda: 0.0,
            ).tick()
            pending = store.load()["days"]["2026-09-04"]
            self.assertEqual(second.exit_code, 0)
            self.assertEqual(github.artifact_calls, [705])
            self.assertEqual(pending["production_run_id"], 705)
            self.assertEqual(pending["production_run_attempt"], 2)
            self.assertIsNotNone(pending["pending_diagnostic"])

            third = WatchdogController(
                repository="acme/epg", github=github, notifier=notifier, store=store,
                now=lambda: dt.datetime(2026, 9, 4, 20, 0, tzinfo=UTC),
                monotonic=lambda: 0.0,
            ).tick()
            final = store.load()["days"]["2026-09-04"]
            self.assertEqual(third.exit_code, 0)
            self.assertEqual(github.artifact_calls, [705, 705])
            self.assertEqual(len(github.dispatches), 1)
            self.assertTrue(final["tombstone"])
            self.assertEqual(final["final_outcome"], "healthy")

    def test_degraded_pending_diagnostic_sends_report_and_exact_degraded_alert(self):
        archive = (ROOT / "tests/fixtures/epg_watchdog/diagnostics-degraded.zip").read_bytes()
        with tempfile.TemporaryDirectory() as td:
            store = StateStore(pathlib.Path(td) / "state.json", pathlib.Path(td) / "watchdog.lock")
            github = FakeGitHub([run(707)], [artifact_for(707)], archive)
            notifier = RecordingNotifier()
            result = WatchdogController(
                repository="acme/epg", github=github, notifier=notifier, store=store,
                now=lambda: dt.datetime(2026, 9, 4, 18, 0, tzinfo=UTC),
                monotonic=lambda: 0.0,
            ).tick()
            final = store.load()["days"]["2026-09-04"]
            self.assertEqual(result.exit_code, 0)
            self.assertEqual(
                sorted(target for target, _ in notifier.calls),
                ["ntfy:alerts", "ntfy:reports"],
            )
            self.assertIn("production passed", " ".join(message for _, message in notifier.calls))
            self.assertIn("alpha=0", " ".join(message for _, message in notifier.calls))
            self.assertTrue(final["tombstone"])
            self.assertEqual(final["final_outcome"], "degraded")

    def test_artifact_expiry_shortens_pending_deadline_before_download(self):
        archive = (ROOT / "tests/fixtures/epg_watchdog/diagnostics-healthy.zip").read_bytes()
        artifact = dict(artifact_for(708), expires_at="2026-09-10T12:00:00Z")

        class DeadlineCheckingGitHub(FakeGitHub):
            def __init__(self, *args, **kwargs):
                super().__init__(*args, **kwargs)
                self.deadline_at_download = None
                self.store = None

            def download_artifact(self, artifact_id):
                self.deadline_at_download = self.store.load()["days"]["2026-09-04"]["pending_diagnostic"]["deadline_at"]
                return super().download_artifact(artifact_id)

        with tempfile.TemporaryDirectory() as td:
            store = StateStore(pathlib.Path(td) / "state.json", pathlib.Path(td) / "watchdog.lock")
            github = DeadlineCheckingGitHub([run(708)], [artifact], archive)
            github.store = store
            result = WatchdogController(
                repository="acme/epg", github=github, notifier=RecordingNotifier(), store=store,
                now=lambda: dt.datetime(2026, 9, 4, 18, 0, tzinfo=UTC),
                monotonic=lambda: 0.0,
            ).tick()
            self.assertEqual(result.exit_code, 0)
            self.assertEqual(github.deadline_at_download, "2026-09-10T12:00:00Z")

    def test_pending_deadline_save_failure_returns_nonzero_without_remote_alert(self):
        archive = (ROOT / "tests/fixtures/epg_watchdog/diagnostics-healthy.zip").read_bytes()
        artifact = dict(artifact_for(718), expires_at="2026-09-10T12:00:00Z")

        class FailingDeadlineStore(StateStore):
            fail_deadline_save = False

            def save(self, state):
                pending = state.get("days", {}).get("2026-09-04", {}).get("pending_diagnostic")
                if (
                    self.fail_deadline_save
                    and pending is not None
                    and pending.get("deadline_at") == "2026-09-10T12:00:00Z"
                ):
                    self.fail_deadline_save = False
                    raise OSError("simulated state write failure")
                return super().save(state)

        with tempfile.TemporaryDirectory() as td:
            store = FailingDeadlineStore(
                pathlib.Path(td) / "state.json", pathlib.Path(td) / "watchdog.lock"
            )
            state = store.load()
            record = store.ensure_day(state, "2026-09-04")
            record["production_run_id"] = 718
            record["production_run_attempt"] = 1
            record["pending_diagnostic"] = pending_for(718, 1)
            store.save(state)
            store.fail_deadline_save = True
            notifier = RecordingNotifier()
            result = WatchdogController(
                repository="acme/epg",
                github=FakeGitHub([], [artifact], archive),
                notifier=notifier,
                store=store,
                now=lambda: dt.datetime(2026, 9, 4, 18, 0, tzinfo=UTC),
                monotonic=lambda: 0.0,
            ).tick()
            persisted = store.load()["days"]["2026-09-04"]
            self.assertEqual(result.exit_code, 1)
            self.assertEqual(notifier.calls, [])
            self.assertEqual(
                persisted["pending_diagnostic"]["deadline_at"],
                "2026-09-18T18:00:00Z",
            )
            self.assertNotIn(
                "2026-09-04:diagnostic-unavailable", persisted["alerts_sent"]
            )

    def test_artifact_expiry_crossed_after_list_prevents_download(self):
        archive = (ROOT / "tests/fixtures/epg_watchdog/diagnostics-healthy.zip").read_bytes()
        artifact = dict(artifact_for(713), expires_at="2026-09-04T18:00:01Z")

        class WallClock:
            def __init__(self):
                self.value = dt.datetime(2026, 9, 4, 18, 0, tzinfo=UTC)

            def __call__(self):
                return self.value

        class AdvancingGitHub(FakeGitHub):
            def __init__(self, clock, *args, **kwargs):
                super().__init__(*args, **kwargs)
                self.clock = clock
                self.artifact_calls = []
                self.download_calls = []

            def list_artifacts(self, run_id):
                self.artifact_calls.append(run_id)
                self.clock.value = dt.datetime(2026, 9, 4, 18, 0, 2, tzinfo=UTC)
                return super().list_artifacts(run_id)

            def download_artifact(self, artifact_id):
                self.download_calls.append(artifact_id)
                return super().download_artifact(artifact_id)

        with tempfile.TemporaryDirectory() as td:
            store = StateStore(pathlib.Path(td) / "state.json", pathlib.Path(td) / "watchdog.lock")
            state = store.load()
            record = store.ensure_day(state, "2026-09-04")
            record["production_run_id"] = 713
            record["production_run_attempt"] = 1
            record["pending_diagnostic"] = pending_for(713, 1)
            store.save(state)
            clock = WallClock()
            github = AdvancingGitHub(clock, [], [artifact], archive)
            result = WatchdogController(
                repository="acme/epg", github=github, notifier=RecordingNotifier(), store=store,
                now=clock, monotonic=lambda: 0.0,
            ).tick()
            final = store.load()["days"]["2026-09-04"]
            self.assertEqual(result.exit_code, 0)
            self.assertEqual(github.artifact_calls, [713])
            self.assertEqual(github.download_calls, [])
            self.assertTrue(final["tombstone"])
            self.assertEqual(final["final_outcome"], "diagnostic-expired")

    def test_artifact_expiry_crossed_during_download_expires_before_parse(self):
        archive = (ROOT / "tests/fixtures/epg_watchdog/diagnostics-healthy.zip").read_bytes()
        artifact = dict(artifact_for(716), expires_at="2026-09-04T18:00:01Z")

        class WallClock:
            def __init__(self):
                self.value = dt.datetime(2026, 9, 4, 18, 0, tzinfo=UTC)

            def __call__(self):
                return self.value

        class AdvancingGitHub(FakeGitHub):
            def __init__(self, clock, *args, **kwargs):
                super().__init__(*args, **kwargs)
                self.clock = clock
                self.download_calls = []

            def download_artifact(self, artifact_id):
                self.download_calls.append(artifact_id)
                self.clock.value = dt.datetime(2026, 9, 4, 18, 0, 2, tzinfo=UTC)
                return super().download_artifact(artifact_id)

        with tempfile.TemporaryDirectory() as td:
            store = StateStore(pathlib.Path(td) / "state.json", pathlib.Path(td) / "watchdog.lock")
            state = store.load()
            record = store.ensure_day(state, "2026-09-04")
            record["production_run_id"] = 716
            record["production_run_attempt"] = 1
            record["pending_diagnostic"] = pending_for(716, 1)
            store.save(state)
            clock = WallClock()
            github = AdvancingGitHub(clock, [], [artifact], archive)
            result = WatchdogController(
                repository="acme/epg", github=github, notifier=RecordingNotifier(), store=store,
                now=clock, monotonic=lambda: 0.0,
            ).tick()
            final = store.load()["days"]["2026-09-04"]
            self.assertEqual(result.exit_code, 0)
            self.assertEqual(github.download_calls, [88])
            self.assertTrue(final["tombstone"])
            self.assertEqual(final["final_outcome"], "diagnostic-expired")

    def test_malformed_artifact_list_is_retryable_not_terminal(self):
        class MalformedArtifactListGitHub(FakeGitHub):
            def list_artifacts(self, run_id):
                return {"artifacts": "not-a-list", "total_count": 1}

        with tempfile.TemporaryDirectory() as td:
            store = StateStore(pathlib.Path(td) / "state.json", pathlib.Path(td) / "watchdog.lock")
            github = MalformedArtifactListGitHub([run(709)])
            result = WatchdogController(
                repository="acme/epg", github=github, notifier=RecordingNotifier(), store=store,
                now=lambda: dt.datetime(2026, 9, 4, 18, 0, tzinfo=UTC),
                monotonic=lambda: 0.0,
            ).tick()
            record = store.load()["days"]["2026-09-04"]
            self.assertEqual(result.exit_code, 0)
            self.assertFalse(record["terminal"])
            self.assertIn("2026-09-04:diagnostic-unavailable", record["alerts_sent"])
            self.assertIsNotNone(record["pending_diagnostic"])

    def test_workflow_list_budget_is_shared_and_exhaustion_prevents_dispatch(self):
        class AdvancingGitHub(FakeGitHub):
            def __init__(self, clock, *args, **kwargs):
                super().__init__(*args, **kwargs)
                self.clock = clock

            def workflow_state(self):
                result = super().workflow_state()
                self.clock.value = 239.0
                return result

            def list_runs(self, created_after):
                self.clock.value = 241.0
                return super().list_runs(created_after)

        class Clock:
            value = 0.0

            def __call__(self):
                return self.value

        with tempfile.TemporaryDirectory() as td:
            store = StateStore(pathlib.Path(td) / "state.json", pathlib.Path(td) / "watchdog.lock")
            clock = Clock()
            github = AdvancingGitHub(clock, [run(710, conclusion="failure"), run(710, conclusion="failure")])
            notifier = RecordingNotifier()
            result = WatchdogController(
                repository="acme/epg", github=github, notifier=notifier, store=store,
                now=lambda: dt.datetime(2026, 9, 4, 18, 0, tzinfo=UTC),
                monotonic=clock,
            ).tick()
            self.assertEqual(result.exit_code, 0)
            self.assertEqual(github.workflow_calls, 1)
            self.assertEqual(github.run_calls, ["2026-09-04T04:17:00Z"])
            self.assertEqual(github.dispatches, [])
            self.assertIn("dependency-error", notifier.calls[0][1])

    def test_budget_expiring_before_dispatch_reservation_does_not_reserve(self):
        class ExpiringRuns(list):
            def __iter__(self):
                clock.value = 240.0
                return super().__iter__()

        class Clock:
            value = 0.0

            def __call__(self):
                return self.value

        with tempfile.TemporaryDirectory() as td:
            store = StateStore(pathlib.Path(td) / "state.json", pathlib.Path(td) / "watchdog.lock")
            clock = Clock()
            github = FakeGitHub([
                [run(711, conclusion="failure")],
                ExpiringRuns([run(711, conclusion="failure")]),
            ])
            notifier = RecordingNotifier()
            result = WatchdogController(
                repository="acme/epg", github=github, notifier=notifier, store=store,
                now=lambda: dt.datetime(2026, 9, 4, 18, 0, tzinfo=UTC),
                monotonic=clock,
            ).tick()

            record = store.load()["days"]["2026-09-04"]
            self.assertEqual(result.exit_code, 0)
            self.assertEqual(github.dispatches, [])
            self.assertFalse(record["dispatch_attempted"])
            self.assertIsNone(record["dispatch_requested_at"])
            self.assertIsNone(record["watchdog_id"])
            self.assertIsNone(record["dispatch_api_result"])

    def test_tick_budget_phase_limits_sum_and_deadlines_share_tick_start(self):
        self.assertEqual(
            NORMAL_WORK_SECONDS + FINAL_NOTIFICATION_SECONDS
            + PROCESS_CLEANUP_SECONDS + LOCAL_HEADROOM_SECONDS,
            TICK_LIMIT_SECONDS,
        )
        with tempfile.TemporaryDirectory() as td:
            store = StateStore(pathlib.Path(td) / "state.json", pathlib.Path(td) / "watchdog.lock")
            controller = WatchdogController(
                repository="acme/epg", github=FakeGitHub([]),
                notifier=RecordingNotifier(), store=store, monotonic=lambda: 10.0,
            )
            controller._begin_budget()
            self.assertEqual(controller._tick_deadline, 310.0)
            self.assertEqual(controller._normal_deadline, 250.0)
            self.assertEqual(controller._final_deadline, 280.0)
            controller._clear_budget()


if __name__ == "__main__":
    unittest.main()
