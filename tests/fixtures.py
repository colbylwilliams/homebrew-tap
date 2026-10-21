import gzip
import io
import json
import sys
import tarfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import sync_release as sync


def archive(entries=None):
    if entries is None:
        entries = [("co", b"fixture binary\n", 0o755, tarfile.REGTYPE),
                   ("LICENSE", b"fixture license\n", 0o644, tarfile.REGTYPE)]
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode="w", format=tarfile.USTAR_FORMAT) as output:
        for name, content, mode, kind in entries:
            member = tarfile.TarInfo(name)
            member.mode = mode
            member.type = kind
            member.size = len(content) if kind == tarfile.REGTYPE else 0
            if kind in (tarfile.SYMTYPE, tarfile.LNKTYPE):
                member.linkname = "/tmp/never-extract-this"
            output.addfile(member, io.BytesIO(content) if member.size else None)
    return gzip.compress(stream.getvalue(), mtime=0)


class FixtureRegistry(sync.Registry):
    """Use the real parsing/digest code with an entirely in-memory transport."""

    def __init__(self, version="1.2.3", payload=None, distinct=False):
        self.token = None
        self.version = version
        self.requests = []
        self.blobs = {}
        self.payload = payload if payload is not None else archive()
        layers = []
        sums = []
        for os, arch in sync.PLATFORMS:
            name = f"co_{version}_{os}_{arch}.tar.gz"
            data = self.payload
            if distinct:
                data = archive([("co", f"fixture {os} {arch}".encode(), 0o755, tarfile.REGTYPE),
                                ("LICENSE", b"fixture license\n", 0o644, tarfile.REGTYPE)])
            layer = self.descriptor(name, data, "application/gzip")
            layers.append(layer)
            sums.append(f"{layer['digest'][7:]}  {name}\n")
        layers.append(self.descriptor("SHA256SUMS", "".join(sums).encode(), "text/plain"))
        self.document = {
            "schemaVersion": 2,
            "mediaType": sync.MANIFEST_TYPE,
            "artifactType": "application/vnd.co.cli.v1",
            "config": {
                "mediaType": "application/vnd.oci.empty.v1+json",
                "digest": sync.digest(b"{}"),
                "size": 2,
            },
            "annotations": {
                "org.opencontainers.image.version": f"v{version}",
                "org.opencontainers.image.revision": "a" * 40,
            },
            "layers": layers,
        }
        self.tag_override = None
        self.token_response = b'{"token":"fixture-token"}'

    def descriptor(self, name, data, media_type):
        checksum = sync.digest(data)
        self.blobs[checksum] = data
        return {"mediaType": media_type, "digest": checksum, "size": len(data),
                "annotations": {"org.opencontainers.image.title": name}}

    def request(self, url, limit, accept=None):
        self.requests.append(url)
        if url.startswith("https://ghcr.io/token?"):
            data = self.token_response
        elif "/manifests/" in url:
            if not url.endswith("/latest") and self.tag_override is not None:
                data = self.tag_override
            else:
                data = json.dumps(self.document, separators=(",", ":")).encode()
        else:
            data = self.blobs[url.split("/blobs/")[1]]
        sync.require(len(data) <= limit, "Registry response exceeds limit")
        return data
