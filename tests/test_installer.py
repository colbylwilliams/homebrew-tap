import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path

from fixtures import FixtureRegistry, ROOT, archive, sync


class InstallerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.bin = self.root / "mock-bin"
        self.bin.mkdir()
        self.home = self.root / "home with spaces"
        self.home.mkdir()
        self.downloads = self.root / "downloads"
        self.downloads.mkdir()
        self.target = self.home / ".local/bin/co"
        self.env = dict(os.environ, HOME=str(self.home), TMPDIR=str(self.downloads),
                        PATH=str(self.bin) + os.pathsep + os.environ["PATH"],
                        FIXTURE_DIR=str(self.root), TEST_OS="Linux", TEST_ARCH="x86_64")
        self.env.pop("CO_INSTALL_DIR", None)
        self.executable("uname", 'import os, sys\nprint(os.environ["TEST_OS" if sys.argv[1] == "-s" else "TEST_ARCH"])\n')
        self.executable("sw_vers", 'import os\nprint(os.environ.get("TEST_MAC_VERSION", "13.0"))\n')
        self.executable("curl", r'''
import os, pathlib, sys
args = sys.argv[1:]
assert args[0] == "-q"
for option in ("--proto", "--proto-redir"):
    assert args[args.index(option) + 1] == "=https"
assert "--tlsv1.2" in args and "--fail" in args and "--location-trusted" not in args
url = args[-1]
root = pathlib.Path(os.environ["FIXTURE_DIR"])
with (root / "requests").open("a") as output:
    output.write(url + "\n")
if "/releases/latest.txt" in url:
    name = "index"
elif "/token?" in url:
    name = "token"
elif "/blobs/sha256:" in url:
    name = "archive"
    assert args[args.index("--header") + 1] == "Authorization: Bearer fixture-token"
else:
    raise AssertionError(url)
if os.environ.get("FAIL_DOWNLOAD") == name:
    print("fixture HTTP download failure", file=sys.stderr)
    sys.exit(22)
destination = pathlib.Path(args[args.index("--output") + 1])
destination.write_bytes((root / name).read_bytes())
''')
        self.prepare()

    def executable(self, name, code):
        path = self.bin / name
        path.write_text(f"#!{sys.executable}\n" + code)
        path.chmod(0o755)

    def prepare(self, payload=None):
        registry = FixtureRegistry(payload=payload)
        # The installer intentionally gets an independent index even for unsafe archive fixtures.
        raw = registry.manifest("latest")
        layers = {item["annotations"]["org.opencontainers.image.title"]: item
                  for item in registry.document["layers"]}
        release = (registry.version, "a" * 40, sync.digest(raw), layers)
        (self.root / "index").write_text(sync.render_index(release))
        (self.root / "token").write_text(' { "token": "fixture-token" }\n')
        (self.root / "archive").write_bytes(registry.payload)
        return registry

    def run_installer(self, success=True, message=None):
        result = subprocess.run(["sh", str(ROOT / "install.sh")], env=self.env,
                                capture_output=True, text=True)
        self.assertEqual(result.returncode == 0, success, result.stdout + result.stderr)
        if message:
            self.assertIn(message, result.stderr)
        self.assertEqual(list(self.downloads.iterdir()), [], "download cleanup failed")
        if self.target.parent.exists():
            self.assertEqual(list(self.target.parent.glob(".co-install.*")), [], "staging cleanup failed")
        return result

    def existing_install(self):
        self.target.parent.mkdir(parents=True, exist_ok=True)
        self.target.write_bytes(b"previous installation")
        self.target.chmod(0o755)

    def assert_preserved(self):
        self.assertEqual(self.target.read_bytes(), b"previous installation")
        self.assertEqual(self.target.stat().st_mode & 0o777, 0o755)

    def test_success_for_all_supported_platform_aliases(self):
        for os_name in ("Linux", "Darwin"):
            for arch in ("x86_64", "amd64", "arm64", "aarch64"):
                with self.subTest(os=os_name, arch=arch):
                    self.env.update(TEST_OS=os_name, TEST_ARCH=arch)
                    self.run_installer()
                    self.assertEqual(self.target.read_bytes(), b"fixture binary\n")
                    self.assertEqual(self.target.stat().st_mode & 0o777, 0o755)

    def test_custom_install_directory(self):
        self.env["CO_INSTALL_DIR"] = str(self.root / "custom bin")
        self.run_installer()
        self.assertEqual((self.root / "custom bin/co").read_bytes(), b"fixture binary\n")
        self.assertFalse(self.target.exists())

    def test_existing_install_is_atomically_replaced(self):
        self.existing_install()
        self.run_installer()
        self.assertEqual(self.target.read_bytes(), b"fixture binary\n")

    def test_unsupported_platform_and_macos_minimum(self):
        for changes, message in [
            ({"TEST_OS": "FreeBSD"}, "Unsupported operating system"),
            ({"TEST_ARCH": "i686"}, "Unsupported architecture"),
            ({"TEST_OS": "Darwin", "TEST_MAC_VERSION": "12.7"}, "requires macOS 13"),
        ]:
            with self.subTest(changes=changes):
                previous = self.env.copy()
                self.env.update(changes)
                self.run_installer(False, message)
                self.assertFalse((self.root / "requests").exists())
                self.env = previous

    def test_failed_downloads_preserve_installation(self):
        self.existing_install()
        for resource in ("index", "token", "archive"):
            with self.subTest(resource=resource):
                self.env["FAIL_DOWNLOAD"] = resource
                self.run_installer(False, "Download failed")
                self.assert_preserved()

    def test_malformed_index_preserves_installation(self):
        self.existing_install()
        original = (self.root / "index").read_text()
        lines = original.splitlines()
        cases = [
            "", "<html>not a release</html>", original + "\n",
            original.replace("version 1.2.3", "version $(touch /tmp/not-run)"),
            original.replace("version 1.2.3", "version 1.2.3-rc.1"),
            original.replace("sha256:", "sha512:"),
            "\n".join(lines[:-1]) + "\n",
            "\n".join(lines[:7] + [lines[4]]) + "\n",
            original.replace(lines[4].split()[2], "z" * 64),
            original.replace(lines[4].split()[3], "9999999999"),
        ]
        for content in cases:
            with self.subTest(content=content[:60]):
                (self.root / "index").write_text(content)
                self.run_installer(False)
                self.assert_preserved()

    def test_bad_tokens_are_rejected(self):
        for content in ('{}', '{"token":""}', '{"token":"x","token":"y"}',
                        '{"token":"x\\ny"}', '{"token":"x"} garbage'):
            with self.subTest(content=content):
                (self.root / "token").write_text(content)
                self.run_installer(False, "Invalid GHCR anonymous token")

    def test_checksum_and_size_mismatch_preserve_installation(self):
        self.existing_install()
        content = (self.root / "archive").read_bytes()
        (self.root / "archive").write_bytes(b"x" * len(content))
        self.run_installer(False, "checksum mismatch")
        self.assert_preserved()
        (self.root / "archive").write_bytes(content[:-1])
        self.run_installer(False, "size mismatch")
        self.assert_preserved()

    def test_unsafe_archive_preserves_installation(self):
        self.existing_install()
        license_entry = ("LICENSE", b"license", 0o644, tarfile.REGTYPE)
        cases = [
            b"not gzip",
            archive([("../co", b"binary", 0o755, tarfile.REGTYPE), license_entry]),
            archive([("co", b"", 0o755, tarfile.SYMTYPE), license_entry]),
            archive([("co", b"", 0o755, tarfile.LNKTYPE), license_entry]),
            archive([("co", b"", 0o755, tarfile.REGTYPE), license_entry]),
            archive([("co", b"binary", 0o755, tarfile.REGTYPE)] * 2),
        ]
        for content in cases:
            with self.subTest(size=len(content)):
                self.prepare(content)
                self.run_installer(False)
                self.assert_preserved()

    def test_install_failures_preserve_installation(self):
        self.existing_install()
        for command, message in (("mv", "previous installation preserved"),
                                 ("chmod", "make staged co executable"),
                                 ("mkdir", "Cannot create")):
            with self.subTest(command=command):
                self.executable(command, "import sys\nsys.exit(1)\n")
                self.run_installer(False, message)
                self.assert_preserved()
                (self.bin / command).unlink()

    def test_extraction_and_checksum_tool_failures_preserve_installation(self):
        self.existing_install()
        real_tar = shutil.which("tar")
        self.executable("tar", f'''import os, sys
if sys.argv[1] == "-xOzf":
    sys.exit(1)
os.execv({real_tar!r}, [{real_tar!r}] + sys.argv[1:])
''')
        self.run_installer(False, "Cannot extract co")
        self.assert_preserved()
        (self.bin / "tar").unlink()
        self.executable("sha256sum", "import sys\nsys.exit(1)\n")
        self.run_installer(False, "SHA256 verification failed")
        self.assert_preserved()

    def test_tar_environment_does_not_override_extraction(self):
        self.env.update(TAR_OPTIONS="--invalid-option", GZIP="--invalid-option")
        self.run_installer()

    def test_symlink_directory_and_relative_destination_rejected(self):
        self.existing_install()
        original = self.target.with_name("original")
        self.target.rename(original)
        self.target.symlink_to(original)
        self.run_installer(False, "symlink or directory")
        self.assertEqual(original.read_bytes(), b"previous installation")
        self.target.unlink()
        self.target.mkdir()
        self.run_installer(False, "symlink or directory")
        self.env["CO_INSTALL_DIR"] = "relative"
        self.run_installer(False, "absolute directory")


if __name__ == "__main__":
    unittest.main()
