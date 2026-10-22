# co public distribution

Install prebuilt `co` binaries without ORAS, Docker, Go, a GitHub login, or access
to the private source repository. Supports macOS 13+ and Linux on x86_64 and arm64.
Linux binaries are built with CGO disabled; no minimum Linux kernel has been
independently established.

**Bootstrap:** this setup intentionally contains no placeholder formula or
checksums. Installation becomes available only after the first public GHCR
release is validated and its generated `Formula/co.rb` and `releases/latest.txt`
are merged. Before then, Homebrew cannot find `co` and the installer fails on the
missing release index.

## Install

Once bootstrap is complete:

```sh
brew install colbylwilliams/tap/co
```

Without Homebrew, install through the public short-link:

```sh
curl -fsSL https://gh.io/co-install | sh
```

This is the standalone installer, not a Homebrew bootstrap script. The short-link
redirects to the canonical raw GitHub endpoint below; GitHub Pages is not required:

```text
https://raw.githubusercontent.com/colbylwilliams/homebrew-tap/main/install.sh
```

To download the complete script before running it:

```sh
(
  installer="$(mktemp)" || exit 1
  trap 'rm -f "$installer"' 0
  curl --proto '=https' --proto-redir '=https' --tlsv1.2 -fsSL \
    https://gh.io/co-install \
    -o "$installer" &&
  sh "$installer"
)
```

The download-first form does not execute an incomplete script or hide a failed
script download behind a pipe. The file can also be inspected before running,
and the raw URL can be used directly in either command.

The default destination is `$HOME/.local/bin/co`, without `sudo`. Add
`$HOME/.local/bin` to your `PATH` if necessary. To choose another **absolute**
directory, run the downloaded script with `CO_INSTALL_DIR=/your/bin sh "$installer"`.
Existing symlinks (including Homebrew-managed installations) and directory targets
are refused. The installer reports unsupported platforms and network, metadata,
checksum, extraction, and installation errors explicitly.

The installer needs `curl`, `tar`, `awk`, `sed`, and standard macOS/Linux utilities,
plus `shasum` or `sha256sum`; it does not need `jq` or Python. macOS version
detection uses `sw_vers`. On macOS, the architecture follows the running shell:
run a native arm64 shell rather than a Rosetta shell to select the arm64 build.

## Upgrade and uninstall

For Homebrew installations:

```sh
brew update
brew upgrade colbylwilliams/tap/co
```

For shell installations, rerun the installer to replace the executable with the
latest **verified tap release**. Failed downloads, verification, extraction, or
staging leave the existing executable intact. The final replacement is an atomic
rename on the destination filesystem. Remove that executable to uninstall.

`co update` is a private-source rebuild command, **not a binary updater**. Public
binary users should use Homebrew upgrades or rerun this installer instead.

## One-time owner setup

1. Review and merge the automatic-sync setup in the public
   `colbylwilliams/homebrew-tap` repository **before** enabling source dispatch.
   This authorizes automatic publication of verified generated release metadata
   only, not source, installer, workflow, or other code PRs.
2. In **Settings > Actions > General > Workflow permissions**, enable
   **Allow GitHub Actions to create and approve pull requests**. The workflow
   requests its own `contents: write` and `pull-requests: write` permissions.
   It never approves PRs. Repository/organization policy must permit these
   permissions and the workflow's fast-forward merge-commit update of `main`.
   Rules requiring a PR-merge API call, reviews, or other unsatisfied checks
   can block publication; the workflow fails rather than bypassing them.
3. Publish the first stable `vMAJOR.MINOR.PATCH` from the source publishing
   workflow. Change **only** the GHCR package
   `ghcr.io/colbylwilliams/co/cli` to **Public** in its package settings.
   The source repository can remain private. Anonymous reads are deliberately
   required; a token with private access must not mask a visibility mistake.
4. In the **private source repository only**, configure `HOMEBREW_TAP_TOKEN` as a
   fine-grained PAT scoped to this public tap with **Actions: read and write**.
   No Contents write, Pull requests write, or private-source access is needed
   on that token. Enable the source workflow's dispatch after successful public
   GHCR publication. It sends:

   ```http
   POST /repos/colbylwilliams/homebrew-tap/actions/workflows/sync-release.yml/dispatches

   {"ref":"main"}
   ```

   No inputs, release files, or credentials are passed to the tap. The tap
   resolves public `latest` itself; dispatch acceptance does not mean the tap
   has finished updating. Monitor **Sync public release** for the outcome.
