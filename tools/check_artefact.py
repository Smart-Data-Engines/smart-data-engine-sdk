#!/usr/bin/env python3
"""Look inside the built artefact, because the test suite runs the source tree and a user runs this.

`python/tests/test_packaging.py` says in its own docstring why this exists: three defects were
found on 8 September 2026 by unpacking a build by hand, and none of them was visible from a green
suite. The worst was `npm pack` producing a tarball of two files - `package.json` and `README.md` -
because `files` lists `dist`, `dist` is a build output, and nothing built it. A publish from a
clean checkout would have put an importable-looking package with no code in it under our own scope,
at a version npm will never let us reuse.

That test asserts the *manifest* each build reads, deliberately, because building needs a network
and a required check that depends on PyPI being reachable fails for reasons unrelated to the change
under review. The release workflow has already fetched from the network by the time it gets here,
so this is where the artefact itself can be opened - once, on the thing that is about to be
published, after which there is no correcting it.

**There is a control in the same run.** A checker that answered "present" to everything would
report a perfect artefact, and that is the shape of good news worth distrusting. So each archive is
asked two questions with known answers: every required member must be found, and one member that
cannot exist must not be. A matcher stuck on true fails the second; one stuck on false fails the
first. Neither question alone says the instrument is looking at anything.

Usage:

    python3 tools/check_artefact.py wheel   dist/smart_data_engine_sdk-0.1.0-py3-none-any.whl 0.1.0
    python3 tools/check_artefact.py sdist   dist/smart_data_engine_sdk-0.1.0.tar.gz           0.1.0
    python3 tools/check_artefact.py npm     sde-0.1.0.tgz                                     0.1.0

Exit status 0 only if every required member is present, the control holds, and the version recorded
inside the artefact is the one given on the command line.
"""

from __future__ import annotations

import json
import re
import sys
import tarfile
import zipfile
from pathlib import Path

#: A member name no build can produce. Asked of every archive, and the answer must be "absent".
CONTROL = "__this_member_cannot_exist__"

#: Required members per kind. Globs, because the wheel's `dist-info` directory carries the version.
#:
#: Each entry is here because its absence has either happened or would be silent:
#: `py.typed` was missing and made every consumer's type checker treat the library as untyped;
#: the licence files were missing entirely; `dist/` was missing from the npm tarball.
REQUIRED = {
    "wheel": (
        "sde/__init__.py",
        "sde/py.typed",
        "*.dist-info/METADATA",
        "*.dist-info/licenses/LICENSE",
        "*.dist-info/licenses/NOTICE",
    ),
    "sdist": (
        "*/LICENSE",
        "*/NOTICE",
        "*/pyproject.toml",
        "*/src/sde/py.typed",
    ),
    "npm": (
        "package/package.json",
        "package/LICENSE",
        "package/NOTICE",
        "package/dist/index.js",
        "package/dist/index.d.ts",
        "package/dist/engines/postgres.js",
        "package/dist/engines/clickhouse.js",
        "package/dist/testing/memory.js",
    ),
}

#: Below this, the archive is not the thing we think it is. The npm tarball measured 70 files on
#: 8 September 2026 and the wheel rather more; the two-file tarball that prompted all of this would
#: fail here even if every glob above were somehow satisfied.
FLOOR = 10


def members(kind: str, path: Path) -> list[str]:
    if kind == "wheel":
        with zipfile.ZipFile(path) as archive:
            return archive.namelist()
    with tarfile.open(path, "r:gz") as archive:
        return archive.getnames()


def matches(pattern: str, names: list[str]) -> bool:
    """Glob match over the whole member list. `fnmatch` is not used: its `*` crosses `/`."""
    regex = re.compile("^" + "[^/]*".join(re.escape(part) for part in pattern.split("*")) + "$")
    return any(regex.match(name) for name in names)


def recorded_version(kind: str, path: Path, names: list[str]) -> str | None:
    """The version written inside the artefact, which is the one a user will actually install."""
    if kind == "wheel":
        with zipfile.ZipFile(path) as archive:
            for name in names:
                if matches("*.dist-info/METADATA", [name]):
                    text = archive.read(name).decode("utf-8", "replace")
                    found = re.search(r"^Version:\s*(.+)$", text, re.MULTILINE)
                    return found.group(1).strip() if found else None
        return None
    if kind == "npm":
        with tarfile.open(path, "r:gz") as archive:
            handle = archive.extractfile("package/package.json")
            if handle is None:
                return None
            return str(json.loads(handle.read().decode("utf-8"))["version"])
    return None  # an sdist records it in the filename, which the caller already chose


def verify(kind: str, path: Path, expected: str | None = None) -> list[str]:
    """Every complaint about this artefact. Empty means it is publishable."""
    names = members(kind, path)
    problems: list[str] = []

    if len(names) < FLOOR:
        problems.append(
            f"{path.name} holds {len(names)} members, fewer than {FLOOR}. An artefact this small "
            f"is "
            f"the shape of a build that did not run: the tarball that started this was two files."
        )

    for pattern in REQUIRED[kind]:
        if not matches(pattern, names):
            problems.append(f"{path.name} is missing {pattern!r}")

    # The control. Asked of the same archive, in the same run, so that a matcher which answers
    # "present" to everything cannot report a perfect artefact.
    if matches(CONTROL, names):
        problems.append(
            f"CONTROL FAILED: {path.name} reports {CONTROL!r} as present, so the matcher is broken "
            f"and every answer above it is meaningless."
        )

    if expected is not None:
        recorded = recorded_version(kind, path, names)
        if recorded is not None and recorded != expected:
            problems.append(
                f"{path.name} records version {recorded!r}, not {expected!r}. The artefact, not "
                f"the "
                f"tag, is what a user installs."
            )

    return problems


def main(argv: list[str]) -> int:
    if len(argv) not in (3, 4):
        print(__doc__)
        return 2
    kind, path = argv[1], Path(argv[2])
    expected = argv[3] if len(argv) == 4 else None
    if kind not in REQUIRED:
        print(f"unknown artefact kind {kind!r}; expected one of {', '.join(REQUIRED)}")
        return 2

    problems = verify(kind, path, expected)
    for problem in problems:
        print(problem)
    if problems:
        return 1
    print(
        f"{path.name}: {len(members(kind, path))} members, "
        f"every required one present, control held"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
