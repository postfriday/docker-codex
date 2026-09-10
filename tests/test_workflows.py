from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]


class WorkflowRuntimeTests(unittest.TestCase):
    def test_first_party_actions_use_node24_releases(self):
        workflows = "\n".join(
            path.read_text() for path in (ROOT / ".github" / "workflows").glob("*.yml")
        )

        self.assertNotIn("actions/checkout@v4", workflows)
        self.assertNotIn("actions/setup-go@v5", workflows)

    def test_actionlint_uses_compatible_go_version(self):
        workflow = (ROOT / ".github" / "workflows" / "release-tests.yml").read_text()

        self.assertIn("go-version: '1.25.x'", workflow)


if __name__ == "__main__":
    unittest.main()
