import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from fixtures import ROOT


class UpdateWorkflowTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        result = subprocess.run(
            ["ruby", "-ryaml", "-e",
             'print YAML.load_file(ARGV[0])["jobs"]["sync"]["steps"].last["run"]',
             str(ROOT / ".github/workflows/sync-release.yml")],
            check=True, capture_output=True, text=True,
        )
        cls.script = result.stdout

    def run_workflow(self, **settings):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "releases").mkdir()
            (root / "releases/latest.txt").write_text("co-release-v1\nversion 1.2.3\n")
            binary = root / "bin"
            binary.mkdir()
            program = f"#!{sys.executable}\n" + r'''
import json, os, pathlib, sys
command = pathlib.Path(sys.argv[0]).name
args = sys.argv[1:]
with open(os.environ["COMMAND_LOG"], "a") as output:
    output.write(json.dumps([command] + args) + "\n")
if command == "git":
    if args[:3] == ["diff", "--cached", "--quiet"]:
        sys.exit(0 if os.environ.get("UNCHANGED") == "1" else 1)
    if args[:2] == ["ls-remote", "--heads"]:
        print("existing-ref" if os.environ.get("BRANCH_EXISTS") == "1" else "")
    if args[0] == "rev-parse":
        print("old-tree" if os.environ.get("BRANCH_DIFFERS") == "1" else "expected-tree")
    if args[0] == "write-tree":
        print("expected-tree")
elif args[:2] == ["pr", "list"]:
    print(os.environ.get("PR_COUNT", "0"))
elif args[:2] == ["pr", "create"]:
    sys.exit(int(os.environ.get("CREATE_EXIT", "0")))
'''
            for command in ("git", "gh"):
                path = binary / command
                path.write_text(program)
                path.chmod(0o755)
            log = root / "commands.jsonl"
            result = subprocess.run(
                ["bash", "-c", self.script], cwd=root, capture_output=True, text=True,
                env=dict(os.environ, PATH=str(binary) + os.pathsep + os.environ["PATH"],
                         COMMAND_LOG=str(log), **settings),
            )
            commands = [json.loads(line) for line in log.read_text().splitlines()]
            return result, commands

    def test_unchanged_or_existing_pr_does_not_push(self):
        for settings in ({"UNCHANGED": "1"}, {"PR_COUNT": "1"}):
            with self.subTest(settings=settings):
                result, commands = self.run_workflow(**settings)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertFalse(any(command[:2] == ["git", "push"] for command in commands))
                self.assertFalse(any(command[:3] == ["gh", "pr", "create"] for command in commands))

    def test_new_update_pushes_only_review_branch_and_opens_pr(self):
        result, commands = self.run_workflow()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(["git", "add", "--", "Formula/co.rb", "releases/latest.txt"], commands)
        self.assertIn(["git", "push", "origin", "HEAD:refs/heads/automation/co-v1.2.3"], commands)
        create = next(command for command in commands if command[:3] == ["gh", "pr", "create"])
        self.assertIn("--base", create)
        self.assertIn("main", create)

    def test_matching_orphan_branch_can_retry_pr_creation_without_push(self):
        result, commands = self.run_workflow(BRANCH_EXISTS="1")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(any(command[:2] == ["git", "push"] for command in commands))
        self.assertTrue(any(command[:3] == ["gh", "pr", "create"] for command in commands))

    def test_changed_orphan_branch_is_not_overwritten(self):
        result, commands = self.run_workflow(BRANCH_EXISTS="1", BRANCH_DIFFERS="1")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Existing update branch differs", result.stdout)
        self.assertFalse(any(command[:2] == ["git", "push"] for command in commands))
        self.assertFalse(any(command[:3] == ["gh", "pr", "create"] for command in commands))

    def test_pr_creation_failure_is_not_hidden(self):
        result, _ = self.run_workflow(CREATE_EXIT="1")
        self.assertNotEqual(result.returncode, 0)


if __name__ == "__main__":
    unittest.main()
