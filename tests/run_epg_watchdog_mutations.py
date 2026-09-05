#!/usr/bin/env python3
"""Run fail-fast watchdog-core mutation checks in isolated temp trees."""

from __future__ import annotations

import pathlib
import shutil
import subprocess
import sys
import tempfile


ROOT = pathlib.Path(__file__).resolve().parents[1]
SOURCE = ROOT / "ops" / "epg_github_watchdog.py"
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
        """                        self.store.reserve_dispatch(
                            state, due_day, _iso(now), watchdog_id
                        )
""",
        """                        # MUTATION: reservation omitted before dispatch
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
        """                try:
                    self.notifier.send(message["target"], message["body"])
                except NotificationError:
                    return False
                self.store.mark_message_sent(state, day, key)
""",
        """                self.store.mark_message_sent(state, day, key)
                try:
                    self.notifier.send(message["target"], message["body"])
                except NotificationError:
                    return False
""",
        "tests.test_epg_github_watchdog.ControllerEndToEndTest.test_ntfy_error_leaves_terminal_message_for_the_next_tick",
    ),
)


def make_tree(destination: pathlib.Path) -> None:
    (destination / "ops").mkdir(parents=True)
    (destination / "tests").mkdir(parents=True)
    shutil.copy2(SOURCE, destination / "ops" / SOURCE.name)
    shutil.copy2(TESTS / "__init__.py", destination / "tests" / "__init__.py")
    shutil.copy2(TESTS / "test_epg_github_watchdog.py", destination / "tests" / "test_epg_github_watchdog.py")
    shutil.copytree(TESTS / "fixtures", destination / "tests" / "fixtures")


def main() -> int:
    failures = []
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
    if failures:
        print("mutation failures: " + ", ".join(failures))
        return 1
    print(f"mutation checks passed: {len(MUTATIONS)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
