#!/usr/bin/env python3
"""Run fail-fast watchdog-core mutation checks in isolated temp trees."""

from __future__ import annotations

import hashlib
import pathlib
import shutil
import subprocess
import sys
import tempfile


ROOT = pathlib.Path(__file__).resolve().parents[1]
SOURCE = ROOT / "ops" / "epg_github_watchdog.py"
RELEASE_SOURCE = ROOT / "pipeline" / "release_order_guard.py"
WORKFLOW_SOURCE = ROOT / ".github" / "workflows" / "build-epg.yml"
TESTS = ROOT / "tests"


MUTATIONS = (
    (
        "remove-active-run-guard",
        "if active_main:",
        "if False and active_main:",
        "tests.test_epg_github_watchdog.ControllerEndToEndTest.test_controller_ignores_active_push_without_false_scheduled_overdue_alert",
    ),
    (
        "remove-newer-success-guard",
        "if newer_success:",
        "if False and newer_success:",
        "tests.test_epg_github_watchdog.ClassifierTest.test_newer_successful_manual_run_suppresses_failed_schedule_recovery",
    ),
    (
        "move-reservation-after-dispatch",
        """                self.store.reserve_dispatch(
                    state, day, _iso(now), watchdog_id
                )
""",
        """                # MUTATION: reservation omitted before dispatch
""",
        "tests.test_epg_github_watchdog.ControllerEndToEndTest.test_dispatch_sees_durable_reservation_before_api_call",
    ),
    (
        "change-dispatch-ref",
        '"ref": "main"',
        '"ref": "other"',
        "tests.test_epg_github_watchdog.ClassifierTest.test_dispatch_accepts_legacy_empty_204_response_and_exact_payload",
    ),
    (
        "remove-exact-recovery-binding",
        "and title == expected",
        "and True",
        "tests.test_epg_github_watchdog.ClassifierTest.test_recovery_binding_rejects_name_without_exact_display_title",
    ),
    (
        "mark-notification-before-send",
        """                    try:
                        self.notifier.send(message["target"], message["body"])
                    except NotificationError as exc:
                        if getattr(exc, "timed_out", False):
                            self._normal_network_stopped = True
                            if final:
                                return False
                            return self._pending(state, final=True)
                        return False
                    self.store.mark_message_sent(state, day, key)
""",
        """                    self.store.mark_message_sent(state, day, key)
                    try:
                        self.notifier.send(message["target"], message["body"])
                    except NotificationError as exc:
                        if getattr(exc, "timed_out", False):
                            self._normal_network_stopped = True
                            if final:
                                return False
                            return self._pending(state, final=True)
                        return False
""",
        "tests.test_epg_github_watchdog.ControllerEndToEndTest.test_ntfy_error_leaves_terminal_message_for_the_next_tick",
    ),
    (
        "restore-terminal-state-after-uncertain-dispatch",
        """                    self.store.record_dispatch(state, day, {"status": "uncertain"})
                    self._queue(
""",
        """                    self.store.record_dispatch(state, day, {"status": "uncertain"})
                    record["terminal"] = True
                    record["final_outcome"] = "dispatch-failed"
                    self._queue(
""",
        "tests.test_epg_github_watchdog.ControllerEndToEndTest.test_uncertain_dispatch_is_observed_on_later_tick_without_redispatch",
    ),
    (
        "close-retryable-diagnostic-error",
        """        except (MissingDiagnosticArtifact, GitHubError, TickTimeoutError):
            self._queue_diagnostic_unavailable(state, day, record)
            return
""",
        """        except (MissingDiagnosticArtifact, GitHubError, TickTimeoutError):
            self._finish_diagnostic(state, day, record, "artifact-error")
            return
""",
        "tests.test_epg_github_watchdog.ControllerEndToEndTest.test_list_error_then_valid_artifact_keeps_pending_and_sends_one_temporary_alert",
    ),
    (
        "remove-diagnostic-deadline-persistence",
        """            if expires_at < pending_deadline:
                record["pending_diagnostic"]["deadline_at"] = _iso(expires_at)
                self.store.save(state)
""",
        """            if expires_at < pending_deadline:
                record["pending_diagnostic"]["deadline_at"] = _iso(expires_at)
""",
        "tests.test_epg_github_watchdog.ControllerEndToEndTest.test_artifact_expiry_shortens_pending_deadline_before_download",
    ),
    (
        "route-pending-diagnostic-into-slot-work",
        """        needs_due = due_record is None or not (
            due_record.get("tombstone")
            or due_record.get("terminal")
            or due_record.get("pending_diagnostic") is not None
        )
""",
        """        needs_due = due_record is None or not (
            due_record.get("tombstone")
            or due_record.get("terminal")
        )
""",
        "tests.test_epg_github_watchdog.ControllerEndToEndTest.test_pending_diagnostic_is_deferred_when_less_than_one_hundred_seconds_remain",
    ),
)