5. For bootstrap or recovery, run **Actions > Sync public release > Run
   workflow** on `main`. It creates a visible generated update PR, validates
   its exact commit on Linux/macOS, and automatically publishes the verified
   two-file update. Smoke-test Homebrew and the installer on real supported
   machines after activation.

These settings and the first production release are owner actions; this repository
does not change package visibility or publish binaries. It automatically merges
only its verified generated metadata updates. The canonical
raw installer URL works as soon as this setup is on `main`; installation still
needs the generated release index and a public package. No Pages deployment is
needed.

## How release sync works

`Sync public release` runs on source dispatch immediately after publication,
with daily and manual runs as recovery paths, pulling anonymously from
`ghcr.io/colbylwilliams/co/cli`. It uses only its own repository's `GITHUB_TOKEN`
to create and publish a visible generated update PR. No PAT, dispatch secret, or
private-source checkout is stored or needed in this public repository.

The expected producer contract is:

| Item | Contract |
| --- | --- |
| Registry tags | Stable immutable `MAJOR.MINOR.PATCH` without `v`; monotonically advancing `MAJOR`, `MAJOR.MINOR`, and `latest` aliases |
| Source Git tag / workflow input | `vMAJOR.MINOR.PATCH` (unchanged) |
| Manifest | One OCI 1.1 image manifest, `application/vnd.oci.image.manifest.v1+json` |
| Artifact type | `application/vnd.co.cli.v1` |
| Config | OCI empty config, `application/vnd.oci.empty.v1+json`, bytes `{}` |
| Annotations | `org.opencontainers.image.version=vX.Y.Z` (at most 64 characters including `v`), `org.opencontainers.image.revision=<40-character commit SHA>` |
| Archives | `co_X.Y.Z_{darwin,linux}_{amd64,arm64}.tar.gz`, media type `application/gzip` |
| Checksums | `SHA256SUMS`, media type `text/plain`, four SHA256 filename entries |
| Layer names | `org.opencontainers.image.title` annotations |
| Archive members | Exactly root `co` (regular file, mode `0755`) and `LICENSE` (regular file, mode `0644`) |

For `v0.1.0`, the immutable registry tag is `0.1.0`; aliases `0`, `0.1`, and
`latest` initially point to that exact manifest. Each alias advances independently:
`MAJOR` tracks the newest release within that major, `MAJOR.MINOR` within that
minor, and `latest` overall. Publishing an older release cannot roll an alias
backwards. Only registry tags omit `v`: the OCI version annotation and binary
`--version` output retain `vX.Y.Z`.

The generator validates the `vX.Y.Z` annotation, then checks that `latest` and
the corresponding unprefixed `X.Y.Z` tag have the same manifest digest. It does
not resolve the major/minor aliases or fall back to a prefixed registry tag.
It validates all descriptors; downloads and checks every layer; compares
`SHA256SUMS` with the archive digests; and checks archive contents without
extracting paths. Archives are limited to 128 MiB compressed and individual
members to 256 MiB. Downgrades and changed content at a previously synced version
are rejected. Identical retries are safe.

