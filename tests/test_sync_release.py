import copy
import io
import json
import os
import subprocess
import tarfile
import tempfile
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest.mock import patch

from fixtures import FixtureRegistry, ROOT, archive, sync


class ReleaseTests(unittest.TestCase):
    def test_bare_zero_major_tag_preserves_release_bytes(self):
        registry = FixtureRegistry("0.1.0", distinct=True)
        document = copy.deepcopy(registry.document)
        blobs = registry.blobs.copy()
        raw = json.dumps(document, separators=(",", ":")).encode()

        release = sync.fetch_release(registry)

        self.assertEqual([url for url in registry.requests if "/manifests/" in url], [
            f"{sync.REGISTRY}/manifests/latest",
            f"{sync.REGISTRY}/manifests/0.1.0",
        ])
        layers = {layer["annotations"]["org.opencontainers.image.title"]: layer
                  for layer in document["layers"]}
        self.assertEqual(release, ("0.1.0", "a" * 40, sync.digest(raw), layers))
        self.assertEqual(registry.document, document)
        self.assertEqual(registry.document["annotations"]["org.opencontainers.image.version"], "v0.1.0")
        self.assertEqual(registry.blobs, blobs)
        expected_rows = ["co-release-v1", "version 0.1.0",
                         f"manifest {sync.digest(raw)}", f"revision {'a' * 40}"]
        for os_name, arch in sync.PLATFORMS:
            layer = layers[f"co_0.1.0_{os_name}_{arch}.tar.gz"]
            data = blobs[layer["digest"]]
            expected_rows.append(f"{os_name} {arch} {sync.digest(data)[7:]} {len(data)}")
        self.assertEqual(sync.render_index(release), "\n".join(expected_rows) + "\n")
        self.assertIn('  version "0.1.0"\n', sync.render_formula(release))

    def test_valid_release_all_platforms_and_formula_behavior(self):
        registry = FixtureRegistry(distinct=True)
        registry.authenticate()
        release = sync.fetch_release(registry)
        self.assertEqual(release[0], "1.2.3")
        self.assertEqual(len(sync.render_index(release).splitlines()), 8)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            sync.write_release(root, release)
            self.assertEqual((root / "releases/latest.txt").read_text(), sync.render_index(release))
            for os_name, arch in sync.PLATFORMS:
                with self.subTest(os=os_name, arch=arch):
                    name = f"co_1.2.3_{os_name}_{arch}.tar.gz"
                    checksum = release[3][name]["digest"][7:]
                    expected = {"version": "1.2.3", "checksum": checksum, "name": name,
                                "url": f"{sync.REGISTRY}/blobs/sha256:{checksum}"}
                    result = subprocess.run(
                        ["ruby", str(ROOT / "tests/formula_harness.rb"), str(root / "Formula/co.rb"),
                         json.dumps(expected)],
                        env=dict(os.environ, FORMULA_OS=os_name, FORMULA_ARCH=arch),
                        capture_output=True, text=True,
                    )
                    self.assertEqual(result.returncode, 0, result.stderr)

    @unittest.skipUnless(os.environ.get("TEST_WITH_HOMEBREW") == "1", "opt-in actual Homebrew loader check")
    def test_actual_homebrew_loader_all_platforms(self):
        registry = FixtureRegistry(distinct=True)
        release = sync.fetch_release(registry)
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            sync.write_release(root, release)
            for os_name, arch in sync.PLATFORMS:
                with self.subTest(os=os_name, arch=arch):
                    name = f"co_1.2.3_{os_name}_{arch}.tar.gz"
                    checksum = release[3][name]["digest"][7:]
                    archive_path = root / f"sha256:{checksum}"
                    archive_path.write_bytes(registry.blobs["sha256:" + checksum])
                    destination = root / f"{os_name}-{arch}"
                    destination.mkdir()
                    result = subprocess.run(
                        ["brew", "ruby", str(ROOT / "tests/homebrew_formula.rb"),
                         "linux" if os_name == "linux" else "ventura",
                         "arm" if arch == "arm64" else "intel",
                         str(root / "Formula/co.rb"), checksum, str(archive_path), str(destination)],
                        env=dict(os.environ, HOMEBREW_NO_AUTO_UPDATE="1",
                                 HOMEBREW_NO_ANALYTICS="1", HOMEBREW_DEVELOPER="1"),
                        capture_output=True, text=True,
                    )
                    self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_idempotence_and_monotonic_immutable_updates(self):
        for version, older, newer in (("0.1.0", "0.0.9", "0.1.1"), ("1.2.3", "1.2.2", "1.3.0")):
            with self.subTest(version=version), tempfile.TemporaryDirectory() as directory:
                first = sync.fetch_release(FixtureRegistry(version))
                root = Path(directory)
                sync.write_release(root, first)
                paths = (root / "releases/latest.txt", root / "Formula/co.rb")
                before = [path.read_bytes() for path in paths]
                sync.write_release(root, sync.fetch_release(FixtureRegistry(version)))
                self.assertEqual([path.read_bytes() for path in paths], before)
                for release in [
                    sync.fetch_release(FixtureRegistry(older)),
                    (first[0], "b" * 40, first[2], first[3]),
                    sync.fetch_release(FixtureRegistry(version, distinct=True)),
                ]:
                    with self.assertRaises(sync.ReleaseError):
                        sync.write_release(root, release)
                    self.assertEqual([path.read_bytes() for path in paths], before)
                sync.write_release(root, sync.fetch_release(FixtureRegistry(newer)))
                self.assertIn(f"version {newer}", paths[0].read_text())

    def test_malformed_version_annotations_are_rejected_before_tag_lookup(self):
        for version in (
            None, 1, {}, ["v0.1.0"], "", "latest", "0.1.0", "v0", "v0.1", "vv0.1.0",
            "v00.1.0", "v0.01.0", "v0.1.00", "v0.1.0-rc.1", "v0.1.0+build.1",
            "v0.1.0\n", "v0.1.0/../latest", "v" + "1" * 60 + ".0.0",
        ):
            with self.subTest(version=version):
                registry = FixtureRegistry("0.1.0")
                registry.document["annotations"]["org.opencontainers.image.version"] = version
                with self.assertRaisesRegex(sync.ReleaseError, "stable vMAJOR.MINOR.PATCH"):
                    sync.fetch_release(registry)
                self.assertEqual(registry.requests, [f"{sync.REGISTRY}/manifests/latest"])

    def test_malformed_manifest_metadata(self):
        mutations = [
            lambda m: m.update(schemaVersion=1),
            lambda m: m.update(mediaType="application/vnd.oci.image.index.v1+json"),
            lambda m: m.update(artifactType="unexpected"),
            lambda m: m.update(annotations=None),
            lambda m: m["annotations"].update({"org.opencontainers.image.version": "v1.2.3-rc.1"}),
            lambda m: m["annotations"].update({"org.opencontainers.image.version": 'v1.2.3";system("bad")'}),
            lambda m: m["annotations"].update({"org.opencontainers.image.revision": "short"}),
            lambda m: m["config"].update(digest="sha256:" + "0" * 64),
            lambda m: m.update(layers=m["layers"][:4]),
            lambda m: m["layers"][0].update(mediaType="text/plain"),
            lambda m: m["layers"][0].update(size=True),
            lambda m: m["layers"][0].update(size=sync.MAX_ARCHIVE + 1),
            lambda m: m["layers"][0].update(digest="sha256:invalid"),
            lambda m: m["layers"][0]["annotations"].update({"org.opencontainers.image.title": "../co"}),
            lambda m: m["layers"][1].update(annotations=copy.deepcopy(m["layers"][0]["annotations"])),
        ]
        for mutate in mutations:
            with self.subTest(mutation=mutations.index(mutate)):
                registry = FixtureRegistry()
                mutate(registry.document)
                with self.assertRaises(sync.ReleaseError):
                    sync.fetch_release(registry)

    def test_tag_mismatch(self):
        for version in ("0.1.0", "1.2.3"):
            registry = FixtureRegistry(version)
            for content in (b"{}", json.dumps(registry.document, indent=2).encode()):
                with self.subTest(version=version, content=content[:30]):
                    registry.tag_override = content
                    with self.assertRaisesRegex(sync.ReleaseError, "Version tag"):
                        sync.fetch_release(registry)

    def test_blob_size_and_digest_mismatch(self):
        for altered in (b"x", b"x" * len(archive())):
            registry = FixtureRegistry()
            registry.blobs[sync.digest(registry.payload)] = altered
            with self.assertRaisesRegex(sync.ReleaseError, "Blob (size|checksum)"):
                sync.fetch_release(registry)

    def test_malformed_checksums(self):
        registry = FixtureRegistry()
        sums = registry.blobs[registry.document["layers"][-1]["digest"]]
        cases = [b"invalid\n", sums + sums, sums.replace(b"  co_", b"  ../co_"),
                 sums.replace(sums[:64], b"0" * 64)]
        for content in cases:
            with self.subTest(content=content[:30]):
                registry = FixtureRegistry()
                registry.document["layers"][-1] = registry.descriptor("SHA256SUMS", content, "text/plain")
                with self.assertRaises(sync.ReleaseError):
                    sync.fetch_release(registry)

    def test_unsafe_archives(self):
        normal = [("co", b"binary", 0o755, tarfile.REGTYPE),
                  ("LICENSE", b"license", 0o644, tarfile.REGTYPE)]
        cases = [
            b"not gzip",
            archive(normal + [("../escape", b"x", 0o644, tarfile.REGTYPE)]),
            archive(normal + [normal[0]]),
            archive([("co", b"", 0o755, tarfile.SYMTYPE), normal[1]]),
            archive([("co", b"", 0o755, tarfile.LNKTYPE), normal[1]]),
            archive([("co", b"binary", 0o4755, tarfile.REGTYPE), normal[1]]),
            archive([("co", b"", 0o755, tarfile.REGTYPE), normal[1]]),
        ]
        for content in cases:
            with self.subTest(size=len(content)):
                with self.assertRaises(sync.ReleaseError):
                    sync.fetch_release(FixtureRegistry(payload=content))

    def test_json_validation(self):
        for content in (b'{"token":"a","token":"b"}', b"[]", b"\xff", b"{", b"null"):
            with self.subTest(content=content):
                with self.assertRaises(sync.ReleaseError):
                    sync.read_json(content)

    def test_authentication_errors(self):
        for response in (b"{}", b'{"token":""}', b'{"token":"a\\nb"}'):
            registry = FixtureRegistry()
            registry.token_response = response
            with self.assertRaises(sync.ReleaseError):
                registry.authenticate()

    def test_http_errors_and_response_limit(self):
        registry = sync.Registry()
        for code in (401, 403, 404, 500):
            error = urllib.error.HTTPError("https://ghcr.io", code, "fixture", {}, None)
            with patch.object(registry.opener, "open", side_effect=error):
                with self.assertRaisesRegex(sync.ReleaseError, f"HTTP {code}"):
                    registry.request("https://ghcr.io", 3)
        with patch.object(registry.opener, "open", return_value=io.BytesIO(b"large")):
            with self.assertRaisesRegex(sync.ReleaseError, "exceeds"):
                registry.request("https://ghcr.io", 3)

    def test_redirects_require_tls_and_strip_cross_host_token(self):
        handler = sync.SecureRedirect()
        request = urllib.request.Request("https://ghcr.io/blob", headers={"Authorization": "Bearer fixture"})
        with self.assertRaisesRegex(sync.ReleaseError, "non-HTTPS"):
            handler.redirect_request(request, None, 302, "", {}, "http://storage.example/blob")
        redirected = handler.redirect_request(request, None, 302, "", {}, "https://storage.example/blob")
        self.assertIsNone(redirected.get_header("Authorization"))
        redirected = handler.redirect_request(request, None, 302, "", {}, "https://ghcr.io/next")
        self.assertEqual(redirected.get_header("Authorization"), "Bearer fixture")


if __name__ == "__main__":
    unittest.main()
