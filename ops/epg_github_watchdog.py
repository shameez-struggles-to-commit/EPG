"""Deterministic GitHub run watchdog."""

import argparse
import datetime as _dt
import fcntl
import io
import json
import os
import re
import secrets
import sys
import tempfile
import time
import zipfile
import zlib
from dataclasses import dataclass
from urllib.parse import urlencode


UTC = _dt.timezone.utc
ACTIVE_STATUSES = frozenset({
    "queued", "requested", "pending", "waiting", "in_progress",
})
TERMINAL_CONCLUSIONS = frozenset({
    "failure", "cancelled", "timed_out", "action_required", "neutral",
    "skipped", "stale", "success",
})


class WatchdogSchemaError(ValueError):
    """A dependency returned a state outside the decision contract."""


class DuplicateRecoveryIdentity(WatchdogSchemaError):
    """More than one exact watchdog recovery run matched."""


class StateError(ValueError):
    """The durable state cannot be trusted."""


class DuplicateTick(RuntimeError):
    """Another watchdog tick owns the lock."""


STATE_SCHEMA_VERSION = 2
DAY_FIELDS = frozenset({
    "scheduled_day", "scheduled_run_id", "scheduled_conclusion",
    "dispatch_attempted", "dispatch_requested_at", "dispatch_api_result",
    "watchdog_id", "recovery_run_id", "recovery_status", "production_run_id",
    "production_run_attempt", "report_run_id", "alerts_sent", "pending_messages",
    "pending_diagnostic", "terminal", "final_outcome", "tombstone",
})
TOMBSTONE_FIELDS = frozenset({
    "scheduled_day", "dispatch_attempted", "watchdog_id", "final_outcome",
    "alerts_sent", "tombstone",
})


def new_state():
    return {"schema_version": STATE_SCHEMA_VERSION, "days": {}}


def _valid_optional_id(value):
    return value is None or (
        isinstance(value, int) and not isinstance(value, bool) and value > 0
    )


def _valid_dispatch_result(value):
    if value is None:
        return True
    if not isinstance(value, dict):
        return False
    if value.get("status") == "uncertain":
        return set(value) == {"status"}
    if value.get("status") != "accepted":
        return False
    http_status = value.get("http_status")
    if http_status == 204:
        return set(value) == {"status", "http_status"}
    if http_status == 200:
        return (
            set(value) == {"status", "http_status", "workflow_run_id"}
            and _valid_optional_id(value.get("workflow_run_id"))
            and value.get("workflow_run_id") is not None
        )
    return False


def _valid_recovery_id(value):
    return isinstance(value, str) and RECOVERY_ID_RE.fullmatch(value) is not None


def _parse_pending_time(value):
    if not isinstance(value, str) or re.fullmatch(
        r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", value
    ) is None:
        raise ValueError("pending timestamp has invalid format")
    try:
        return _dt.datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=UTC)
    except (TypeError, ValueError) as exc:
        raise ValueError("pending timestamp is not a real UTC time") from exc


def _validate_state(state):
    if not isinstance(state, dict) or state.get("schema_version") != STATE_SCHEMA_VERSION:
        raise StateError("unknown state schema")
    if set(state) != {"schema_version", "days"} or not isinstance(state.get("days"), dict):
        raise StateError("invalid state days")
    for day, record in state["days"].items():
        try:
            parsed_day = _dt.date.fromisoformat(day)
        except (TypeError, ValueError) as exc:
            raise StateError("invalid state day") from exc
        if parsed_day.isoformat() != day or not isinstance(record, dict):
            raise StateError("invalid day record")
        tombstone = record.get("tombstone")
        if tombstone is True:
            if set(record) != TOMBSTONE_FIELDS:
                raise StateError("invalid tombstone fields")
            if record["scheduled_day"] != day or not isinstance(record["dispatch_attempted"], bool):
                raise StateError("invalid tombstone identity")
            if record["dispatch_attempted"]:
                if not _valid_recovery_id(record["watchdog_id"]):
                    raise StateError("invalid tombstone recovery identity")
            elif record["watchdog_id"] is not None:
                raise StateError("unexpected tombstone recovery identity")
            if not isinstance(record["final_outcome"], str) or not record["final_outcome"]:
                raise StateError("invalid tombstone outcome")
            if not isinstance(record["alerts_sent"], dict) or not all(
                isinstance(key, str) and value is True
                for key, value in record["alerts_sent"].items()
            ):
                raise StateError("invalid tombstone alerts")
            continue
        if tombstone is not False or set(record) != DAY_FIELDS:
            raise StateError("invalid day fields")
        if record["scheduled_day"] != day:
            raise StateError("scheduled day mismatch")
        if not isinstance(record["dispatch_attempted"], bool) or not isinstance(record["terminal"], bool):
            raise StateError("invalid day flags")
        if record["dispatch_attempted"]:
            if (
                not isinstance(record["dispatch_requested_at"], str)
                or not _valid_recovery_id(record["watchdog_id"])
            ):
                raise StateError("incomplete dispatch reservation")
        else:
            if any(record[field] is not None for field in (
                "dispatch_requested_at", "dispatch_api_result", "watchdog_id",
                "recovery_run_id", "recovery_status",
            )):
                raise StateError("dispatch fields without attempt")
        for field in ("scheduled_run_id", "recovery_run_id", "production_run_id", "production_run_attempt", "report_run_id"):
            if not _valid_optional_id(record[field]):
                raise StateError("invalid day run id")
        if (record["production_run_id"] is None) != (record["production_run_attempt"] is None):
            raise StateError("incomplete production identity")
        if record["scheduled_conclusion"] is not None and not isinstance(record["scheduled_conclusion"], str):
            raise StateError("invalid scheduled conclusion")
        if record["dispatch_requested_at"] is not None:
            try:
                _parse_time(record["dispatch_requested_at"])
            except (TypeError, ValueError) as exc:
                raise StateError("invalid dispatch request time") from exc
        if not _valid_dispatch_result(record["dispatch_api_result"]):
            raise StateError("invalid dispatch result")
        if record["watchdog_id"] is not None and not _valid_recovery_id(record["watchdog_id"]):
            raise StateError("invalid recovery identity")
        if record["recovery_status"] is not None and not isinstance(record["recovery_status"], str):
            raise StateError("invalid recovery status")
        if record["final_outcome"] is not None and not isinstance(record["final_outcome"], str):
            raise StateError("invalid final outcome")
        if record["terminal"] and not record["final_outcome"]:
            raise StateError("terminal day has no outcome")
        pending = record["pending_diagnostic"]
        if pending is not None:
            if not isinstance(pending, dict) or set(pending) != {"run_id", "run_attempt", "first_observed_at", "deadline_at"}:
                raise StateError("invalid pending diagnostic fields")
            if (
                not _valid_optional_id(pending["run_id"])
                or pending["run_id"] is None
                or not _valid_optional_id(pending["run_attempt"])
                or pending["run_attempt"] is None
                or record["production_run_id"] != pending["run_id"]
                or record["production_run_attempt"] != pending["run_attempt"]
                or record["terminal"]
                or record["final_outcome"] is not None
            ):
                raise StateError("invalid pending diagnostic identity")
            try:
                first_observed = _parse_pending_time(pending["first_observed_at"])
                deadline = _parse_pending_time(pending["deadline_at"])
            except (TypeError, ValueError) as exc:
                raise StateError("invalid pending diagnostic time") from exc
            if not (
                first_observed <= deadline <= first_observed + _dt.timedelta(days=14)
            ):
                raise StateError("invalid pending diagnostic deadline")
        if not isinstance(record["alerts_sent"], dict) or not all(
            isinstance(key, str) and value is True
            for key, value in record["alerts_sent"].items()
        ):
            raise StateError("invalid alerts")
        if not isinstance(record["pending_messages"], dict):
            raise StateError("invalid pending messages")
        for key, message in record["pending_messages"].items():
            if (
                not isinstance(key, str)
                or not isinstance(message, dict)
                or set(message) != {"target", "body"}
                or message["target"] not in {REPORT_TARGET, ALERT_TARGET}
                or not isinstance(message["body"], str)
                or not message["body"]
            ):
                raise StateError("invalid pending message")


