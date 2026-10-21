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
latest **reviewed tap release**. Failed downloads, verification, extraction, or
staging leave the existing executable intact. The final replacement is an atomic
rename on the destination filesystem. Remove that executable to uninstall.

`co update` is a private-source rebuild command, **not a binary updater**. Public
binary users should use Homebrew upgrades or rerun this installer instead.

## One-time owner setup

1. Merge this setup PR in the public `colbylwilliams/homebrew-tap` repository.
2. In **Settings > Actions > General > Workflow permissions**, enable
   **Allow GitHub Actions to create and approve pull requests**. The workflow
   requests its own `contents: write` and `pull-requests: write` permissions; it
   never approves or merges. Repository/organization policy must permit these.
3. Publish the first stable `vMAJOR.MINOR.PATCH` from the source publishing
   workflow. Change **only** the GHCR package
   `ghcr.io/colbylwilliams/co/cli` to **Public** in its package settings.
   The source repository can remain private. Anonymous reads are deliberately
   required; a token with private access must not mask a visibility mistake.
4. Run **Actions > Sync public release > Run workflow**. Review and merge the
   resulting update PR containing `Formula/co.rb` and `releases/latest.txt`.
   Then smoke-test Homebrew and the public installer on real supported machines.

These settings and the first production release are owner actions; this repository
does not change package visibility, publish binaries, or merge PRs. The canonical
raw installer URL works as soon as this setup is on `main`; installation still
needs the generated release index and a public package. No Pages deployment is
needed.

## How release sync works

`Sync public release` runs daily and manually, pulling anonymously from
`ghcr.io/colbylwilliams/co/cli`. It uses only its own repository's `GITHUB_TOKEN`
to propose a reviewable update PR: no cross-repository PAT, dispatch secret, or
private-source checkout is needed.

The expected producer contract is:

| Item | Contract |
| --- | --- |
| Tags | Stable immutable `vMAJOR.MINOR.PATCH` (at most 64 characters including `v`); monotonically advancing `latest` |
| Manifest | One OCI 1.1 image manifest, `application/vnd.oci.image.manifest.v1+json` |
| Artifact type | `application/vnd.co.cli.v1` |
| Config | OCI empty config, `application/vnd.oci.empty.v1+json`, bytes `{}` |
| Annotations | `org.opencontainers.image.version=vX.Y.Z`, `org.opencontainers.image.revision=<40-character commit SHA>` |
| Archives | `co_X.Y.Z_{darwin,linux}_{amd64,arm64}.tar.gz`, media type `application/gzip` |
| Checksums | `SHA256SUMS`, media type `text/plain`, four SHA256 filename entries |
| Layer names | `org.opencontainers.image.title` annotations |
| Archive members | Exactly root `co` (regular file, mode `0755`) and `LICENSE` (regular file, mode `0644`) |

The generator checks that `latest` and its version tag have the same manifest
digest; validates all descriptors; downloads and checks every layer; compares
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

The index is a trust root reviewed in this public tap, not a signature: checksums
detect changed downloads, but do not protect against a compromised publisher or
tap maintainer. The formula and installer advance together when the update PR is
merged, not immediately when the producer changes `latest`.

The workflow never force-pushes or overwrites an existing update PR, including
closed PRs. Reopen a deliberately closed update PR if it should proceed. If a
push succeeded but PR creation failed, a rerun can reuse the branch only when
its entire tree still matches the generated update; otherwise inspect the branch
manually. Sync fails explicitly if no public release exists or metadata disagrees.
Scheduled workflows in public repositories may be disabled after inactivity;
manual dispatch remains available.

PRs created using `GITHUB_TOKEN` do **not** trigger `pull_request` workflows
automatically. Fixture checks run before the sync creates its PR; maintainers can
also dispatch **Checks** on the update branch. This limitation is intentional
rather than requiring a PAT.

## Development

Only the generator and tests need Python 3.9+; formula tests also need Ruby.
No third-party Python packages are needed.

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
selection/install/version-test behavior. Tests mock the transport; they neither
publish a release nor disable production TLS. CI runs them on macOS and Linux.
Real package download and installation remain first-release smoke tests, not
claims made by the fixture suite.

To generate a real candidate locally after the package is public:

```sh
python3 scripts/sync_release.py
```

Review both generated files together. Do not hand-edit checksums or commit a
fixture formula as a substitute for a published release.