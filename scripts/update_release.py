#!/usr/bin/env python3
"""Prepare and publish only verified, generated release metadata."""

import argparse
import json
import os
import re
import subprocess
import sys
import time
import urllib.parse
from pathlib import Path

import sync_release as sync

FILES = {"Formula/co.rb", "releases/latest.txt"}
REPOSITORY = "colbylwilliams/homebrew-tap"
SHA = re.compile(r"[0-9a-f]{40}")
BOT_NAME = "github-actions[bot]"
BOT_EMAIL = "41898282+github-actions[bot]@users.noreply.github.com"


class Update:
    def __init__(self, root):
        self.root = root

    def run(self, *args, data=None):
        result = subprocess.run(args, cwd=self.root, input=data, text=True,
                                capture_output=True)
        sync.require(result.returncode == 0,
                     f"{args[0]} {args[1]} failed: {result.stderr.strip()}")
        return result.stdout.strip()

    def git(self, *args):
        return self.run("git", *args)

    def api(self, path, data=None):
        args = ["gh", "api", "--method", "GET" if data is None else "POST",
                f"repos/{REPOSITORY}/{path}"]
        if data is not None:
            args.extend(["--input", "-"])
        raw = self.run(*args, data=None if data is None else json.dumps(data))
        try:
            return json.loads(raw, object_pairs_hook=sync.unique_object)
        except (ValueError, UnicodeError) as error:
            raise sync.ReleaseError(f"Invalid GitHub JSON: {error}") from error

    def remote(self, branch):
        ref = f"refs/heads/{branch}"
        result = self.git("ls-remote", "--heads", "origin", ref)
        if not result:
            return None
        fields = result.split()
        sync.require(len(fields) == 2 and SHA.fullmatch(fields[0]) and fields[1] == ref,
                     f"Malformed remote ref for {branch}")
        return fields[0]

    def fetch(self, branch, expected):
        self.git("fetch", "--no-tags", "origin", f"refs/heads/{branch}")
        sync.require(self.git("rev-parse", "FETCH_HEAD") == expected,
                     f"{branch} changed during fetch; inspect and rerun")

    def clean(self):
        sync.require(not self.git("status", "--porcelain"),
                     "Working tree must be clean")

    def identity(self):
        self.git("config", "user.name", BOT_NAME)
        self.git("config", "user.email", BOT_EMAIL)

    @staticmethod
    def branch(state):
        return f"automation/co-v{state['version']}"

    @staticmethod
    def title(state):
        return f"Update co to {state['version']}"

    @staticmethod
    def body(state):
        return (
            "Automatically generated from anonymously verified public GHCR metadata. "
            "All four archives, checksums, immutable version tag, and archive layouts "
            "were validated. Only Formula/co.rb and releases/latest.txt may change.\n\n"
            "The sync run validates this exact head on Linux and macOS (including "
            "Homebrew) before a leased, fast-forward-only publication to main. "
            "No approval or automatic merge of source/workflow changes is authorized.\n\n"
            f"Base: `{state['base']}`\nHead: `{state['head']}`\nTree: `{state['tree']}`"
        )

    def check_state(self, state):
        sync.require(isinstance(state, dict), "Malformed update state")
        for field in ("base", "head", "tree"):
            sync.require(isinstance(state.get(field), str) and SHA.fullmatch(state[field]),
                         f"Invalid update {field}")
        version = state.get("version")
        sync.require(isinstance(version, str) and len(version) <= 63 and
                     sync.VERSION.fullmatch(version), "Invalid update version")
        sync.require(type(state.get("pr")) is int and state["pr"] > 0, "Invalid update PR")

    def check_commit(self, state):
        head, base = state["head"], state["base"]
        sync.require(self.git("show", "-s", "--format=%P", head) == base,
                     "Update must be one commit on the exact prepared main; inspect stale/modified branch")
        sync.require(self.git("rev-parse", f"{head}^{{tree}}") == state["tree"],
                     "Update tree differs from verified generated content")
        changed = self.git("diff", "--name-only", "--no-renames", "-z", base, head).split("\0")
        sync.require(set(changed) - {""} == FILES, "Update must change exactly the two generated files")
        for path in FILES:
            entry = self.git("ls-tree", head, "--", path).split()
            sync.require(len(entry) == 4 and entry[:2] == ["100644", "blob"] and entry[3] == path,
                         f"Generated file must be a regular nonexecutable blob: {path}")

    def check_pr(self, pr, state, published=False):
        sync.require(isinstance(pr, dict), "Malformed GitHub PR response")
        head, base, user = pr.get("head"), pr.get("base"), pr.get("user")
        sync.require(all(isinstance(value, dict) for value in (head, base, user)),
                     "Malformed GitHub PR identity")
        sync.require(isinstance(head.get("repo"), dict) and isinstance(base.get("repo"), dict),
                     "Missing GitHub PR repository")
        sync.require(type(pr.get("number")) is int and pr["number"] == state["pr"] and
                     head["repo"].get("full_name") == REPOSITORY and
                     base["repo"].get("full_name") == REPOSITORY and
                     head.get("ref") == self.branch(state) and head.get("sha") == state["head"] and
                     base.get("ref") == "main" and user.get("login") == BOT_NAME,
                     "PR identity or head changed; refusing unrelated/modified PR")
        sync.require(pr.get("title") == self.title(state) and pr.get("body") == self.body(state) and
                     pr.get("draft") is False,
                     "Existing update PR was modified; inspect it rather than adopting it")
        sync.require(type(pr.get("merged")) is bool, "Malformed GitHub PR merged status")
        if published:
            sync.require((pr.get("state"), pr.get("merged")) in
                         (("open", False), ("closed", True)),
                         "Published commit, but update PR was closed without merging; inspect it")
        else:
            sync.require(pr.get("state") == "open" and pr.get("merged") is False and
                         base.get("sha") == state["base"],
                         "Update PR is closed/merged or its base changed; inspect and rerun")

    def prepare(self, registry):
        self.clean()
        base = self.remote("main")
        sync.require(base is not None and self.git("rev-parse", "HEAD") == base,
                     "Checkout no longer matches main; rerun sync")
        registry.authenticate()
        release = sync.fetch_release(registry)
        sync.write_release(self.root, release)
        self.git("add", "--", *sorted(FILES))
        tree = self.git("write-tree")
        if tree == self.git("rev-parse", f"{base}^{{tree}}"):
            print("The tap already matches the verified public release.")
            return None
        state = {"base": base, "version": release[0], "tree": tree}
        branch = self.branch(state)
        query = urllib.parse.urlencode(
            {"state": "all", "head": f"{REPOSITORY.split('/')[0]}:{branch}", "per_page": 100})
        prs = self.api(f"pulls?{query}")
        sync.require(isinstance(prs, list) and len(prs) <= 1,
                     "Ambiguous/malformed update PR list; inspect all PRs for this branch")
        head = self.remote(branch)
        sync.require(not prs or head is not None, "Existing update PR lost its branch; inspect it")
        if head:
            self.fetch(branch, head)
        else:
            self.identity()
            head = self.git("commit-tree", tree, "-p", base, "-m", self.title(state))
        state["head"] = head
        self.check_commit(state)
        if prs:
            sync.require(isinstance(prs[0], dict) and type(prs[0].get("number")) is int,
                         "Malformed update PR list entry")
            state["pr"] = prs[0]["number"]
            self.check_state(state)
            self.check_pr(self.api(f"pulls/{state['pr']}"), state)
        else:
            sync.require(self.remote("main") == base, "Main advanced; rerun sync")
            # Supplying an empty expected ref creates only a missing branch, never overwrites one.
            if not self.remote(branch):
                self.git("push", f"--force-with-lease=refs/heads/{branch}:",
                         "origin", f"{head}:refs/heads/{branch}")
            sync.require(self.remote(branch) == head, "Update branch changed; inspect it")
            pr = self.api("pulls", {"base": "main", "head": branch,
                                    "title": self.title(state), "body": self.body(state)})
            sync.require(isinstance(pr, dict) and type(pr.get("number")) is int,
                         "Malformed PR creation response")
            state["pr"] = pr["number"]
            self.check_state(state)
            self.check_pr(self.api(f"pulls/{state['pr']}"), state)
        return state

    def merge(self, state, validation):
        self.check_state(state)
        sync.require(validation == "success", "Exact-head Linux/macOS validation did not succeed")
        self.clean()
        sync.require(self.git("rev-parse", "HEAD") == state["base"],
                     "Merge checkout must be the prepared base")
        sync.require(self.remote("main") == state["base"],
                     "Main advanced after preparation; refusing stale publication")
        branch = self.branch(state)
        sync.require(self.remote(branch) == state["head"], "Update branch head changed")
        self.fetch(branch, state["head"])
        self.check_commit(state)
        self.check_pr(self.api(f"pulls/{state['pr']}"), state)
        self.identity()
        commit = self.git("commit-tree", state["tree"], "-p", state["base"], "-p", state["head"],
                          "-m", f"Merge pull request #{state['pr']} from {REPOSITORY.split('/')[0]}/{branch}"
                          f"\n\n{self.title(state)}")
        # The REST merge API cannot compare-and-swap the base. This lease also
        # catches rewinds; the fixed first parent guarantees a fast-forward, not a rewrite.
        self.git("push", f"--force-with-lease=refs/heads/main:{state['base']}",
                 "origin", f"{commit}:refs/heads/main")
        published = self.remote("main")
        sync.require(published is not None, "Published main is missing")
        self.fetch("main", published)
        self.git("merge-base", "--is-ancestor", commit, published)
        for attempt in range(6):
            pr = self.api(f"pulls/{state['pr']}")
            self.check_pr(pr, state, published=True)
            if pr.get("merged") is True:
                print(f"Published co v{state['version']} via PR #{state['pr']} ({commit}).")
                return
            if attempt < 5:
                time.sleep(5)
        raise sync.ReleaseError("Main was published but PR merge recording is pending; inspect GitHub")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=("prepare", "merge"))
    args = parser.parse_args()
    try:
        sync.require(os.environ.get("GITHUB_REF") == "refs/heads/main" and
                     os.environ.get("GITHUB_REPOSITORY") == REPOSITORY,
                     "Release automation must run on this repository's main")
        update = Update(Path(__file__).resolve().parents[1])
        if args.operation == "prepare":
            state = update.prepare(sync.Registry())
            if state:
                with open(os.environ["GITHUB_OUTPUT"], "a", encoding="ascii") as output:
                    for key, value in state.items():
                        output.write(f"{key}={value}\n")
        else:
            state = {key: os.environ.get(f"UPDATE_{key.upper()}", "")
                     for key in ("base", "head", "tree", "version", "pr")}
            sync.require(state["pr"].isdigit(), "Invalid update PR number")
            state["pr"] = int(state["pr"])
            update.merge(state, os.environ.get("VALIDATION_RESULT"))
    except (sync.ReleaseError, OSError, UnicodeError, KeyError) as error:
        print(f"Release update failed: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
