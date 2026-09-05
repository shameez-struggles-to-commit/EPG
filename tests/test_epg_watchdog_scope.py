import ast
import pathlib
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
SOURCE_PATH = ROOT / "ops" / "epg_github_watchdog.py"


class WatchdogScopeTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.source = SOURCE_PATH.read_text(encoding="utf-8")
        cls.tree = ast.parse(cls.source, filename=str(SOURCE_PATH))

    def test_watchdog_has_no_deferred_pipeline_or_provider_scope(self):
        self.assertNotIn("pk_scrapers.py", self.source)
        self.assertNotRegex(self.source, r"\bIPTV_[A-Z_]+\b")
        self.assertNotIn("B2", self.source)
        self.assertNotRegex(self.source, r"\b(?:XMLTV|ElementTree)\b")
        self.assertNotIn("git pull", self.source.lower())
        self.assertNotRegex(self.source, r"/actions/(?:runs/[^\s/]+/)?(?:cancel|rerun)\b")

    def test_external_commands_are_restricted_to_the_fixed_adapters(self):
        process_calls = [
            node for node in ast.walk(self.tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr in {"run", "Popen"}
        ]
        self.assertGreaterEqual(len(process_calls), 4)
        for node in process_calls:
            if node.func.attr == "Popen":
                continue
            owner = node.func.value
            self.assertTrue(
                isinstance(owner, ast.Attribute) and owner.attr in {"runner", "process"},
                ast.dump(node),
            )

    def test_timeout_and_process_group_contract_is_present(self):
        self.assertIn("TICK_LIMIT_SECONDS = 300", self.source)
        self.assertIn("start_new_session=True", self.source)
        self.assertIn("os.killpg(process.pid, signal.SIGTERM)", self.source)
        self.assertIn("os.killpg(process.pid, signal.SIGKILL)", self.source)
        self.assertIn("timed_out=True", self.source)
        self.assertIn("            30,\n            input_data=None", self.source)
        self.assertIn("            60,\n            input_data=None", self.source)

    def test_lock_release_is_in_tick_finally(self):
        self.assertIn(
            "        finally:\n            self._clear_budget()\n            self.store.release()",
            self.source,
        )


if __name__ == "__main__":
    unittest.main()