class StateStore:
    """Locked, same-directory atomic JSON state store."""

    def __init__(self, path, lock_path):
        self.path = os.fspath(path)
        self.lock_path = os.fspath(lock_path)
        self._lock_handle = None

    def acquire(self):
        parent = os.path.dirname(self.lock_path) or "."
        os.makedirs(parent, mode=0o700, exist_ok=True)
        handle = open(self.lock_path, "a+", encoding="utf-8")
        try:
            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            handle.close()
            raise DuplicateTick from exc
        self._lock_handle = handle
        return True

    def release(self):
        if self._lock_handle is not None:
            try:
                fcntl.flock(self._lock_handle.fileno(), fcntl.LOCK_UN)
            finally:
                self._lock_handle.close()
                self._lock_handle = None

    def load(self):
        if not os.path.exists(self.path):
            return new_state()
        try:
            with open(self.path, "r", encoding="utf-8") as handle:
                state = json.load(handle)
            _validate_state(state)
            return state
        except StateError:
            raise
        except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
            raise StateError("corrupt state") from exc

    def save(self, state):
        _validate_state(state)
        parent = os.path.dirname(self.path) or "."
        os.makedirs(parent, mode=0o700, exist_ok=True)
        fd, temporary = tempfile.mkstemp(prefix=".epg-watchdog-", dir=parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(state, handle, sort_keys=True, separators=(",", ":"))
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(temporary, 0o600)
            os.replace(temporary, self.path)
            directory_fd = os.open(parent, os.O_RDONLY)
            try:
                os.fsync(directory_fd)
            finally:
                os.close(directory_fd)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    @staticmethod
    def ensure_day(state, day):
        _validate_state(state)
        if day not in state["days"]:
            state["days"][day] = default_day_record(day)
        return state["days"][day]

    def reserve_dispatch(self, state, day, requested_at, watchdog_id):
        record = self.ensure_day(state, day)
        if record.get("dispatch_attempted"):
            raise StateError("dispatch already reserved")
        record["dispatch_attempted"] = True
        record["dispatch_requested_at"] = requested_at
        record["watchdog_id"] = watchdog_id
        self.save(state)

    def record_dispatch(self, state, day, result):
        record = self.ensure_day(state, day)
        record["dispatch_api_result"] = result
        self.save(state)

    def queue_message(self, state, day, event_key, target, body, *, save=True):
        record = self.ensure_day(state, day)
        if record["alerts_sent"].get(event_key) is not True:
            record["pending_messages"].setdefault(
                event_key, {"target": target, "body": body}
            )
        if save:
            self.save(state)

    def mark_message_sent(self, state, day, event_key):
        record = self.ensure_day(state, day)
        record["pending_messages"].pop(event_key, None)
        record["alerts_sent"][event_key] = True
        self.save(state)

    def finalize_day(self, state, day):
        record = self.ensure_day(state, day)
        if record.get("pending_diagnostic") is not None:
            return False
        if not record.get("terminal") or record.get("pending_messages"):
            return False
        compact = {
            "scheduled_day": record["scheduled_day"],
            "dispatch_attempted": bool(record.get("dispatch_attempted")),
            "watchdog_id": record.get("watchdog_id"),
            "final_outcome": record.get("final_outcome"),
            "alerts_sent": dict(record.get("alerts_sent", {})),
            "tombstone": True,
        }
        state["days"][day] = compact
        self.save(state)
        return True



def default_day_record(day):
    return {
        "scheduled_day": day,
        "scheduled_run_id": None,
        "scheduled_conclusion": None,
        "dispatch_attempted": False,
        "dispatch_requested_at": None,
        "dispatch_api_result": None,
        "watchdog_id": None,
        "recovery_run_id": None,
        "recovery_status": None,
        "production_run_id": None,
        "production_run_attempt": None,
        "report_run_id": None,
        "alerts_sent": {},
        "pending_messages": {},
        "pending_diagnostic": None,
        "terminal": False,
        "final_outcome": None,
        "tombstone": False,
    }


class NotificationError(RuntimeError):
    """The fixed message route did not complete successfully."""

    def __init__(self, message, *, timed_out=False):
        super().__init__(message)
        self.timed_out = timed_out


class HermesNotifier:
    executable = "/Users/shameez/.local/bin/hermes"
    targets = frozenset({"ntfy:reports", "ntfy:alerts"})

    def __init__(self, *, runner):
        self.runner = runner

    def send(self, target, message):
        if target not in self.targets:
            raise NotificationError("invalid notification target")
        result = self.runner.run(
            [self.executable, "send", "--quiet", "--to", target, message],
            30,
            input_data=None,
        )
        if result.returncode != 0 or getattr(result, "timed_out", False):
            raise NotificationError(
                "notification command failed",
                timed_out=getattr(result, "timed_out", False),
            )
        return True


class DiagnosticError(ValueError):
    """The production diagnostic artifact is absent or unsafe."""


class MissingDiagnosticArtifact(DiagnosticError):
    """A complete artifact list has no exact diagnostic artifact."""


class ExpiredDiagnosticArtifact(DiagnosticError):
    """The exact diagnostic artifact is no longer retained."""


@dataclass(frozen=True)
class DiagnosticResult:
    healthy: bool
    scraper_count: int
    degraded: dict


class DiagnosticReader:
    max_artifact_bytes = 1024 * 1024

    def __init__(self, *, now=None, max_artifact_bytes=None):
        self.now = now or _dt.datetime.now(UTC)
        self.max_artifact_bytes = max_artifact_bytes or self.max_artifact_bytes

    def validate_artifact(self, run, artifacts):
        if not isinstance(run, dict) or not isinstance(run.get("id"), int) or isinstance(run.get("id"), bool):
            raise DiagnosticError("invalid workflow run metadata")
        attempt = run.get("run_attempt")
        if not isinstance(attempt, int) or isinstance(attempt, bool) or attempt < 1:
            raise DiagnosticError("invalid run attempt")
        expected = "epg-diagnostics-%d" % attempt
        matches = [item for item in artifacts if isinstance(item, dict) and item.get("name") == expected]
        if not matches:
            raise MissingDiagnosticArtifact("diagnostic artifact missing")
        if len(matches) != 1:
            raise DiagnosticError("duplicate diagnostic artifact")
        artifact = matches[0]
        artifact_id = artifact.get("id")
        if not isinstance(artifact_id, int) or isinstance(artifact_id, bool) or artifact_id <= 0:
            raise DiagnosticError("invalid diagnostic artifact id")
        if not isinstance(artifact.get("expired"), bool):
            raise DiagnosticError("invalid artifact expired flag")
        if artifact["expired"]:
            raise ExpiredDiagnosticArtifact("diagnostic artifact expired")
        expires_at = artifact.get("expires_at")
        if not isinstance(expires_at, str) or not expires_at:
            raise DiagnosticError("invalid artifact expiry")
        try:
            if _parse_time(expires_at) <= self.now:
                raise ExpiredDiagnosticArtifact("diagnostic artifact expired")
        except DiagnosticError:
            raise
        except (TypeError, ValueError) as exc:
            raise DiagnosticError("invalid artifact expiry") from exc
        workflow_run = artifact.get("workflow_run")
        if not isinstance(workflow_run, dict):
            raise DiagnosticError("invalid artifact workflow run")
        artifact_run_id = workflow_run.get("id")
        artifact_branch = workflow_run.get("head_branch")
        if (
            not isinstance(artifact_run_id, int)
            or isinstance(artifact_run_id, bool)
            or artifact_run_id <= 0
        ):
            raise DiagnosticError("invalid artifact workflow run id")
        if not isinstance(artifact_branch, str):
            raise DiagnosticError("invalid artifact workflow branch")
        if artifact_run_id != run.get("id"):
            raise DiagnosticError("artifact run mismatch")
        if artifact_branch != "main" or run.get("head_branch") != "main":
            raise DiagnosticError("artifact branch mismatch")
        size = artifact.get("size_in_bytes")
        if not isinstance(size, int) or isinstance(size, bool) or size < 0:
            raise DiagnosticError("invalid artifact size")
        if size > self.max_artifact_bytes:
            raise DiagnosticError("diagnostic artifact oversized")
        return artifact

    def read(self, run, artifacts, zip_bytes):
        self.validate_artifact(run, artifacts)
        if not isinstance(zip_bytes, bytes) or len(zip_bytes) > self.max_artifact_bytes:
            raise DiagnosticError("diagnostic artifact oversized")
        try:
            with zipfile.ZipFile(io.BytesIO(zip_bytes), "r") as archive:
                names = archive.namelist()
                if len(names) != len(set(names)):
                    raise DiagnosticError("duplicate archive member")
                if "pk_status.json" not in names:
                    raise DiagnosticError("status member missing")
                member = archive.getinfo("pk_status.json")
                if (
                    not isinstance(member.file_size, int)
                    or isinstance(member.file_size, bool)
                    or member.file_size < 0
                    or member.file_size > self.max_artifact_bytes
                ):
                    raise DiagnosticError("status member oversized")
                with archive.open("pk_status.json", "r") as member_stream:
                    raw = member_stream.read(self.max_artifact_bytes + 1)
        except DiagnosticError:
            raise
        except (OSError, zipfile.BadZipFile, KeyError, RuntimeError, zlib.error) as exc:
            raise DiagnosticError("invalid diagnostic archive") from exc
        if len(raw) > self.max_artifact_bytes:
            raise DiagnosticError("status member oversized")
        try:
            payload = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, ValueError, TypeError, json.JSONDecodeError) as exc:
            raise DiagnosticError("invalid status JSON") from exc
        if not isinstance(payload, dict) or not payload:
            raise DiagnosticError("invalid status object")
        degraded = {}
        for name, status in payload.items():
            if not isinstance(name, str) or not isinstance(status, dict):
                raise DiagnosticError("invalid status entry")
            ok = status.get("ok")
            count = status.get("count")
            error = status.get("error")
            if not isinstance(ok, bool):
                raise DiagnosticError("invalid status ok")
            if not isinstance(count, int) or isinstance(count, bool) or count < 0:
                raise DiagnosticError("invalid status count")
            if error is not None and not isinstance(error, str):
                raise DiagnosticError("invalid status error")
            if not ok:
                degraded[name] = {"count": count, "error": error}
        return DiagnosticResult(not degraded, len(payload), degraded)


