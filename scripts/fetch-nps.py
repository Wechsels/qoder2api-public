#!/usr/bin/env python3
"""Fetch a pinned nps release after verifying its administrator-supplied SHA-256."""

import argparse
import hashlib
import os
from pathlib import Path, PurePosixPath
import re
import stat
import sys
import tarfile
import tempfile
import urllib.request


VERSION = "v0.26.10"
ASSETS = {
    ("linux", "amd64", "client"): "linux_amd64_client.tar.gz",
    ("linux", "amd64", "server"): "linux_amd64_server.tar.gz",
    ("linux", "arm64", "client"): "linux_arm64_client.tar.gz",
    ("linux", "arm64", "server"): "linux_arm64_server.tar.gz",
    ("windows", "amd64", "client"): "windows_amd64_client.tar.gz",
    ("windows", "amd64", "server"): "windows_amd64_server.tar.gz",
}


def safe_members(archive):
    for member in archive.getmembers():
        path = PurePosixPath(member.name)
        if (path.is_absolute() or ".." in path.parts or not path.parts
                or member.issym() or member.islnk() or member.isdev()
                or not (member.isfile() or member.isdir())):
            raise ValueError("unsafe archive member: %s" % member.name)
        if member.mode & (stat.S_ISUID | stat.S_ISGID | stat.S_ISVTX):
            raise ValueError("unsafe archive permissions: %s" % member.name)
        yield member


def extract_verified(archive_path, dest):
    with tarfile.open(str(archive_path), "r:gz") as archive:
        members = list(safe_members(archive))
        targets = [dest.joinpath(*PurePosixPath(member.name).parts).resolve() for member in members]
        conflicts = [str(path) for path in targets if path.exists() or path.is_symlink()]
        if conflicts:
            raise ValueError("refusing to overwrite existing archive paths: %s" % ", ".join(conflicts))
        for path in targets:
            if dest not in path.parents and path != dest:
                raise ValueError("archive member escapes destination")
        archive.extractall(str(dest), members=members)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--platform", choices=("linux", "windows"), required=True)
    parser.add_argument("--arch", choices=("amd64", "arm64"), required=True)
    parser.add_argument("--role", choices=("client", "server"), required=True)
    parser.add_argument("--sha256", required=True, help="independently verified release SHA-256")
    parser.add_argument("--dest", required=True)
    args = parser.parse_args()
    try:
        asset = ASSETS[(args.platform, args.arch, args.role)]
    except KeyError:
        parser.error("this release has no %s/%s %s asset" % (args.platform, args.arch, args.role))
    expected = args.sha256.lower()
    if not re.fullmatch(r"[0-9a-f]{64}", expected):
        parser.error("--sha256 must be exactly 64 hexadecimal characters")
    dest = Path(args.dest).resolve()
    dest.mkdir(parents=True, exist_ok=True)
    url = "https://github.com/ehang-io/nps/releases/download/%s/%s" % (VERSION, asset)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(prefix="nps-download-", suffix=".tar.gz", dir=str(dest), delete=False) as handle:
            temporary = Path(handle.name)
            digest = hashlib.sha256()
            request = urllib.request.Request(url, headers={"User-Agent": "qoder2api-public-fetch"})
            with urllib.request.urlopen(request, timeout=60) as response:
                while True:
                    chunk = response.read(1024 * 1024)
                    if not chunk:
                        break
                    digest.update(chunk)
                    handle.write(chunk)
        if digest.hexdigest() != expected:
            raise ValueError("SHA-256 mismatch; archive was not extracted")
        extract_verified(temporary, dest)
        print("Verified and extracted %s (%s) to %s" % (asset, VERSION, dest))
    except Exception as exc:
        print("error: %s" % exc, file=sys.stderr)
        return 1
    finally:
        if temporary:
            temporary.unlink(missing_ok=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
