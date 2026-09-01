import json
import pathlib
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
RETIRED_SITE = "tvcesoir.fr"


class RetiredSourceTest(unittest.TestCase):
    def test_tvcesoir_is_retired_and_not_active(self):
        workflow = (ROOT / ".github/workflows/build-epg.yml").read_text(encoding="utf-8")
        registry = json.loads(
            (ROOT / "config/sources.json").read_text(encoding="utf-8")
        )
        active = {source["name"] for source in registry["sources"]}
        retired = {source["name"] for source in registry["retired"]}

        self.assertNotIn(RETIRED_SITE, workflow)
        self.assertNotIn(RETIRED_SITE, active)
        self.assertIn(RETIRED_SITE, retired)


if __name__ == "__main__":
    unittest.main()