ADDITIONAL_MUTATIONS = (
    (
        "remove-queue-max",
        WORKFLOW_SOURCE,
        "  queue: max\n",
        "",
        "tests.test_workflow_security.WorkflowSecurityTest.test_build_uses_serial_queue_without_cancellation",
        "workflow",
    ),
    (
        "disable-release-order-comparison",
        RELEASE_SOURCE,
        "        if key > current_key:\n",
        "        if False and key > current_key:\n",
        "tests.test_release_order_guard.ReleaseOrderGuardTest.test_same_timestamp_with_larger_run_id_blocks_deploy",
        "release",
    ),
)


def make_tree(destination: pathlib.Path) -> None:
    (destination / "ops").mkdir(parents=True)
    (destination / "tests").mkdir(parents=True)
    shutil.copy2(SOURCE, destination / "ops" / SOURCE.name)
    shutil.copy2(TESTS / "__init__.py", destination / "tests" / "__init__.py")
    shutil.copy2(TESTS / "test_epg_github_watchdog.py", destination / "tests" / "test_epg_github_watchdog.py")
    shutil.copytree(TESTS / "fixtures", destination / "tests" / "fixtures")


def make_release_tree(destination: pathlib.Path) -> None:
    (destination / "pipeline").mkdir(parents=True)
    (destination / "tests").mkdir(parents=True)
    shutil.copy2(RELEASE_SOURCE, destination / "pipeline" / RELEASE_SOURCE.name)
    shutil.copy2(TESTS / "__init__.py", destination / "tests" / "__init__.py")
    shutil.copy2(TESTS / "test_release_order_guard.py", destination / "tests" / "test_release_order_guard.py")
    shutil.copytree(TESTS / "fixtures", destination / "tests" / "fixtures")


def make_workflow_tree(destination: pathlib.Path) -> None:
    (destination / ".github" / "workflows").mkdir(parents=True)
    (destination / "pipeline").mkdir(parents=True)
    (destination / "tests").mkdir(parents=True)
    shutil.copy2(WORKFLOW_SOURCE, destination / ".github" / "workflows" / WORKFLOW_SOURCE.name)
    shutil.copy2(TESTS / "__init__.py", destination / "tests" / "__init__.py")
    shutil.copy2(TESTS / "test_workflow_security.py", destination / "tests" / "test_workflow_security.py")
    for filename in ("make_iptvorg_channels.py", "fetch_skyhawk.py"):
        shutil.copy2(ROOT / "pipeline" / filename, destination / "pipeline" / filename)


def _assert_source_unchanged(source_path: pathlib.Path, original_digest: str) -> None:
    current_digest = hashlib.sha256(source_path.read_bytes()).hexdigest()
    if current_digest != original_digest:
        raise RuntimeError("mutation runner modified source: %s" % source_path)


def main() -> int:
    failures = []
    source_digests = {
        path: hashlib.sha256(path.read_bytes()).hexdigest()
        for path in (SOURCE, RELEASE_SOURCE, WORKFLOW_SOURCE)
    }
    for name, old, new, test_name in MUTATIONS:
        with tempfile.TemporaryDirectory(prefix="epg-watchdog-mut-") as directory:
            tree = pathlib.Path(directory)
            make_tree(tree)
            path = tree / "ops" / SOURCE.name
            source = path.read_text(encoding="utf-8")
            occurrences = source.count(old)
            if occurrences != 1:
                print(f"MUTATION {name}: setup failed, occurrences={occurrences}")
                failures.append(name)
                continue
            path.write_text(source.replace(old, new, 1), encoding="utf-8")
            completed = subprocess.run(
                [sys.executable, "-m", "unittest", test_name, "-q"],
                cwd=tree,
                text=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                check=False,
            )
            if completed.returncode == 0:
                print(f"MUTATION {name}: SURVIVED (exit 0)")
                failures.append(name)
            else:
                print(f"MUTATION {name}: killed (expected non-zero exit {completed.returncode})")

    builders = {"release": make_release_tree, "workflow": make_workflow_tree}
    for name, source_path, old, new, test_name, kind in ADDITIONAL_MUTATIONS:
        original_digest = hashlib.sha256(source_path.read_bytes()).hexdigest()
        try:
            with tempfile.TemporaryDirectory(prefix="epg-watchdog-mut-") as directory:
                tree = pathlib.Path(directory)
                builders[kind](tree)
                path = tree / source_path.relative_to(ROOT)
                source = path.read_text(encoding="utf-8")
                occurrences = source.count(old)
                if occurrences != 1:
                    print(f"MUTATION {name}: setup failed, occurrences={occurrences}")
                    failures.append(name)
                    continue
                path.write_text(source.replace(old, new, 1), encoding="utf-8")
                completed = subprocess.run(
                    [sys.executable, "-m", "unittest", test_name, "-q"],
                    cwd=tree,
                    text=True,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    check=False,
                )
                if completed.returncode == 0:
                    print(f"MUTATION {name}: SURVIVED (exit 0)")
                    failures.append(name)
                else:
                    print(f"MUTATION {name}: killed (expected non-zero exit {completed.returncode})")
        finally:
            _assert_source_unchanged(source_path, original_digest)

    for source_path, original_digest in source_digests.items():
        _assert_source_unchanged(source_path, original_digest)
    if failures:
        print("mutation failures: " + ", ".join(failures))
        return 1
    print(f"mutation checks passed: {len(MUTATIONS) + len(ADDITIONAL_MUTATIONS)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
