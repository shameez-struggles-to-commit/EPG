import json
import pathlib
import tempfile
import unittest

from pipeline.source_registry import validate_iptv_org_outputs


ROOT = pathlib.Path(__file__).resolve().parents[1]
CONFIG = ROOT / "config/sources.json"


class SourceRegistryTest(unittest.TestCase):
    def load_registry(self):
        return json.loads(CONFIG.read_text(encoding="utf-8"))

    def test_registry_has_unique_source_names(self):
        sources = self.load_registry()["sources"]
        names = [source["name"] for source in sources]
        self.assertEqual(len(names), len(set(names)))

    def test_every_iptv_org_source_has_complete_metadata(self):
        sources = [
            source
            for source in self.load_registry()["sources"]
            if source["kind"] == "iptv-org"
        ]
        self.assertGreaterEqual(len(sources), 30)
        for source in sources:
            with self.subTest(source=source["name"]):
                self.assertTrue(source["countries"])
                self.assertIn(source["policy"], {"required", "optional"})
                self.assertGreaterEqual(source["min_programmes"], 0)
                self.assertTrue(source["output"].startswith("io_"))
                self.assertTrue(source["output"].endswith(".xml"))

    def test_retired_source_is_recorded_but_not_active(self):
        retired = self.load_registry()["retired"]
        entry = next(source for source in retired if source["name"] == "tvcesoir.fr")
        self.assertEqual(entry["retired_on"], "2026-09-01")
        self.assertEqual(entry["reason"], "service_closed")
        active = {source["name"] for source in self.load_registry()["sources"]}
        self.assertNotIn("tvcesoir.fr", active)

    def test_all_iptv_org_sources_are_optional(self):
        sources = [
            source for source in self.load_registry()["sources"]
            if source["kind"] == "iptv-org"
        ]
        self.assertTrue(sources)
        self.assertEqual({"optional"}, {source["policy"] for source in sources})

    def test_python_consumers_use_the_registry(self):
        make_channels = (ROOT / "pipeline/make_iptvorg_channels.py").read_text(
            encoding="utf-8"
        )
        mapping = (ROOT / "pipeline/build_mapping.py").read_text(encoding="utf-8")
        self.assertIn("load_source_registry", make_channels)
        self.assertIn("iptv_org_countries", mapping)
        self.assertNotIn("SITES = [", make_channels)
        self.assertNotIn("IPTV_ORG_COUNTRIES = {", mapping)

    def test_optional_empty_output_is_degraded_not_fatal(self):
        registry = {
            "schema_version": 1,
            "sources": [{
                "name": "optional.example", "kind": "iptv-org",
                "site": "optional.example", "countries": ["US"],
                "filtered": True, "policy": "optional",
                "min_programmes": 1, "output": "io_optional.xml",
            }],
            "retired": [],
        }
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            config = root / "sources.json"
            config.write_text(json.dumps(registry), encoding="utf-8")
            (root / "io_optional.xml").write_text("<tv></tv>", encoding="utf-8")
            statuses, failures = validate_iptv_org_outputs(root, config)
        self.assertEqual([], failures)
        self.assertEqual("degraded", statuses[0]["status"])
        self.assertFalse(statuses[0]["usable"])

    def test_required_empty_output_is_fatal(self):
        registry = {
            "schema_version": 1,
            "sources": [{
                "name": "required.example", "kind": "iptv-org",
                "site": "required.example", "countries": ["US"],
                "filtered": True, "policy": "required",
                "min_programmes": 1, "output": "io_required.xml",
            }],
            "retired": [],
        }
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            config = root / "sources.json"
            config.write_text(json.dumps(registry), encoding="utf-8")
            (root / "io_required.xml").write_text("<tv></tv>", encoding="utf-8")
            statuses, failures = validate_iptv_org_outputs(root, config)
        self.assertEqual(1, len(failures))
        self.assertEqual("failed", statuses[0]["status"])

    def test_required_malformed_output_is_fatal(self):
        registry = {
            "schema_version": 1,
            "sources": [{
                "name": "required.example", "kind": "iptv-org",
                "site": "required.example", "countries": ["US"],
                "filtered": True, "policy": "required",
                "min_programmes": 1, "output": "io_required.xml",
            }],
            "retired": [],
        }
        with tempfile.TemporaryDirectory() as directory:
            root = pathlib.Path(directory)
            config = root / "sources.json"
            config.write_text(json.dumps(registry), encoding="utf-8")
            (root / "io_required.xml").write_text("<html><programme></html>", encoding="utf-8")
            statuses, failures = validate_iptv_org_outputs(root, config)
        self.assertEqual(1, len(failures))
        self.assertEqual("invalid_xmltv", statuses[0]["reason"])

    def test_invalid_policy_is_rejected(self):
        registry = {
            "schema_version": 1,
            "sources": [{
                "name": "bad.example", "kind": "iptv-org",
                "site": "bad.example", "countries": ["US"],
                "filtered": True, "policy": "requred",
                "min_programmes": 1, "output": "io_bad.xml",
            }],
            "retired": [],
        }
        with tempfile.TemporaryDirectory() as directory:
            config = pathlib.Path(directory) / "sources.json"
            config.write_text(json.dumps(registry), encoding="utf-8")
            with self.assertRaises(ValueError):
                validate_iptv_org_outputs(directory, config)

    def test_invalid_field_types_are_rejected(self):
        base = {
            "schema_version": 1,
            "sources": [{
                "name": "good.example", "kind": "iptv-org",
                "site": "good.example", "countries": ["US"],
                "filtered": True, "policy": "required",
                "min_programmes": 1, "output": "io_good.xml",
            }],
            "retired": [],
        }
        mutations = {
            "countries": "US",
            "min_programmes": True,
            "output": "io_good.txt",
            "name": "",
            "site": "bad|site",
        }
        for field, value in mutations.items():
            with self.subTest(field=field), tempfile.TemporaryDirectory() as directory:
                registry = json.loads(json.dumps(base))
                registry["sources"][0][field] = value
                config = pathlib.Path(directory) / "sources.json"
                config.write_text(json.dumps(registry), encoding="utf-8")
                with self.assertRaises(ValueError):
                    validate_iptv_org_outputs(directory, config)

    def test_workflow_does_not_copy_the_source_list(self):
        workflow = (ROOT / ".github/workflows/build-epg.yml").read_text(encoding="utf-8")
        self.assertIn("list-iptv-org --filtered --pairs", workflow)
        self.assertIn("list-iptv-org --unfiltered --pairs", workflow)
        self.assertNotIn("for site in tvpassport.com", workflow)
        self.assertNotIn("IPTV_ORG_FILES: \"data/io_jiotv.xml", workflow)


if __name__ == "__main__":
    unittest.main()
