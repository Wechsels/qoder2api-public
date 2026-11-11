#!/usr/bin/env python3
"""Optional, explicit download helper. The e2e never invokes this itself."""

import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import sys
import tarfile
import tempfile
import urllib.request


ASSETS = {
    "windows_amd64": ("windows_amd64_server.tar.gz", "windows_amd64_client.tar.gz"),
    "linux_amd64": ("linux_amd64_server.tar.gz", "linux_amd64_client.tar.gz"),
    "linux_arm64": ("linux_arm64_server.tar.gz", "linux_arm64_client.tar.gz"),
}


def sha256(path):
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def extract(archive, root):
    with tarfile.open(archive, "r:gz") as bundle:
        for member in bundle.getmembers():
            path = Path(member.name)
            if path.is_absolute() or ".." in path.parts or member.issym() or member.islnk():
                raise RuntimeError("unsafe archive member: " + member.name)
        for member in bundle.getmembers():
            if not (member.isfile() or member.isdir()):
                raise RuntimeError("unsupported archive member: " + member.name)
        bundle.extractall(root)


def find_binary(root, stem):
    suffix = ".exe" if os.name == "nt" else ""
    matches = [p for p in root.rglob(stem + suffix) if p.is_file()]
    if not matches and os.name == "nt":
        matches = [p for p in root.rglob(stem + ".exe") if p.is_file()]
    if len(matches) != 1:
        raise RuntimeError("expected one %s binary, found %d" % (stem, len(matches)))
    return matches[0]


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version", required=True, help="exact release, e.g. v0.26.10")
    parser.add_argument("--platform", choices=sorted(ASSETS),
                        default="windows_amd64" if os.name == "nt" else "linux_amd64")
    parser.add_argument("--dest", required=True, help="empty/nonexistent external tool directory")
    parser.add_argument("--sha256-nps", required=True)
    parser.add_argument("--sha256-npc", required=True)
    args = parser.parse_args(argv)
    if not args.version.startswith("v") or any(c not in "v0123456789.-" for c in args.version):
        parser.error("--version must be an exact release tag")
    dest = Path(args.dest).expanduser().resolve()
    if dest.exists() and (not dest.is_dir() or any(dest.iterdir())):
        parser.error("--dest must be an empty or nonexistent directory")
    dest.mkdir(parents=True, exist_ok=True)
    if len(args.sha256_nps) != 64 or len(args.sha256_npc) != 64:
        parser.error("both SHA-256 values must contain 64 hexadecimal characters")
    try:
        int(args.sha256_nps, 16)
        int(args.sha256_npc, 16)
    except ValueError:
        parser.error("invalid hexadecimal SHA-256")

    names = ASSETS[args.platform]
    with tempfile.TemporaryDirectory(prefix="qdp-fetch-nps-") as temp:
        for kind, name, expected in zip(("nps", "npc"), names,
                                        (args.sha256_nps, args.sha256_npc)):
            url = "https://github.com/ehang-io/nps/releases/download/%s/%s" % (
                args.version, name)
            archive = Path(temp) / name
            print("Downloading official release: " + url)
            request = urllib.request.Request(url, headers={"User-Agent": "qdp-e2e-fetch/1"})
            with urllib.request.urlopen(request, timeout=60) as response, archive.open("wb") as out:
                shutil.copyfileobj(response, out)
            actual = sha256(archive)
            if actual.lower() != expected.lower():
                raise RuntimeError("SHA-256 mismatch for %s: expected %s, got %s" % (
                    name, expected, actual))
            unpack = Path(temp) / kind
            unpack.mkdir()
            extract(str(archive), unpack)
            source = find_binary(unpack, kind)
            target = dest / source.name
            shutil.copy2(str(source), str(target))
            if os.name != "nt":
                target.chmod(0o700)
            print(json.dumps({kind: str(target), "sha256_archive": actual}))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as exc:
        print("DOWNLOAD FAILED: %s" % exc, file=sys.stderr)
        sys.exit(1)
