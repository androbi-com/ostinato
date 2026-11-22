# SPDX-License-Identifier: GPL-3.0-only
"""Ostinato's version, in the three files that hold it: pyproject.toml, src/ostinato/__init__.py and uv.lock.

    uv run --no-project tools/bump_version.py                show the version (exit code 1 when the files disagree)
    uv run --no-project tools/bump_version.py patch          0.3.0 -> 0.3.1 (or: minor, major)
    uv run --no-project tools/bump_version.py 0.4.0          this version
    ... --dry-run                                            only say what would change

It only edits the files: commit and push them, then publish the release with the tag v<version> (the release workflow
refuses a tag that does not match). `--no-project` leaves the environment alone (a running ostinato-device holds its
programs open). Run `uv sync` later to bring the environment up to date.
"""
from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
# file -> what stands around its version (exactly one match each; \r? : either kind of line ending)
PLACES = {
    "pyproject.toml": r'(?m)^(version = ")([^"\r\n]+)(")',
    "src/ostinato/__init__.py": r'(?m)^(__version__ = ")([^"\r\n]+)(")',
    "uv.lock": r'(?m)^(name = "ostinato"\r?\nversion = ")([^"\r\n]+)(")',
}
VERSION = re.compile(r"(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)")
PARTS = ("major", "minor", "patch")


def read(name: str) -> tuple[str, re.Match]:
    text = (ROOT / name).read_bytes().decode("utf-8")  # (bytes: the line endings stay as they are)
    found = list(re.finditer(PLACES[name], text))
    if len(found) != 1:
        sys.exit(f"{name}: expected one version, found {len(found)}; nothing was changed")
    return text, found[0]


def numbers(version: str) -> tuple[int, ...]:
    m = VERSION.fullmatch(version)
    if not m:
        sys.exit(f"not a version: {version!r} (patch, minor, major, or like 0.4.0); nothing was changed")
    return tuple(int(x) for x in m.groups())


def main() -> int:
    ap = argparse.ArgumentParser(description="Show or set Ostinato's version in all the files that hold it.")
    ap.add_argument("version", nargs="?", help="patch, minor, major, or the version itself (0.4.0)")
    ap.add_argument("--dry-run", action="store_true", help="only say what would change")
    args = ap.parse_args()

    files = {name: read(name) for name in PLACES}
    now = {name: m.group(2) for name, (_, m) in files.items()}
    agree = len(set(now.values())) == 1

    if args.version is None:
        for name, v in now.items():
            print(f"{v:<10} {name}")
        if not agree:
            print("The files disagree: give a version to set them all.", file=sys.stderr)
        return 0 if agree else 1

    if args.version in PARTS:
        if not agree:
            sys.exit("the files disagree (" + ", ".join(f"{n} {v}" for n, v in now.items())
                     + "): give the version itself; nothing was changed")
        n = numbers(next(iter(now.values())))
        i = PARTS.index(args.version)
        new = ".".join(str(x) for x in (*n[:i], n[i] + 1, *[0] * (2 - i)))
    else:
        new = args.version.removeprefix("v")
        highest = max(numbers(v) for v in now.values())
        if numbers(new) < highest:
            sys.exit(f"{new} is lower than the current {'.'.join(map(str, highest))}; nothing was changed")

    for name, (text, m) in files.items():
        if m.group(2) == new:
            print(f"{name}: already {new}")
            continue
        print(f"{name}: {m.group(2)} -> {new}")
        if not args.dry_run:
            (ROOT / name).write_bytes((text[: m.start(2)] + new + text[m.end(2):]).encode("utf-8"))
    if args.dry_run:
        print("(dry run: nothing was changed)")
    else:
        print(f"Next: commit and push these files, then publish the release with the tag v{new}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
