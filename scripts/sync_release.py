#!/usr/bin/env python3
"""Validate the public OCI release and generate the tap's pinned install metadata."""

import argparse
import hashlib
import io
import json
import re
import sys
import tarfile
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

PACKAGE = "colbylwilliams/co/cli"
REGISTRY = f"https://ghcr.io/v2/{PACKAGE}"
MANIFEST_TYPE = "application/vnd.oci.image.manifest.v1+json"
PLATFORMS = (("darwin", "amd64"), ("darwin", "arm64"),
             ("linux", "amd64"), ("linux", "arm64"))
VERSION = re.compile(r"(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)\.(0|[1-9][0-9]*)")
DIGEST = re.compile(r"sha256:[0-9a-f]{64}")
MAX_ARCHIVE = 128 * 1024 * 1024
MAX_BINARY = 256 * 1024 * 1024


class ReleaseError(Exception):
    pass


def require(condition, message):
    if not condition:
        raise ReleaseError(message)


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        require(key not in result, f"Duplicate JSON key: {key}")
        result[key] = value
    return result


def read_json(data):
    try:
        result = json.loads(data, object_pairs_hook=unique_object)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ReleaseError(f"Invalid registry JSON: {error}") from error
    require(isinstance(result, dict), "Expected a JSON object")
    return result


def digest(data):
    return "sha256:" + hashlib.sha256(data).hexdigest()


class SecureRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, msg, headers, newurl):
        require(urllib.parse.urlsplit(newurl).scheme == "https",
                "Registry attempted a non-HTTPS redirect")
        redirected = super().redirect_request(request, fp, code, msg, headers, newurl)
        if redirected and urllib.parse.urlsplit(request.full_url).netloc != urllib.parse.urlsplit(newurl).netloc:
            redirected.remove_header("Authorization")
        return redirected


