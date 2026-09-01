import ast
import pathlib
import unittest


ROOT = pathlib.Path(__file__).resolve().parents[1]
RETIRED_SITE = "tvcesoir.fr"


def assigned_literal(path, name):
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        if any(isinstance(target, ast.Name) and target.id == name for target in node.targets):
            return ast.literal_eval(node.value)
    raise AssertionError(f"{name} not found in {path}")


class RetiredSourceTest(unittest.TestCase):
    def test_tvcesoir_is_not_configured(self):
        workflow = (ROOT / ".github/workflows/build-epg.yml").read_text(encoding="utf-8")
        sites = assigned_literal(ROOT / "pipeline/make_iptvorg_channels.py", "SITES")
        countries = assigned_literal(
            ROOT / "pipeline/build_mapping.py", "IPTV_ORG_COUNTRIES"
        )

        self.assertNotIn(RETIRED_SITE, workflow)
        self.assertNotIn(RETIRED_SITE, sites)
        self.assertNotIn(RETIRED_SITE, countries)


if __name__ == "__main__":
    unittest.main()
