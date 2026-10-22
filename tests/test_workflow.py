import json
import subprocess
import unittest

from fixtures import ROOT


def workflow(name):
    result = subprocess.run(
        ["ruby", "-ryaml", "-rjson", "-e",
         "puts JSON.generate(YAML.load_file(ARGV[0]))",
         str(ROOT / ".github/workflows" / name)],
        check=True, capture_output=True, text=True,
    )
    return json.loads(result.stdout)


class UpdateWorkflowTests(unittest.TestCase):
    def test_single_non_cancelling_lock_and_unchanged_dispatch_contract(self):
        sync = workflow("sync-release.yml")
        self.assertEqual(sync["concurrency"],
                         {"group": "sync-public-release", "cancel-in-progress": False})
        events = sync.get("on", sync.get("true"))
        self.assertEqual(set(events), {"workflow_dispatch", "schedule"})
        self.assertIsNone(events["workflow_dispatch"])
        self.assertEqual(sync["permissions"], {"contents": "read"})

    def test_generated_head_validation_gates_publication_in_same_run(self):
        jobs = workflow("sync-release.yml")["jobs"]
        validate, publish = jobs["validate"], jobs["publish"]
        self.assertEqual(validate["needs"], "sync")
        self.assertEqual(validate["uses"], "./.github/workflows/checks.yml")
        self.assertEqual(validate["with"]["ref"], "${{ needs.sync.outputs.head }}")
        self.assertEqual(validate["permissions"], {"contents": "read"})
        self.assertEqual(publish["needs"], ["sync", "validate"])
        for job in (validate, publish):
            self.assertEqual(job["if"], "needs.sync.outputs.head != ''")
        self.assertEqual(publish["steps"][0]["with"]["ref"], "${{ needs.sync.outputs.base }}")
        self.assertEqual(publish["steps"][-1]["env"]["VALIDATION_RESULT"],
                         "${{ needs.validate.result }}")
        for key in ("BASE", "HEAD", "TREE", "PR", "VERSION"):
            self.assertEqual(publish["steps"][-1]["env"][f"UPDATE_{key}"],
                             "${{ needs.sync.outputs." + key.lower() + " }}")

    def test_reusable_checks_preserve_both_platforms_and_actual_homebrew(self):
        checks = workflow("checks.yml")
        events = checks.get("on", checks.get("true"))
        self.assertEqual(events["workflow_call"]["inputs"]["ref"]["type"], "string")
        self.assertTrue(events["workflow_call"]["inputs"]["ref"]["required"])
        job = checks["jobs"]["test"]
        self.assertEqual(job["strategy"]["matrix"]["os"], ["ubuntu-latest", "macos-latest"])
        self.assertEqual(job["steps"][0]["with"]["ref"], "${{ inputs.ref || github.sha }}")
        self.assertEqual(job["steps"][1]["env"]["EXPECTED_HEAD"], "${{ inputs.ref || github.sha }}")
        self.assertIn('test "$(git rev-parse HEAD)" = "$EXPECTED_HEAD"', job["steps"][1]["run"])
        self.assertIn("runner.os == 'macOS'", job["steps"][-1]["env"]["TEST_WITH_HOMEBREW"])
        self.assertIn("python3 -m unittest discover -s tests -v", job["steps"][-1]["run"])
        self.assertEqual(checks["permissions"], {"contents": "read"})


if __name__ == "__main__":
    unittest.main()
