"""Every workflow under ``.github/workflows`` must be valid YAML.

A workflow with one line indented too little is not "a step that fails": GitHub refuses the
whole file before any job starts (run 39070916784, PR #14), so nothing runs and nothing says
which line is wrong. This test parses each file the way the runner must, and fails with the
offending line instead.
"""

from pathlib import Path
import unittest

import yaml

REPO = Path(__file__).resolve().parents[1]
WORKFLOWS = REPO / ".github" / "workflows"


class TestWorkflowFiles(unittest.TestCase):
    def test_every_workflow_parses_as_yaml(self):
        files = sorted(WORKFLOWS.glob("*.yml"))
        self.assertTrue(files, f"no workflows found in {WORKFLOWS}")
        for path in files:
            with self.subTest(path=path.name):
                text = path.read_text(encoding="utf-8")
                try:
                    parsed = yaml.safe_load(text)
                except yaml.YAMLError as exc:   # line and column of the offending token
                    self.fail(f"{path.name} is not valid YAML: {exc}")
                self.assertIsInstance(parsed, dict, path.name)
                self.assertTrue(parsed.get("jobs"), f"{path.name} declares no jobs")

    def test_every_block_scalar_keeps_its_indentation(self):
        """``run: |`` ends at the first line that is not indented under the key.

        That is how the guard step lost ``echo``/``exit 1`` to column 1 in PR #14: the
        block ended mid-command and the rest of the file stopped being YAML.
        """
        for path in sorted(WORKFLOWS.glob("*.yml")):
            lines = path.read_text(encoding="utf-8").splitlines()
            block_indent = None
            for number, line in enumerate(lines, start=1):
                if not line.strip():
                    continue
                if block_indent is not None:
                    indent = len(line) - len(line.lstrip())
                    if indent > block_indent:
                        continue
                    block_indent = None       # the block ended on a properly indented line
                if line.rstrip().endswith((": |", ": |-", ": >", ": >-")):
                    block_indent = len(line) - len(line.lstrip())
                elif line.startswith(" ") and not line.lstrip().startswith("#") and \
                        not line.lstrip().startswith("-") and ":" not in line:
                    # a line inside a list item that is not a `key: value` must still be
                    # indented deeper than the enclosing `- name:`; column-0 text never is
                    self.assertGreater(
                        len(line) - len(line.lstrip()), 0,
                        f"{path.name}:{number} starts at column 1 inside a mapping")


if __name__ == "__main__":
    unittest.main()
