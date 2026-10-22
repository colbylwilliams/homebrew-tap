import copy
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from fixtures import FixtureRegistry, sync
import update_release as update


def git(root, *args):
    command = ["git", f"--git-dir={root}"] if root.name == "remote.git" else ["git"]
    result = subprocess.run(
        [*command, *args], cwd=root, text=True, capture_output=True,
        env=dict(os.environ, GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull),
    )
    if result.returncode:
        raise AssertionError(result.stderr)
    return result.stdout.strip()


class GitHub:
    """Fake only GitHub PR transport; refs, trees, leases, and merges use real Git."""

    def __init__(self, remote):
        self.remote = remote
        self.pr = None
        self.calls = []
        self.create_error = False
        self.overrides = {}
        self.record_merge = True

    def api(self, path, data=None):
        self.calls.append((path, data))
        if path.startswith("pulls?"):
            return [] if self.pr is None else [{"number": 1}]
        if data is not None:
            if self.create_error:
                raise sync.ReleaseError("GitHub HTTP 403: PR creation denied")
            self.pr = {
                "number": 1, "state": "open", "merged": False, "draft": False,
                "title": data["title"], "body": data["body"],
                "user": {"login": update.BOT_NAME},
                "head": {"ref": data["head"], "repo": {"full_name": update.REPOSITORY}},
                "base": {"ref": "main", "repo": {"full_name": update.REPOSITORY}},
            }
        result = copy.deepcopy(self.pr)
        result["head"]["sha"] = git(self.remote, "rev-parse", f"refs/heads/{result['head']['ref']}")
        result["base"]["sha"] = git(self.remote, "rev-parse", "refs/heads/main")
        if self.record_merge and result["state"] == "open":
            merged = subprocess.run(
                ["git", f"--git-dir={self.remote}", "merge-base", "--is-ancestor",
                 result["head"]["sha"], result["base"]["sha"]],
                cwd=self.remote, capture_output=True,
            ).returncode == 0
            if merged:
                result.update(merged=True, state="closed")
        result.update(self.overrides)
        return result


class LocalUpdate(update.Update):
    def __init__(self, root, github):
        super().__init__(root)
        self.github = github
        self.commands = []
        self.before_publish = None
        self.before_branch_push = None

    def run(self, *args, data=None):
        with patch.dict(os.environ, GIT_CONFIG_NOSYSTEM="1", GIT_CONFIG_GLOBAL=os.devnull):
            return super().run(*args, data=data)

    def git(self, *args):
        self.commands.append(args)
        if args[0] == "push":
            if args[-1].endswith(":refs/heads/main") and self.before_publish:
                self.before_publish()
            elif "automation/" in args[-1] and self.before_branch_push:
                self.before_branch_push()
        return super().git(*args)

    def api(self, path, data=None):
        return self.github.api(path, data)


class ReleaseUpdateTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.remote = self.root / "remote.git"
        git(self.root, "init", "--bare", "--initial-branch=main", str(self.remote))
        self.github = GitHub(self.remote)
        self.counter = 0
        work = self.checkout()
        (work.root / "README.md").write_text("Unrelated source must stay unchanged.\n")
        self.commit(work.root, "Initial source")
        git(work.root, "push", "origin", "HEAD:refs/heads/main")
        self.initial = git(work.root, "rev-parse", "HEAD")
        sync.write_release(work.root, sync.fetch_release(FixtureRegistry("1.2.2")))
        self.commit(work.root, "Previous release")
        git(work.root, "push", "origin", "HEAD:refs/heads/main")
        self.base = git(work.root, "rev-parse", "HEAD")

    def checkout(self, ref=None):
        self.counter += 1
        root = self.root / f"work{self.counter}"
        git(self.root, "clone", "--quiet", str(self.remote), str(root))
        git(root, "config", "user.name", update.BOT_NAME)
        git(root, "config", "user.email", update.BOT_EMAIL)
        if ref:
            git(root, "checkout", "--detach", ref)
        return LocalUpdate(root, self.github)

    def commit(self, root, message):
        git(root, "add", ".")
        git(root, "-c", "commit.gpgSign=false", "commit", "-qm", message)

    def prepare(self):
        return self.checkout().prepare(FixtureRegistry())

    def main_head(self):
        return git(self.remote, "rev-parse", "main")

    def advance_main(self, version=None):
        work = self.checkout()
        if version:
            sync.write_release(work.root, sync.fetch_release(FixtureRegistry(version)))
        else:
            (work.root / "README.md").write_text("Concurrent manual source change.\n")
        self.commit(work.root, "Concurrent main change")
        git(work.root, "push", "origin", "HEAD:refs/heads/main")
        return self.main_head()

    def change_branch(self, state, path="README.md"):
        work = self.checkout(state["head"])
        (work.root / path).write_text("Manual branch change.\n")
        self.commit(work.root, "Manual change")
        git(work.root, "push", "origin", f"HEAD:refs/heads/{update.Update.branch(state)}")
        return git(work.root, "rev-parse", "HEAD")

    def test_success_exact_two_file_merge_and_noop_retry(self):
        state = self.prepare()
        self.assertEqual(self.main_head(), self.base)
        work = self.checkout(state["base"])
        work.merge(state, "success")
        merged = self.main_head()
        self.assertEqual(git(work.root, "show", "-s", "--format=%P", merged),
                         f"{state['base']} {state['head']}")
        self.assertEqual(git(work.root, "rev-parse", f"{merged}^{{tree}}"), state["tree"])
        self.assertEqual(set(git(work.root, "diff", "--name-only", self.base, merged).splitlines()),
                         update.FILES)
        self.assertTrue(self.github.api("pulls/1")["merged"])
        calls = len(self.github.calls)
        self.assertIsNone(self.checkout().prepare(FixtureRegistry()))
        self.assertEqual(len(self.github.calls), calls)
        self.assertEqual(self.main_head(), merged)

    def test_identical_open_pr_retry_reuses_head_without_push(self):
        state = self.prepare()
        work = self.checkout()
        self.assertEqual(work.prepare(FixtureRegistry()), state)
        self.assertFalse(any(args[0] == "push" for args in work.commands))
        self.assertEqual(sum(data is not None for _, data in self.github.calls), 1)

    def test_failed_pr_creation_retries_matching_orphan_without_push(self):
        self.github.create_error = True
        with self.assertRaisesRegex(sync.ReleaseError, "403"):
            self.prepare()
        self.github.create_error = False
        work = self.checkout()
        state = work.prepare(FixtureRegistry())
        self.assertEqual(state["pr"], 1)
        self.assertFalse(any(args[0] == "push" for args in work.commands))

    def test_changed_orphan_branch_is_not_overwritten(self):
        self.github.create_error = True
        with self.assertRaises(sync.ReleaseError):
            self.prepare()
        self.github.create_error = False
        head = git(self.remote, "rev-parse", "automation/co-v1.2.3")
        changed = self.change_branch({"version": "1.2.3", "head": head})
        with self.assertRaisesRegex(sync.ReleaseError, "one commit"):
            self.prepare()
        self.assertEqual(git(self.remote, "rev-parse", "automation/co-v1.2.3"), changed)
        self.assertIsNone(self.github.pr)

    def test_branch_creation_race_does_not_adopt_or_overwrite_ref(self):
        work = self.checkout()
        work.before_branch_push = lambda: git(
            self.remote, "update-ref", "refs/heads/automation/co-v1.2.3", self.base)
        with self.assertRaisesRegex(sync.ReleaseError, "git push failed"):
            work.prepare(FixtureRegistry())
        self.assertEqual(git(self.remote, "rev-parse", "automation/co-v1.2.3"), self.base)
        self.assertIsNone(self.github.pr)

    def test_older_or_rewritten_release_is_rejected(self):
        for registry in (FixtureRegistry("1.2.1"), FixtureRegistry("1.2.2", distinct=True)):
            with self.subTest(version=registry.version):
                with self.assertRaisesRegex(sync.ReleaseError, "downgrade|immutable"):
                    self.checkout().prepare(registry)
                self.assertEqual(self.main_head(), self.base)
                self.assertIsNone(self.github.pr)

    def test_invalid_public_release_never_creates_branch_or_pr(self):
        registry = FixtureRegistry()
        registry.tag_override = b"{}"
        with self.assertRaisesRegex(sync.ReleaseError, "Version tag"):
            self.checkout().prepare(registry)
        self.assertEqual(git(self.remote, "for-each-ref", "--format=%(refname)", "refs/heads"),
                         "refs/heads/main")
        self.assertEqual(self.github.calls, [])

    def test_stale_preparation_and_publication_fail_after_main_advances(self):
        state = self.prepare()
        for version in (None, "1.3.0"):
            with self.subTest(version=version):
                latest = self.advance_main(version)
                with self.assertRaisesRegex(sync.ReleaseError, "Main advanced"):
                    self.checkout(state["base"]).merge(state, "success")
                self.assertEqual(self.main_head(), latest)
        with self.assertRaisesRegex(sync.ReleaseError, "downgrade"):
            self.prepare()

    def test_stale_existing_pr_is_not_rebased_or_adopted(self):
        self.prepare()
        latest = self.advance_main()
        with self.assertRaisesRegex(sync.ReleaseError, "one commit"):
            self.prepare()
        self.assertEqual(self.main_head(), latest)

    def test_late_main_advance_and_rewind_are_rejected_by_lease(self):
        state = self.prepare()
        for change in (
            lambda: self.advance_main("1.3.0"),
            lambda: git(self.remote, "update-ref", "refs/heads/main", self.initial),
        ):
            with self.subTest(change=change):
                git(self.remote, "update-ref", "refs/heads/main", self.base)
                work = self.checkout(state["base"])
                work.before_publish = change
                with self.assertRaisesRegex(sync.ReleaseError, "git push failed"):
                    work.merge(state, "success")
                self.assertNotEqual(self.main_head(), self.base)
                self.assertNotEqual(self.main_head(), state["head"])
                self.assertNotEqual(git(self.remote, "rev-parse", "main^{tree}"), state["tree"])

    def test_closed_and_modified_prs_fail_both_retry_and_merge(self):
        state = self.prepare()
        changes = [
            {"state": "closed"}, {"merged": True}, {"draft": True},
            {"title": "Manual PR"}, {"body": "Manually modified"},
            {"user": {"login": "someone-else"}},
            {"head": {"ref": update.Update.branch(state), "sha": state["head"],
                      "repo": {"full_name": "someone/fork"}}},
            {"base": {"ref": "other", "sha": state["base"],
                      "repo": {"full_name": update.REPOSITORY}}},
        ]
        for changeset in changes:
            with self.subTest(changes=changeset):
                self.github.overrides = changeset
                with self.assertRaises(sync.ReleaseError):
                    self.prepare()
                with self.assertRaises(sync.ReleaseError):
                    self.checkout(state["base"]).merge(state, "success")
                self.assertEqual(self.main_head(), self.base)

    def test_changed_head_is_never_validated_or_published_as_original(self):
        state = self.prepare()
        changed = self.change_branch(state)
        with self.assertRaisesRegex(sync.ReleaseError, "head changed"):
            self.checkout(state["base"]).merge(state, "success")
        with self.assertRaisesRegex(sync.ReleaseError, "one commit"):
            self.prepare()
        self.assertEqual(self.main_head(), self.base)
        self.assertEqual(git(self.remote, "rev-parse", update.Update.branch(state)), changed)

    def test_changed_head_after_final_check_cannot_publish_unvalidated_content(self):
        state = self.prepare()
        work = self.checkout(state["base"])
        work.before_publish = lambda: self.change_branch(state)
        with self.assertRaisesRegex(sync.ReleaseError, "identity or head changed"):
            work.merge(state, "success")
        self.assertEqual(git(self.remote, "rev-parse", "main^{tree}"), state["tree"])
        self.assertFalse(self.github.api("pulls/1")["merged"])

    def test_rewritten_single_commit_with_identical_tree_is_not_adopted(self):
        state = self.prepare()
        work = self.checkout(state["base"])
        work.fetch(update.Update.branch(state), state["head"])
        replacement = git(work.root, "commit-tree", state["tree"], "-p", state["base"],
                          "-m", "Manually rewritten commit with the same tree")
        git(work.root, "push", "origin", f"{replacement}:refs/heads/manual-object")
        git(self.remote, "update-ref", f"refs/heads/{update.Update.branch(state)}", replacement)
        with self.assertRaisesRegex(sync.ReleaseError, "PR was modified"):
            self.prepare()
        with self.assertRaisesRegex(sync.ReleaseError, "head changed"):
            work.merge(state, "success")
        self.assertEqual(self.main_head(), self.base)

    def test_closed_or_missing_branch_and_malformed_pr_fail_without_mutation(self):
        state = self.prepare()
        git(self.remote, "update-ref", "-d", f"refs/heads/{update.Update.branch(state)}")
        with self.assertRaisesRegex(sync.ReleaseError, "lost its branch"):
            self.prepare()
        with self.assertRaisesRegex(sync.ReleaseError, "head changed"):
            self.checkout(state["base"]).merge(state, "success")
        for pr in (None, [], {}, {"head": None}):
            with self.subTest(pr=pr), self.assertRaises(sync.ReleaseError):
                self.checkout(state["base"]).check_pr(pr, state)
        self.assertEqual(self.main_head(), self.base)

    def test_allowlist_and_generated_tree_are_independently_enforced(self):
        state = self.prepare()
        work = self.checkout(state["head"])
        (work.root / "README.md").write_text("Unexpected source update.\n")
        git(work.root, "add", ".")
        tree = git(work.root, "write-tree")
        head = git(work.root, "commit-tree", tree, "-p", state["base"], "-m", "Invalid")
        altered = dict(state, head=head)
        with self.assertRaisesRegex(sync.ReleaseError, "generated content"):
            work.check_commit(altered)
        altered["tree"] = tree
        with self.assertRaisesRegex(sync.ReleaseError, "exactly the two"):
            work.check_commit(altered)

    def test_generated_files_cannot_be_executable_or_symlinks(self):
        state = self.prepare()
        for mode in ("100755", "120000"):
            with self.subTest(mode=mode):
                work = self.checkout(state["head"])
                blob = git(work.root, "rev-parse", f"{state['head']}:Formula/co.rb")
                git(work.root, "update-index", "--cacheinfo", f"{mode},{blob},Formula/co.rb")
                tree = git(work.root, "write-tree")
                head = git(work.root, "commit-tree", tree, "-p", state["base"], "-m", "Bad mode")
                with self.assertRaisesRegex(sync.ReleaseError, "regular nonexecutable"):
                    work.check_commit(dict(state, head=head, tree=tree))

    def test_failed_cancelled_or_missing_validation_cannot_publish(self):
        state = self.prepare()
        for validation in ("failure", "cancelled", "skipped", "", None):
            with self.subTest(validation=validation):
                work = self.checkout(state["base"])
                with self.assertRaisesRegex(sync.ReleaseError, "validation did not succeed"):
                    work.merge(state, validation)
                self.assertFalse(any(args[0] == "push" for args in work.commands))
                self.assertEqual(self.main_head(), self.base)

    def test_merge_permission_or_ruleset_denial_is_actionable(self):
        state = self.prepare()
        hook = self.remote / "hooks/pre-receive"
        hook.write_text("#!/bin/sh\necho 'permission/ruleset denied main update' >&2\nexit 1\n")
        hook.chmod(0o755)
        with self.assertRaisesRegex(sync.ReleaseError, "permission/ruleset denied"):
            self.checkout(state["base"]).merge(state, "success")
        self.assertEqual(self.main_head(), self.base)
        self.assertFalse(self.github.api("pulls/1")["merged"])

    def test_missing_merge_record_is_not_silently_successful(self):
        state = self.prepare()
        self.github.record_merge = False
        with patch.object(update.time, "sleep"):
            with self.assertRaisesRegex(sync.ReleaseError, "merge recording is pending"):
                self.checkout(state["base"]).merge(state, "success")

    def test_malformed_or_ambiguous_github_responses_fail_closed(self):
        for response in ({}, None, [None], [{"number": True}], [{"number": 1}, {"number": 2}]):
            with self.subTest(response=response):
                work = self.checkout()
                with patch.object(work, "api", return_value=response):
                    with self.assertRaises(sync.ReleaseError):
                        work.prepare(FixtureRegistry())
                self.assertEqual(self.main_head(), self.base)
        client = update.Update(self.root)
        for response in ("not JSON", '{"a":1,"a":2}'):
            with self.subTest(response=response), patch.object(client, "run", return_value=response):
                with self.assertRaises(sync.ReleaseError):
                    client.api("pulls")

    def test_workflow_entrypoint_refuses_non_main_and_other_repositories(self):
        for ref, repo in (("refs/heads/review", update.REPOSITORY),
                          ("refs/heads/main", "someone/fork")):
            with self.subTest(ref=ref, repo=repo), patch.dict(
                os.environ, GITHUB_REF=ref, GITHUB_REPOSITORY=repo
            ), patch("sys.argv", ["update_release.py", "prepare"]), patch.object(update, "Update") as client:
                self.assertEqual(update.main(), 1)
                client.assert_not_called()


if __name__ == "__main__":
    unittest.main()
