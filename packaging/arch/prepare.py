# SPDX-License-Identifier: GPL-3.0-or-later
"""Stage a makepkg build of this checkout, without fetching anything."""

import argparse
import gzip
import hashlib
import io
import os
import re
import subprocess
import tarfile
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
# Everything the package or its check() needs; nothing else is shipped in the source archive.
INCLUDE = ("bin/", "src/", "prompts/", "systemd/", "tests/", "docs/", "config.example.toml",
           "README.md", "LICENSE")


def _git(*args):
    proc = subprocess.run(["git", "-c", "safe.directory=*", "-C", str(ROOT), *args],
                          capture_output=True, text=True)
    return proc.stdout if proc.returncode == 0 else None


def source_files():
    """Tracked and new-but-not-ignored files; falls back to a walk outside a Git checkout."""
    listing = _git("ls-files", "-z", "--cached", "--others", "--exclude-standard")
    if listing is not None:
        names = [name for name in listing.split("\0") if name]
    else:
        names = [str(path.relative_to(ROOT)) for path in ROOT.rglob("*")
                 if path.is_file() and ".git" not in path.parts and "__pycache__" not in path.parts]
    return sorted(name for name in names
                  if name.startswith(INCLUDE) and (ROOT / name).is_file() and "__pycache__" not in name)


def revision():
    head = _git("rev-parse", "HEAD")
    if not head:
        return "unknown"
    dirty = _git("status", "--porcelain", "--untracked-files=no")
    return head.strip() + ("-dirty" if dirty else "")


def prepare(destination, tag=None):
    version = tomllib.loads((ROOT / "pyproject.toml").read_text())["project"]["version"]
    if not re.fullmatch(r"[0-9][A-Za-z0-9.]*", version):
        raise ValueError(f"Unsupported pacman version: {version!r}")
    if tag is not None and tag != f"v{version}":
        raise ValueError(f"Release tag {tag!r} does not match project version {version!r}")
    mtime = int(os.environ.get("SOURCE_DATE_EPOCH") or _git("log", "-1", "--format=%ct") or 0)

    def add(archive, name, data, mode):
        info = tarfile.TarInfo(f"local-autoreviewer/{name}")
        info.size, info.mode, info.mtime = len(data), mode, mtime
        info.uid = info.gid = 0
        info.uname = info.gname = "root"
        archive.addfile(info, io.BytesIO(data))

    destination.mkdir(parents=True, exist_ok=True)
    archive_path = destination / "local-autoreviewer.tar.gz"
    with open(archive_path, "wb") as raw:
        # mtime=0 in the gzip header keeps the archive reproducible.
        with gzip.GzipFile(fileobj=raw, mode="wb", mtime=0) as gz, tarfile.open(fileobj=gz, mode="w") as archive:
            for name in source_files():
                path = ROOT / name
                add(archive, name, path.read_bytes(), 0o755 if os.access(path, os.X_OK) else 0o644)
            add(archive, ".source-revision", (revision() + "\n").encode(), 0o644)

    recipe = (ROOT / "packaging/arch/PKGBUILD.in").read_text()
    for key, value in {"VERSION": version,
                       "SHA256": hashlib.sha256(archive_path.read_bytes()).hexdigest()}.items():
        recipe = recipe.replace(f"@{key}@", value)
    (destination / "PKGBUILD").write_text(recipe)
    return version


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("destination", type=Path)
    parser.add_argument("--tag", help="require an exact match with the release version")
    args = parser.parse_args()
    print(f"Staged local-autoreviewer {prepare(args.destination, args.tag)} in {args.destination}")