The formula uses Homebrew's built-in
[`CurlGitHubPackagesDownloadStrategy`](https://docs.brew.sh/rubydoc/CurlGitHubPackagesDownloadStrategy.html),
immutable GHCR blob URLs, and per-platform SHA256 checksums. Homebrew identifies
the extensionless gzip/tar blobs by content. No custom download strategy or user
credentials are required.

The generated index has exactly eight lines:

```text
co-release-v1
version X.Y.Z
manifest sha256:<manifest digest>
revision <source commit SHA>
darwin amd64 <archive SHA256> <archive byte count>
darwin arm64 <archive SHA256> <archive byte count>
linux amd64 <archive SHA256> <archive byte count>
linux arm64 <archive SHA256> <archive byte count>
```

The shell installer always reads the index from this public repository's `main`
branch, even when the installer is reached through a short-link. It validates
every row, gets an anonymous GHCR pull token, and downloads only the selected
immutable blob over HTTPS. It verifies its byte count and SHA256, rejects
unexpected archive names and nonregular entries, and streams `co` into a private
staging directory beside the destination before replacing it. It never evaluates
remote metadata or unpacks archive paths. Curl configuration and tar environment
options cannot override its download/extraction policy. Temporary files are
cleaned on completion and catchable signals.

The index is a trust root in this public tap, not a signature. Automatic sync
explicitly trusts the public GHCR publisher to choose the next stable release;
there is no longer a human review gate for each generated version. Checksums and
layout checks detect changed or malformed downloads, not malicious binaries from
a compromised publisher or tap maintainer. Source/workflow/installer code changes
still require normal review. The formula and installer index advance together
only after the sync's exact-head validation and publication succeed, not merely
when the producer changes `latest`.

One non-cancelling `sync-public-release` concurrency group covers preparation,
Linux/macOS validation, and publication. GitHub does not guarantee dispatch
ordering and may replace pending runs; each admitted run resolves `latest` anew.
The generator pins a single release and
the workflow records its base, head, and entire generated tree. It permits exactly
`Formula/co.rb` and `releases/latest.txt`, both regular nonexecutable files, in a
single commit on the prepared `main`. It checks the PR's repository, author, base,
head, state, title, and recorded content before publishing.

The REST PR merge endpoint can guard the head SHA but cannot compare-and-swap
the base SHA. Instead, the workflow constructs a two-parent merge commit with
the verified tree and prepared base, then pushes it with an explicit
`--force-with-lease=refs/heads/main:<prepared-base>`. Despite the flag's name,
this cannot rewrite history: the required current base is the commit's first
parent, so the update is always a fast-forward. The lease rejects concurrent
advances **and rewinds**, including newer releases and manual source edits.
Candidate branch creation uses an empty expected-ref lease; existing branches
are never pushed or overwritten. GitHub records the visible PR as merged when
its exact head becomes reachable from `main`, and the workflow verifies this.

The Git ref update and PR metadata are **not** one atomic transaction. A PR
closed or edited after the final API check can leave the already validated head
published while the workflow reports a PR-recording failure. Such a race cannot
substitute changed/unvalidated branch contents. Inspect the failed run and PR;
the workflow never rolls back a published release to hide a failure.

Identical retries reuse only the exact open generated PR, or an orphaned branch
with the exact generated tree and prepared parent after PR creation failed.
Closed, edited, ambiguous, or stale PRs/branches fail visibly instead of being
silently adopted, rebased, or replaced. Reopen an intentionally closed PR only
after inspecting it; a stale base or modified candidate requires owner
reconciliation. No-op runs leave already-published metadata untouched.
Missing permissions, blocked pushes, failed validation, malformed responses, and
missing merge recording are actionable failures. Sync also fails explicitly if
no public release exists or metadata disagrees.
Scheduled workflows in public repositories may be disabled after inactivity;
manual dispatch remains available.

PRs created using `GITHUB_TOKEN` do **not** trigger `pull_request` workflows
automatically, and token-created pushes do not trigger another push CI run.
Sync therefore calls the reusable **Checks** workflow in the same run, checking
out and asserting the exact generated head. Both Linux and macOS must pass,
including real Homebrew parsing/extraction on macOS, before publication.
The check jobs have read-only permissions; only preparation/publication jobs
receive repository write permissions. Failed or cancelled checks cannot reach
publication. Maintainers can still dispatch **Checks** independently, but that
does not authorize publication or replace the same-run gate.

## Development

Only the generator and tests need Python 3.9+; formula tests also need Ruby.
Publication tests also need Git and use isolated local bare repositories, never
production refs or live GitHub mutations. No third-party Python packages are needed.

```sh
sh -n install.sh
python3 -m unittest discover -s tests -v
```

With Homebrew already installed, the following also loads the generated formula
in real Homebrew for all four platform selections, checking the actual GHCR
strategy and extraction of extensionless archives. macOS CI enables this check.

```sh
TEST_WITH_HOMEBREW=1 python3 -m unittest discover -s tests -v
```

Fixtures exercise all supported platforms, malformed metadata and tokens, failed
downloads, digest/size mismatches, unsafe archives, installation-failure
preservation, release monotonicity, TLS redirects, and generated formula
selection/install/version-test behavior. Strict tag fixtures verify the bare
`0.1.0` lookup, unchanged manifest/blob bytes and eight-line index, byte-exact
manifest equality, and identical retry output. Tests mock the transport; they neither
publish a release nor disable production TLS. CI runs them on macOS and Linux.
Publication fixtures cover successful/no-op/retry updates, exact generated trees
and allowlists, changed/closed PRs and branches, failed validation and permissions,
and real Git lease rejection of concurrent main advances and rewinds.
Real package download and installation remain first-release smoke tests, not
claims made by the fixture suite.

To generate a real candidate locally after the package is public:

```sh
python3 scripts/sync_release.py
```

Review both generated files together. Do not hand-edit checksums or commit a
fixture formula as a substitute for a published release.