class GitHubError(RuntimeError):
    """A GitHub read or write could not be trusted."""

    def __init__(self, message, *, timed_out=False):
        super().__init__(message)
        self.timed_out = timed_out


@dataclass(frozen=True)
class CommandResult:
    returncode: int
    stdout: bytes
    stderr: bytes
    timed_out: bool = False


class DeadlineRunner:
    """Clamp each subprocess timeout to the remaining tick budget."""

    def __init__(self, runner, *, monotonic=None):
        self.runner = runner
        self.monotonic = monotonic or time.monotonic
        self.deadline = None

    def run(self, args, timeout, input_data=None):
        effective_timeout = timeout
        if self.deadline is not None:
            remaining = self.deadline - self.monotonic()
            if remaining <= 0:
                return CommandResult(-1, b"", b"tick budget exhausted", timed_out=True)
            effective_timeout = min(timeout, remaining)
        return self.runner.run(args, effective_timeout, input_data=input_data)


class SubprocessAdapter:
    """Run one argument-vector command and clean its entire process group."""

    def run(self, args, timeout, input_data=None):
        import signal
        import subprocess

        process = subprocess.Popen(
            list(args),
            stdin=subprocess.PIPE if input_data is not None else subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
        try:
            stdout, stderr = process.communicate(input=input_data, timeout=timeout)
            return CommandResult(process.returncode, stdout, stderr)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                stdout, stderr = process.communicate(timeout=5)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                stdout, stderr = process.communicate(timeout=5)
            return CommandResult(process.returncode, stdout, stderr, timed_out=True)


@dataclass(frozen=True)
class DispatchResult:
    http_status: int
    workflow_run_id: object = None
    workflow_run_url: object = None


class GitHubAdapter:
    """Small fixed-endpoint adapter; all calls go through an injected runner."""

    executable = "/opt/homebrew/bin/gh"
    workflow_file = "build-epg.yml"

    def __init__(self, repository, *, runner):
        self.repository = repository
        self.runner = runner

    def _request(self, method, endpoint, *, timeout, body=None):
        args = [self.executable, "api", "--method", method, endpoint]
        result = self.runner.run(args, timeout, input_data=body)
        if result.returncode != 0 or getattr(result, "timed_out", False):
            raise GitHubError(
                "GitHub command failed",
                timed_out=getattr(result, "timed_out", False),
            )
        output = result.stdout
        if isinstance(output, bytes):
            try:
                output = output.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise GitHubError("GitHub returned malformed text") from exc
        if not output.strip():
            return None
        try:
            return json.loads(output)
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise GitHubError("GitHub returned malformed JSON") from exc

    def workflow_state(self):
        endpoint = "repos/%s/actions/workflows/%s" % (self.repository, self.workflow_file)
        payload = self._request("GET", endpoint, timeout=30)
        if not isinstance(payload, dict) or not isinstance(payload.get("id"), int):
            raise GitHubError("invalid workflow response")
        if payload.get("state") != "active":
            raise GitHubError("workflow is not active")
        return payload

    def list_runs(self, created_after):
        pages = []
        total = None
        for page in (1, 2):
            query = urlencode({
                "branch": "main",
                "per_page": 100,
                "created": ">=" + created_after,
                "page": page,
            })
            endpoint = "repos/%s/actions/workflows/%s/runs?%s" % (
                self.repository, self.workflow_file, query
            )
            payload = self._request("GET", endpoint, timeout=30)
            if not isinstance(payload, dict):
                raise GitHubError("invalid run-list response")
            reported = payload.get("total_count")
            batch = payload.get("workflow_runs")
            if (
                not isinstance(reported, int)
                or isinstance(reported, bool)
                or reported < 0
                or not isinstance(batch, list)
            ):
                raise GitHubError("invalid run-list response")
            if len(batch) > 100:
                raise GitHubError("run page exceeds bound")
            if total is None:
                total = reported
                if total > 200:
                    raise GitHubError("run-list exceeds bound")
            elif reported != total:
                raise GitHubError("run-list count changed")
            pages.extend(batch)
            if len(pages) >= total or page == 2:
                break
        if total is None or len(pages) != total:
            raise GitHubError("run-list count mismatch")
        return pages

    def list_artifacts(self, run_id):
        endpoint = "repos/%s/actions/runs/%s/artifacts?per_page=100" % (
            self.repository, int(run_id)
        )
        payload = self._request("GET", endpoint, timeout=30)
        if not isinstance(payload, dict):
            raise GitHubError("invalid artifact response")
        artifacts = payload.get("artifacts")
        total = payload.get("total_count")
        if (
            not isinstance(artifacts, list)
            or not isinstance(total, int)
            or isinstance(total, bool)
            or total < 0
            or total > 100
            or len(artifacts) != total
        ):
            raise GitHubError("incomplete artifact response")
        return artifacts

    def download_artifact(self, artifact_id):
        endpoint = "repos/%s/actions/artifacts/%s/zip" % (
            self.repository, int(artifact_id)
        )
        result = self.runner.run(
            [self.executable, "api", "--method", "GET", endpoint],
            60,
            input_data=None,
        )
        if result.returncode != 0 or getattr(result, "timed_out", False):
            raise GitHubError(
                "artifact download failed",
                timed_out=getattr(result, "timed_out", False),
            )
        if not isinstance(result.stdout, bytes):
            raise GitHubError("artifact download was not binary")
        return result.stdout

    def dispatch_recovery(self, watchdog_id):
        endpoint = "repos/%s/actions/workflows/%s/dispatches" % (
            self.repository, self.workflow_file
        )
        body = json.dumps({
            "ref": "main",
            "inputs": {"watchdog_id": watchdog_id},
        }, sort_keys=True, separators=(",", ":")).encode("utf-8")
        result = self.runner.run(
            [self.executable, "api", "--method", "POST", endpoint, "--input", "-"],
            30,
            input_data=body,
        )
        if result.returncode != 0 or getattr(result, "timed_out", False):
            raise GitHubError(
                "dispatch command failed",
                timed_out=getattr(result, "timed_out", False),
            )
        output = result.stdout
        if isinstance(output, bytes):
            output = output.decode("utf-8")
        http_status = getattr(result, "http_status", None)
        if http_status is None:
            http_status = 204 if not output.strip() else 200
        if http_status == 204:
            if output.strip():
                raise GitHubError("malformed dispatch response")
            return DispatchResult(204)
        if http_status != 200 or not output.strip():
            raise GitHubError("malformed dispatch response")
        try:
            payload = json.loads(output)
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise GitHubError("malformed dispatch response") from exc
        if not isinstance(payload, dict):
            raise GitHubError("malformed dispatch response")
        run_id = payload.get("workflow_run_id")
        run_url = payload.get("workflow_run_url") or payload.get("run_url") or payload.get("html_url")
        if not isinstance(run_id, int) or isinstance(run_id, bool) or run_id <= 0:
            raise GitHubError("malformed dispatch response")
        if not isinstance(run_url, str) or not run_url:
            raise GitHubError("malformed dispatch response")
        return DispatchResult(200, run_id, run_url)


@dataclass(frozen=True)
class Decision:
    kind: str
    should_dispatch: bool = False
    production_run: object = None
    scheduled_run: object = None
    alert_type: str = ""
    reason: str = ""


def _parse_time(value):
    if not isinstance(value, str) or not value:
        raise ValueError("timestamp must be a non-empty string")
    normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
    parsed = _dt.datetime.fromisoformat(normalized)
    if parsed.tzinfo is None:
        raise ValueError("timestamp must include a timezone")
    return parsed.astimezone(UTC)


def _order(run):
    return (_parse_time(run["created_at"]), int(run["id"]))


def _validate_run(run):
    if (
        not isinstance(run, dict)
        or not isinstance(run.get("id"), int)
        or isinstance(run.get("id"), bool)
        or run.get("id") <= 0
    ):
        raise WatchdogSchemaError("run schema has invalid id")
    event = run.get("event")
    if not isinstance(event, str):
        raise WatchdogSchemaError("run schema has invalid event")
    head_branch = run.get("head_branch")
    if not isinstance(head_branch, str):
        raise WatchdogSchemaError("run schema has invalid head_branch")
    status = run.get("status")
    if not isinstance(status, str):
        raise WatchdogSchemaError("run schema has invalid status")
    if status != "completed" and status not in ACTIVE_STATUSES:
        raise WatchdogSchemaError("run schema has unknown status")
    conclusion = run.get("conclusion")
    if conclusion is not None and not isinstance(conclusion, str):
        raise WatchdogSchemaError("run schema has invalid conclusion")
    if status == "completed" and conclusion not in TERMINAL_CONCLUSIONS:
        raise WatchdogSchemaError("run schema has unknown conclusion")
    if status in ACTIVE_STATUSES and conclusion is not None:
        raise WatchdogSchemaError("run schema has unknown conclusion")
    run_attempt = run.get("run_attempt")
    if not isinstance(run_attempt, int) or isinstance(run_attempt, bool) or run_attempt < 1:
        raise WatchdogSchemaError("run schema has invalid run_attempt")
    try:
        _parse_time(run["created_at"])
    except (KeyError, TypeError, ValueError) as exc:
        raise WatchdogSchemaError("run schema has invalid created_at") from exc


def bind_recovery_run(runs, watchdog_id):
    """Return the sole exact recovery match, or None."""
    if not isinstance(watchdog_id, str) or not RECOVERY_ID_RE.fullmatch(watchdog_id):
        raise WatchdogSchemaError("invalid recovery identity")
    expected = "EPG watchdog recovery " + watchdog_id
    matches = []
    for item in runs:
        _validate_run(item)
        title = item.get("display_title")
        if (
            item.get("event") == "workflow_dispatch"
            and item.get("head_branch") == "main"
            and title == expected
        ):
            matches.append(item)
    if len(matches) > 1:
        raise DuplicateRecoveryIdentity("duplicate recovery identity")
    return matches[0] if matches else None


def classify_day(*, now, slot, runs):
    """Classify one UTC slot without performing I/O."""
    for item in runs:
        _validate_run(item)
    scheduled = [
        item for item in runs
        if item.get("event") == "schedule"
        and item.get("head_branch") == "main"
        and slot <= _parse_time(item["created_at"]) < slot + _dt.timedelta(days=1)
    ]
    scheduled.sort(key=_order)
    selected = scheduled[-1] if scheduled else None
    if selected and selected.get("status") == "completed" and selected.get("conclusion") == "success":
        return Decision("healthy", production_run=selected, scheduled_run=selected)
    deadline = slot + _dt.timedelta(hours=13)
    if now < deadline:
        return Decision("delayed", scheduled_run=selected)
    newer_success = [
        item for item in runs
        if item.get("head_branch") == "main"
        and item.get("status") == "completed"
        and item.get("conclusion") == "success"
        and _parse_time(item["created_at"]) >= slot
        and (selected is None or _order(item) > _order(selected))
    ]
    if newer_success:
        production = max(newer_success, key=_order)
        return Decision("newer-success", production_run=production, scheduled_run=selected)
    if selected and selected.get("status") in ACTIVE_STATUSES:
        return Decision(
            "scheduled-overdue",
            scheduled_run=selected,
            alert_type="scheduled-run-overdue",
        )
    active_main = [
        item for item in runs
        if item.get("head_branch") == "main"
        and item.get("status") in ACTIVE_STATUSES
        and _parse_time(item["created_at"]) >= slot
    ]
    if active_main:
        return Decision(
            "active-run",
            scheduled_run=selected,
            reason="eligible main run is active",
        )
    if selected and selected.get("status") == "completed":
        return Decision(
            "failed",
            should_dispatch=True,
            scheduled_run=selected,
            alert_type="recovery-start",
        )
    return Decision("missing", should_dispatch=True, alert_type="recovery-start")


@dataclass(frozen=True)
class TickResult:
    exit_code: int
    decision: object = None
    stdout: str = ""
    duplicate: bool = False


RECOVERY_ID_RE = re.compile(r"^\d{4}-\d{2}-\d{2}\.[0-9a-f]{32}$")
TICK_LIMIT_SECONDS = 300
NORMAL_WORK_SECONDS = 240
FINAL_NOTIFICATION_SECONDS = 30
PROCESS_CLEANUP_SECONDS = 10
LOCAL_HEADROOM_SECONDS = 20
PENDING_DIAGNOSTIC_ADMISSION_SECONDS = 100
assert (
    NORMAL_WORK_SECONDS + FINAL_NOTIFICATION_SECONDS
    + PROCESS_CLEANUP_SECONDS + LOCAL_HEADROOM_SECONDS
    == TICK_LIMIT_SECONDS
)
REPORT_TARGET = "ntfy:reports"
ALERT_TARGET = "ntfy:alerts"


def slot_for_day(day):
    if isinstance(day, str):
        day = _dt.date.fromisoformat(day)
    return _dt.datetime.combine(day, _dt.time(4, 17), tzinfo=UTC)


def _iso(value):
    return value.astimezone(UTC).replace(microsecond=0).isoformat().replace("+00:00", "Z")


class TickTimeoutError(RuntimeError):
    """The complete watchdog tick exceeded its hard wall-clock budget."""


class WatchdogController:
    """One complete, lock-held watchdog tick."""

    def __init__(self, *, repository, github, notifier, store, now=None,
                 monotonic=None, tick_limit_seconds=TICK_LIMIT_SECONDS):
        self.repository = repository
        self.github = github
        self.notifier = notifier
        self.store = store
        self.clock = now or (lambda: _dt.datetime.now(UTC))
        self.monotonic = monotonic or time.monotonic
        self.tick_limit_seconds = tick_limit_seconds
        self._tick_start = None
        self._tick_deadline = None
        self._normal_deadline = None
        self._final_deadline = None
        self._normal_network_stopped = False
        self._final_notification_attempted = False
        self._deadline_runners = []

    def _set_runner_deadline(self, deadline):
        for runner in self._deadline_runners:
            runner.deadline = deadline

    def _begin_budget(self):
        self._tick_start = self.monotonic()
        self._tick_deadline = self._tick_start + self.tick_limit_seconds
        normal_seconds = min(NORMAL_WORK_SECONDS, self.tick_limit_seconds)
        self._normal_deadline = self._tick_start + normal_seconds
        self._final_deadline = self._normal_deadline + FINAL_NOTIFICATION_SECONDS
        self._normal_network_stopped = False
        self._final_notification_attempted = False
        self._diagnostic_attempts = 0
        self._deadline_runners = []
        for owner in (self.github, self.notifier):
            runner = getattr(owner, "runner", None)
            if runner is None:
                continue
            if not isinstance(runner, DeadlineRunner):
                runner = DeadlineRunner(runner, monotonic=self.monotonic)
                owner.runner = runner
            runner.deadline = self._normal_deadline
            self._deadline_runners.append(runner)

    def _clear_budget(self):
        for runner in self._deadline_runners:
            runner.deadline = None
        self._deadline_runners = []
        self._tick_start = None
        self._tick_deadline = None
        self._normal_deadline = None
        self._final_deadline = None
        self._normal_network_stopped = False
        self._final_notification_attempted = False
        self._diagnostic_attempts = 0

    def _check_budget(self):
        if self._normal_deadline is not None and self.monotonic() >= self._normal_deadline:
            self._normal_network_stopped = True
            raise TickTimeoutError("watchdog normal-work budget exhausted")

    def _admit_pending_diagnostic(self):
        if self._normal_network_stopped or self._diagnostic_attempts >= 2:
            return False
        if self._normal_deadline is None:
            self._diagnostic_attempts += 1
            return True
        if self._normal_deadline - self.monotonic() < PENDING_DIAGNOSTIC_ADMISSION_SECONDS:
            return False
        self._diagnostic_attempts += 1
        return True

    def _github_call(self, operation, *args):
        self._check_budget()
        try:
            result = operation(*args)
        except TickTimeoutError:
            self._normal_network_stopped = True
            raise
        except GitHubError as exc:
            if getattr(exc, "timed_out", False):
                self._normal_network_stopped = True
            raise
        except OSError as exc:
            raise GitHubError("GitHub operation failed") from exc
        self._check_budget()
        return result

    def _now(self):
        value = self.clock()
        return value.astimezone(UTC)

    def _due_day(self, now):
        today_deadline = slot_for_day(now.date()) + _dt.timedelta(hours=13)
        if now >= today_deadline:
            return now.date().isoformat()
        return (now.date() - _dt.timedelta(days=1)).isoformat()

    def _eligible_work(self, state, now):
        due_day = self._due_day(now)
        older_days = sorted(
            day for day, record in state["days"].items()
            if day < due_day
            and not record.get("tombstone")
            and not record.get("terminal")
            and record.get("pending_diagnostic") is None
        )
        due_record = state["days"].get(due_day)
        needs_due = due_record is None or not (
            due_record.get("tombstone")
            or due_record.get("terminal")
            or due_record.get("pending_diagnostic") is not None
        )
        work_days = older_days + ([due_day] if needs_due else [])
        return due_day, older_days, needs_due, work_days

    def _pending(self, state, *, final=False):
        previous_deadlines = [runner.deadline for runner in self._deadline_runners]
        if final:
            if self._final_notification_attempted:
                return True
            self._final_notification_attempted = True
            self._set_runner_deadline(self._final_deadline)
        try:
            for day in sorted(state["days"], reverse=True):
                record = state["days"][day]
                for key in sorted(record.get("pending_messages", {})):
                    if not final:
                        self._check_budget()
                    message = record["pending_messages"][key]
                    try:
                        self.notifier.send(message["target"], message["body"])
                    except NotificationError as exc:
                        if getattr(exc, "timed_out", False):
                            self._normal_network_stopped = True
                            if final:
                                return False
                            return self._pending(state, final=True)
                        return False
                    self.store.mark_message_sent(state, day, key)
                    if final:
                        return True
            return True
        finally:
            if final:
                for runner, deadline in zip(self._deadline_runners, previous_deadlines):
                    runner.deadline = deadline

    def _queue(self, state, day, event_type, target, body, *, save=True):
        key = "%s:%s" % (day, event_type)
        self.store.queue_message(state, day, key, target, body, save=save)
        return key

    def _deliver(self, state):
        if (
            self._normal_network_stopped
            or (
                self._normal_deadline is not None
                and self.monotonic() >= self._normal_deadline
            )
        ):
            self._normal_network_stopped = True
            return self._pending(state, final=True)
        return self._pending(state)

    def _run_url(self, run):
        return run.get("html_url") or run.get("url") or (
            "https://github.com/%s/actions/runs/%s" % (self.repository, run["id"])
        )

    def _queue_diagnostic_unavailable(self, state, day, record):
        self._queue(
            state, day, "diagnostic-unavailable", ALERT_TARGET,
            "event=%s:diagnostic-unavailable day=%s run=%s" % (
                day, day, record["production_run_id"]
            ),
            save=False,
        )
        self.store.save(state)

    def _finish_diagnostic(self, state, day, record, outcome):
        record["pending_diagnostic"] = None
        record["terminal"] = True
        record["final_outcome"] = outcome
        self._queue(
            state, day, outcome, ALERT_TARGET,
            "event=%s:%s day=%s run=%s" % (
                day, outcome, day, record["production_run_id"]
            ),
            save=False,
        )
        self.store.save(state)

    def _read_pending_diagnostic(self, state, day, record, run, now):
        now = self._now()
        pending = record["pending_diagnostic"]
        if now >= _parse_pending_time(pending["deadline_at"]):
            self._finish_diagnostic(state, day, record, "diagnostic-expired")
            return
        try:
            artifacts = self._github_call(self.github.list_artifacts, run["id"])
            if not isinstance(artifacts, list) or any(
                not isinstance(item, dict) for item in artifacts
            ):
                raise GitHubError("invalid artifact list")
            now = self._now()
            if now >= _parse_pending_time(pending["deadline_at"]):
                self._finish_diagnostic(state, day, record, "diagnostic-expired")
                return
            reader = DiagnosticReader(now=now)
            artifact = reader.validate_artifact(run, artifacts)
            expires_at = _parse_time(artifact["expires_at"])
            pending_deadline = _parse_pending_time(pending["deadline_at"])
            if expires_at < pending_deadline:
                record["pending_diagnostic"]["deadline_at"] = _iso(expires_at)
                self.store.save(state)
            if self._now() >= _parse_pending_time(pending["deadline_at"]):
                self._finish_diagnostic(state, day, record, "diagnostic-expired")
                return
            archive = self._github_call(self.github.download_artifact, artifact["id"])
            now = self._now()
            if now >= _parse_pending_time(pending["deadline_at"]):
                self._finish_diagnostic(state, day, record, "diagnostic-expired")
                return
            diagnostic = DiagnosticReader(now=now).read(run, [artifact], archive)
        except (MissingDiagnosticArtifact, GitHubError, TickTimeoutError):
            self._queue_diagnostic_unavailable(state, day, record)
            return
        except ExpiredDiagnosticArtifact:
            self._finish_diagnostic(state, day, record, "diagnostic-expired")
            return
        except (DiagnosticError, KeyError, TypeError, ValueError):
            self._finish_diagnostic(state, day, record, "artifact-error")
            return
        record["pending_diagnostic"] = None
        record["terminal"] = True
        record["final_outcome"] = "healthy" if diagnostic.healthy else "degraded"
        url = self._run_url(run)
        if diagnostic.healthy:
            self._queue(
                state, day, "report", REPORT_TARGET,
                "event=%s:report EPG healthy day=%s run=%s scrapers=%d" % (
                    day, day, url, diagnostic.scraper_count
                ),
                save=False,
            )
        else:
            self._queue(
                state, day, "report", REPORT_TARGET,
                "event=%s:report EPG production passed day=%s run=%s degraded=%d" % (
                    day, day, url, len(diagnostic.degraded)
                ),
                save=False,
            )
            details = ",".join(
                "%s=%d" % (name, diagnostic.degraded[name]["count"])
                for name in sorted(diagnostic.degraded)
            )
            self._queue(
                state, day, "degraded-alert", ALERT_TARGET,
                "event=%s:degraded-alert day=%s scrapers=%s" % (day, day, details),
                save=False,
            )
        self.store.save(state)

    def _production(self, state, day, record, run, now):
        run_attempt = run.get("run_attempt")
        if not _valid_optional_id(run.get("id")) or not _valid_optional_id(run_attempt):
            raise StateError("invalid production identity")
        pending = record.get("pending_diagnostic")
        if pending is None:
            record["production_run_id"] = run["id"]
            record["production_run_attempt"] = run_attempt
            record["report_run_id"] = run["id"]
            first_observed = _iso(self._now())
            record["pending_diagnostic"] = {
                "run_id": run["id"],
                "run_attempt": run_attempt,
                "first_observed_at": first_observed,
                "deadline_at": _iso(
                    _parse_pending_time(first_observed) + _dt.timedelta(days=14)
                ),
            }
            self.store.save(state)
        elif (
            pending["run_id"] != run["id"]
            or pending["run_attempt"] != run_attempt
        ):
            raise StateError("production run changed while diagnostic pending")
        if not self._admit_pending_diagnostic():
            return
        self._read_pending_diagnostic(state, day, record, run, now)

    def _process_pending_diagnostic(self, state, day, record, now):
        production_id = record.get("production_run_id")
        production_attempt = record.get("production_run_attempt")
        run = {
            "id": production_id,
            "run_attempt": production_attempt,
            "head_branch": "main",
        }
        self._read_pending_diagnostic(state, day, record, run, now)

    def _expire_pending_diagnostics(self, state, now):
        for day in sorted(state["days"], reverse=True):
            record = state["days"][day]
            pending = record.get("pending_diagnostic")
            if pending is None:
                continue
            if now >= _parse_pending_time(pending["deadline_at"]):
                self._finish_diagnostic(state, day, record, "diagnostic-expired")

    def _expire_recoveries(self, state, now):
        recovery_days = [
            day for day, record in state["days"].items()
            if not record.get("tombstone")
            and record.get("dispatch_attempted")
            and not record.get("terminal")
            and record.get("pending_diagnostic") is None
        ]
        for day in sorted(recovery_days, reverse=True):
            record = state["days"][day]
            requested = record.get("dispatch_requested_at")
            try:
                recovery_age = now - _parse_time(requested)
            except (TypeError, ValueError):
                recovery_age = _dt.timedelta(days=999)
            if recovery_age >= _dt.timedelta(days=14):
                self._process_recovery(state, day, record, [], now)

    def _pending_diagnostic_days(self, state):
        return [
            day for day, record in state["days"].items()
            if not record.get("tombstone") and record.get("pending_diagnostic") is not None
        ]

    def _process_pending_diagnostics(self, state, now):
        admitted = 0
        for day in sorted(self._pending_diagnostic_days(state), reverse=True):
            if self._normal_network_stopped or not self._admit_pending_diagnostic():
                break
            self._process_pending_diagnostic(state, day, state["days"][day], now)
            admitted += 1
            if self._normal_network_stopped:
                break
        return admitted

    def _process_recovery(self, state, day, record, runs, now):
        now = self._now()
        requested = record.get("dispatch_requested_at")
        if not requested or not record.get("watchdog_id"):
            return
        try:
            age = now - _parse_time(requested)
        except (TypeError, ValueError):
            age = _dt.timedelta(days=999)
        if age >= _dt.timedelta(days=14):
            record["terminal"] = True
            record["final_outcome"] = "expired-unresolved"
            self._queue(
                state, day, "expired-unresolved", ALERT_TARGET,
                "event=%s:expired-unresolved day=%s" % (day, day),
                save=False,
            )
            self.store.save(state)
            return
        try:
            recovery = bind_recovery_run(runs, record["watchdog_id"])
        except DuplicateRecoveryIdentity:
            record["terminal"] = True
            record["final_outcome"] = "ambiguous-recovery"
            self._queue(
                state, day, "ambiguous-recovery", ALERT_TARGET,
                "event=%s:ambiguous-recovery day=%s" % (day, day),
                save=False,
            )
            self.store.save(state)
            return
        if recovery is None:
            if runs:
                day_decision = classify_day(now=now, slot=slot_for_day(day), runs=runs)
                if day_decision.kind in {"healthy", "newer-success"}:
                    self._production(state, day, record, day_decision.production_run, now)
                    return
            if age >= _dt.timedelta(hours=2):
                self._queue(
                    state, day, "unbound-recovery", ALERT_TARGET,
                    "event=%s:unbound-recovery day=%s" % (day, day),
                )
            return
        newer_successes = [
            item for item in runs
            if item["head_branch"] == "main"
            and item["status"] == "completed"
            and item["conclusion"] == "success"
            and _order(item) > _order(recovery)
        ]
        if newer_successes:
            self._production(
                state, day, record, max(newer_successes, key=_order), now
            )
            return
        record["recovery_run_id"] = recovery["id"]
        record["recovery_status"] = recovery["status"]
        self.store.save(state)
        recovery_age = now - _parse_time(recovery["created_at"])
        if recovery["status"] in ACTIVE_STATUSES:
            if recovery_age >= _dt.timedelta(hours=3):
                self._queue(
                    state, day, "recovery-overdue", ALERT_TARGET,
                    "event=%s:recovery-overdue day=%s run=%s" % (
                        day, day, recovery["id"]
                    ),
                )
            return
        if recovery["conclusion"] == "success":
            self._production(state, day, record, recovery, now)
        else:
            record["terminal"] = True
            record["final_outcome"] = "recovery-failed"
            self._queue(
                state, day, "recovery-failed", ALERT_TARGET,
                "event=%s:recovery-failed day=%s run=%s" % (
                    day, day, recovery["id"]
                ),
                save=False,
            )
            self.store.save(state)

    def _process_slot(self, state, day, record, runs, now, oldest_day):
        slot = slot_for_day(day)
        decision = classify_day(now=now, slot=slot, runs=runs)
        if decision.scheduled_run:
            record["scheduled_run_id"] = decision.scheduled_run["id"]
            record["scheduled_conclusion"] = decision.scheduled_run.get("conclusion")
        self.store.save(state)
        if decision.kind in {"healthy", "newer-success"}:
            self._production(state, day, record, decision.production_run, now)
        elif decision.kind == "scheduled-overdue":
            self._queue(
                state, day, "scheduled-run-overdue", ALERT_TARGET,
                "event=%s:scheduled-run-overdue day=%s run=%s" % (
                    day, day, decision.scheduled_run["id"]
                ),
            )
        elif decision.should_dispatch and not record.get("dispatch_attempted"):
            self._github_call(self.github.workflow_state)
            second_runs = self._github_call(
                self.github.list_runs, _iso(slot_for_day(oldest_day))
            )
            second = classify_day(now=now, slot=slot, runs=second_runs)
            decision = second
            if second.kind in {"healthy", "newer-success"}:
                self._production(state, day, record, second.production_run, now)
            elif second.kind == "scheduled-overdue":
                self._queue(
                    state, day, "scheduled-run-overdue", ALERT_TARGET,
                    "event=%s:scheduled-run-overdue day=%s run=%s" % (
                        day, day, second.scheduled_run["id"]
                    ),
                )
            elif not any(item.get("status") in ACTIVE_STATUSES for item in second_runs):
                watchdog_id = "%s.%s" % (day, secrets.token_hex(16))
                if not RECOVERY_ID_RE.fullmatch(watchdog_id):
                    raise StateError("invalid recovery identity")
                self._check_budget()
                self.store.reserve_dispatch(
                    state, day, _iso(now), watchdog_id
                )
                try:
                    dispatch = self._github_call(self.github.dispatch_recovery, watchdog_id)
                    dispatch_result = {
                        "status": "accepted",
                        "http_status": dispatch.http_status,
                    }
                    if dispatch.http_status == 200:
                        dispatch_result["workflow_run_id"] = dispatch.workflow_run_id
                    self.store.record_dispatch(state, day, dispatch_result)
                    self._queue(
                        state, day, "recovery-start", ALERT_TARGET,
                        "event=%s:recovery-start day=%s watchdog_id=%s" % (
                            day, day, watchdog_id
                        ),
                    )
                except (GitHubError, OSError, ValueError):
                    self.store.record_dispatch(state, day, {"status": "uncertain"})
                    self._queue(
                        state, day, "dispatch-uncertain", ALERT_TARGET,
                        "event=%s:dispatch-uncertain day=%s watchdog_id=%s request may have been accepted" % (
                            day, day, watchdog_id
                        ),
                        save=False,
                    )
                    self.store.save(state)
        return decision

    def _dependency_failure(self, state, day, event_type):
        self._queue(
            state, day, event_type, ALERT_TARGET,
            "event=%s:%s day=%s" % (day, event_type, day),
        )
        return self._deliver(state)

    def check_only(self):
        self._begin_budget()
        try:
            now = self._now()
            day = self._due_day(now)
            if day is None:
                decision = Decision("delayed")
            else:
                self._github_call(self.github.workflow_state)
                runs = self._github_call(self.github.list_runs, _iso(slot_for_day(day)))
                decision = classify_day(now=now, slot=slot_for_day(day), runs=runs)
            successful = decision.kind in {"healthy", "newer-success"}
            selected_run = decision.production_run if successful else decision.scheduled_run
            redacted = json.dumps({
                "day": day or now.date().isoformat(),
                "kind": "production-run-success" if successful else decision.kind,
                "run_id": selected_run["id"] if selected_run else None,
                "diagnostics_checked": False,
            }, sort_keys=True)
            return TickResult(0, decision=decision, stdout=redacted)
        except (GitHubError, WatchdogSchemaError, TickTimeoutError, OSError, ValueError):
            return TickResult(1)
        finally:
            self._clear_budget()

    def tick(self, *, check_only=False):
        if check_only:
            return self.check_only()
        try:
            self.store.acquire()
        except DuplicateTick:
            return TickResult(0, duplicate=True)
        try:
            self._begin_budget()
            self._check_budget()
            now = self._now()
            try:
                state = self.store.load()
            except StateError:
                try:
                    self.notifier.send(ALERT_TARGET, "event=state-error state=unreadable")
                except NotificationError:
                    return TickResult(1)
                return TickResult(0)
            active_day = None

            # These are local transitions and must happen before any external call.
            self._expire_pending_diagnostics(state, now)
            self._expire_recoveries(state, now)

            if not self._deliver(state):
                return TickResult(1)
            if self._normal_network_stopped:
                return TickResult(0)

            self._process_pending_diagnostics(state, now)
            if not self._deliver(state):
                return TickResult(1)
            if self._normal_network_stopped:
                return TickResult(0)

            now = self._now()
            self._expire_pending_diagnostics(state, now)
            self._expire_recoveries(state, now)
            if not self._deliver(state):
                return TickResult(1)
            if self._normal_network_stopped:
                return TickResult(0)
            now = self._now()

            due_day, older_days, needs_due, work_days = self._eligible_work(state, now)
            if not work_days:
                if not self._deliver(state):
                    return TickResult(1)
                for day, item in list(state["days"].items()):
                    if item.get("terminal"):
                        self.store.finalize_day(state, day)
                return TickResult(0)

            oldest = min(work_days)
            self._github_call(self.github.workflow_state)
            now = self._now()
            self._expire_pending_diagnostics(state, now)
            self._expire_recoveries(state, now)
            if not self._deliver(state):
                return TickResult(1)
            if self._normal_network_stopped:
                return TickResult(0)
            now = self._now()
            self._expire_pending_diagnostics(state, now)
            self._expire_recoveries(state, now)
            due_day, older_days, needs_due, work_days = self._eligible_work(state, now)
            if not work_days:
                if not self._deliver(state):
                    return TickResult(1)
                for day, item in list(state["days"].items()):
                    if item.get("terminal"):
                        self.store.finalize_day(state, day)
                return TickResult(0)
            oldest = min(work_days)
            runs = self._github_call(self.github.list_runs, _iso(slot_for_day(oldest)))
            for day in sorted(older_days, reverse=True):
                if self._normal_network_stopped:
                    break
                active_day = day
                record = state["days"][day]
                if record.get("dispatch_attempted"):
                    self._process_recovery(state, day, record, runs, now)
                else:
                    self._process_slot(state, day, record, runs, now, oldest)

            decision = None
            if needs_due and not self._normal_network_stopped:
                active_day = due_day
                record = state["days"].get(due_day)
                if record and record.get("dispatch_attempted") and not record.get("terminal"):
                    self._process_recovery(state, due_day, record, runs, now)
                elif not (record and record.get("tombstone")):
                    record = self.store.ensure_day(state, due_day)
                    decision = self._process_slot(
                        state, due_day, record, runs, now, oldest
                    )
            if not self._deliver(state):
                return TickResult(1, decision=decision)
            for day, item in list(state["days"].items()):
                if item.get("terminal"):
                    self.store.finalize_day(state, day)
            return TickResult(0, decision=decision)
        except OSError:
            return TickResult(1)
        except (TickTimeoutError, GitHubError, WatchdogSchemaError, DiagnosticError, StateError, ValueError) as exc:
            try:
                state = locals().get("state")
                if state is None:
                    return TickResult(1)
                day = locals().get("active_day") or locals().get("due_day")
                if day is None:
                    pending_days = [
                        candidate for candidate, item in state["days"].items()
                        if item.get("dispatch_attempted") and not item.get("tombstone")
                    ]
                    day = max(pending_days, default=None)
                if day is not None and not state["days"].get(day, {}).get("tombstone"):
                    delivered = self._dependency_failure(state, day, "dependency-error")
                    return TickResult(0 if delivered else 1)
            except (NotificationError, OSError, StateError):
                return TickResult(1)
            return TickResult(1)
        finally:
            self._clear_budget()
            self.store.release()


DEFAULT_REPOSITORY = "shameez-struggles-to-commit/EPG"
DEFAULT_STATE_PATH = "/Users/shameez/.hermes/cron/epg_github_watchdog_state.json"
DEFAULT_LOCK_PATH = "/Users/shameez/.hermes/cron/epg_github_watchdog.lock"


def main(argv=None, *, github=None, notifier=None, store=None, now=None, monotonic=None):
    parser = argparse.ArgumentParser(description="Check and recover the EPG GitHub workflow")
    parser.add_argument("--repository", default=DEFAULT_REPOSITORY)
    parser.add_argument("--state-path", default=DEFAULT_STATE_PATH)
    parser.add_argument("--lock-path", default=DEFAULT_LOCK_PATH)
    parser.add_argument("--check-only", action="store_true")
    args = parser.parse_args(argv)

    if store is None:
        store = StateStore(args.state_path, args.lock_path)
    if github is None or notifier is None:
        runner = SubprocessAdapter()
        if github is None:
            github = GitHubAdapter(args.repository, runner=runner)
        if notifier is None:
            notifier = HermesNotifier(runner=runner)
    controller = WatchdogController(
        repository=args.repository,
        github=github,
        notifier=notifier,
        store=store,
        now=now,
        monotonic=monotonic,
    )
    result = controller.tick(check_only=args.check_only)
    if result.stdout:
        print(result.stdout)
    return result.exit_code


if __name__ == "__main__":
    raise SystemExit(main())