class Registry:
    def __init__(self):
        self.opener = urllib.request.build_opener(SecureRedirect())
        self.token = None

    def request(self, url, limit, accept=None):
        headers = {"User-Agent": "co-public-tap-sync"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        if accept:
            headers["Accept"] = accept
        try:
            with self.opener.open(urllib.request.Request(url, headers=headers), timeout=60) as response:
                data = response.read(limit + 1)
                require(len(data) <= limit, f"Registry response exceeds {limit} bytes")
                return data
        except urllib.error.HTTPError as error:
            hint = (" Publish a stable release and make the GHCR package public first."
                    if error.code in (401, 403, 404) else "")
            raise ReleaseError(f"GHCR returned HTTP {error.code}.{hint}") from error
        except (urllib.error.URLError, TimeoutError, OSError) as error:
            raise ReleaseError(f"Registry download failed: {error}") from error

    def authenticate(self):
        query = urllib.parse.urlencode({"service": "ghcr.io", "scope": f"repository:{PACKAGE}:pull"})
        response = read_json(self.request(f"https://ghcr.io/token?{query}", 65536))
        token = response.get("token")
        require(isinstance(token, str) and re.fullmatch(r"[A-Za-z0-9._~+/=-]+", token),
                "Invalid GHCR anonymous token response")
        self.token = token

    def manifest(self, reference):
        return self.request(f"{REGISTRY}/manifests/{reference}", 65536, MANIFEST_TYPE)

    def blob(self, descriptor, limit):
        data = self.request(f"{REGISTRY}/blobs/{descriptor['digest']}", limit)
        require(len(data) == descriptor["size"], "Blob size does not match its descriptor")
        require(digest(data) == descriptor["digest"], "Blob checksum does not match its descriptor")
        return data


def validate_descriptor(value, media_type, limit):
    require(isinstance(value, dict), "Missing OCI descriptor")
    require(value.get("mediaType") == media_type, f"Expected descriptor media type {media_type}")
    require(isinstance(value.get("digest"), str) and DIGEST.fullmatch(value["digest"]),
            "Invalid SHA256 descriptor")
    require(type(value.get("size")) is int and 0 < value["size"] <= limit, "Invalid descriptor size")


def validate_archive(data):
    try:
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as archive:
            names = set()
            for member in archive:
                require(member.name in {"co", "LICENSE"} and member.name not in names,
                        "Archive must contain exactly root co and LICENSE")
                names.add(member.name)
                require(member.isfile() and not member.issparse(), "Archive entries must be regular files")
                expected_mode = 0o755 if member.name == "co" else 0o644
                require(member.mode == expected_mode, f"Unexpected mode for {member.name}")
                require(0 < member.size <= MAX_BINARY, "Invalid uncompressed archive entry size")
                # Reading every member detects truncated payloads, not just valid headers.
                with archive.extractfile(member) as source:
                    remaining = member.size
                    while remaining:
                        block = source.read(min(remaining, 1024 * 1024))
                        require(block, "Truncated archive payload")
                        remaining -= len(block)
            require(names == {"co", "LICENSE"}, "Archive must contain exactly root co and LICENSE")
    except (tarfile.TarError, OSError, EOFError) as error:
        raise ReleaseError(f"Invalid release archive: {error}") from error


def fetch_release(registry):
    raw = registry.manifest("latest")
    manifest = read_json(raw)
    require(manifest.get("schemaVersion") == 2 and manifest.get("mediaType") == MANIFEST_TYPE,
            "Expected one OCI image manifest, not an image index")
    require(manifest.get("artifactType") == "application/vnd.co.cli.v1", "Unexpected OCI artifact type")
    annotations = manifest.get("annotations")
    require(isinstance(annotations, dict), "Missing release annotations")
    tag = annotations.get("org.opencontainers.image.version", "")
    require(isinstance(tag, str) and len(tag) <= 64 and tag.startswith("v") and VERSION.fullmatch(tag[1:]),
            "Release must have a stable vMAJOR.MINOR.PATCH version")
    version = tag[1:]
    revision = annotations.get("org.opencontainers.image.revision", "")
    require(isinstance(revision, str) and re.fullmatch(r"[0-9a-f]{40}", revision),
            "Release must have a full source commit revision")
    require(digest(registry.manifest(version)) == digest(raw), "Version tag does not match latest manifest")
    config = manifest.get("config")
    validate_descriptor(config, "application/vnd.oci.empty.v1+json", 2)
    require(config["size"] == 2 and config["digest"] == digest(b"{}"), "Expected OCI empty config")
    layers = manifest.get("layers")
    require(isinstance(layers, list) and len(layers) == 5, "Expected exactly five release layers")
    by_name = {}
    for layer in layers:
        require(isinstance(layer, dict), "Invalid layer descriptor")
        labels = layer.get("annotations")
        require(isinstance(labels, dict), "Missing layer title")
        name = labels.get("org.opencontainers.image.title")
        require(isinstance(name, str) and name not in by_name, "Missing or duplicate layer title")
        by_name[name] = layer
    names = [f"co_{version}_{os}_{arch}.tar.gz" for os, arch in PLATFORMS]
    require(set(by_name) == set(names + ["SHA256SUMS"]), "Unexpected release layer names")
    validate_descriptor(by_name["SHA256SUMS"], "text/plain", 4096)
    try:
        checksums = registry.blob(by_name["SHA256SUMS"], 4096).decode("ascii").splitlines()
    except UnicodeDecodeError as error:
        raise ReleaseError("SHA256SUMS must be ASCII") from error
    require(len(checksums) == 4, "SHA256SUMS must contain exactly four checksums")
    expected = {}
    for line in checksums:
        match = re.fullmatch(r"([0-9a-f]{64}) [ *](co_[0-9.]+_(?:darwin|linux)_(?:amd64|arm64)\.tar\.gz)", line)
        require(match is not None, "Malformed SHA256SUMS entry")
        checksum, name = match.groups()
        require(name not in expected, "Duplicate SHA256SUMS entry")
        expected[name] = "sha256:" + checksum
    require(set(expected) == set(names), "SHA256SUMS filenames do not match release")
    for name in names:
        layer = by_name[name]
        validate_descriptor(layer, "application/gzip", MAX_ARCHIVE)
        require(expected[name] == layer["digest"], "SHA256SUMS disagrees with manifest")
        validate_archive(registry.blob(layer, MAX_ARCHIVE))
    return version, revision, digest(raw), by_name


def render_index(release):
    version, revision, manifest, layers = release
    rows = ["co-release-v1", f"version {version}", f"manifest {manifest}", f"revision {revision}"]
    for os, arch in PLATFORMS:
        layer = layers[f"co_{version}_{os}_{arch}.tar.gz"]
        rows.append(f"{os} {arch} {layer['digest'][7:]} {layer['size']}")
    return "\n".join(rows) + "\n"


def render_formula(release):
    version, _, _, layers = release
    lines = [
        "# Generated by scripts/sync_release.py; do not edit release metadata by hand.",
        "class Co < Formula",
        '  desc "Copilot workflow command-line tool"',
        '  homepage "https://github.com/colbylwilliams/homebrew-tap"',
        f'  version "{version}"',
        '  license "MIT"',
    ]
    for os, brew_os in (("darwin", "macos"), ("linux", "linux")):
        lines.extend(["", f"  on_{brew_os} do"])
        if os == "darwin":
            lines.append("    depends_on macos: :ventura")
            lines.append("")
        for arch, brew_arch in (("arm64", "arm"), ("amd64", "intel")):
            name = f"co_{version}_{os}_{arch}.tar.gz"
            layer = layers[name]
            lines.extend([
                f"    on_{brew_arch} do",
                f'      url "{REGISTRY}/blobs/{layer["digest"]}",',
                "          using: CurlGitHubPackagesDownloadStrategy",
                f'      sha256 "{layer["digest"][7:]}"',
                "    end",
            ])
        lines.append("  end")
    lines.extend([
        "",
        "  def install",
        '    bin.install "co"',
        '    prefix.install "LICENSE"',
        "  end",
        "",
        "  test do",
        '    system bin/"co", "--version"',
        "  end",
        "end",
        "",
    ])
    return "\n".join(lines)


def write_release(root, release):
    index = render_index(release)
    path = root / "releases/latest.txt"
    if path.exists():
        previous = path.read_text(encoding="ascii")
        rows = previous.splitlines()
        require(len(rows) == 8 and rows[0] == "co-release-v1" and rows[1].startswith("version "),
                "Existing release index is malformed")
        old_version = rows[1][8:]
        require(VERSION.fullmatch(old_version), "Existing release version is malformed")
        old = tuple(map(int, old_version.split(".")))
        new = tuple(map(int, release[0].split(".")))
        require(new >= old, "Refusing to downgrade the published release")
        require(new != old or previous == index, "Refusing to replace an immutable published release")
    formula = render_formula(release)
    path.parent.mkdir(parents=True, exist_ok=True)
    (root / "Formula").mkdir(parents=True, exist_ok=True)
    path.write_text(index, encoding="ascii")
    (root / "Formula/co.rb").write_text(formula, encoding="ascii")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output-root", type=Path, default=Path(__file__).resolve().parents[1])
    args = parser.parse_args()
    try:
        registry = Registry()
        registry.authenticate()
        release = fetch_release(registry)
        write_release(args.output_root, release)
    except (ReleaseError, OSError, UnicodeError) as error:
        print(f"Release sync failed: {error}", file=sys.stderr)
        return 1
    print(f"Validated co v{release[0]} ({release[2]})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
