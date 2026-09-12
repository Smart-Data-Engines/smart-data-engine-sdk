#!/usr/bin/env python3
"""Which package does this tag publish, and does its manifest agree?

This is the one decision in the release pipeline that cannot be taken back. A tag is immutable
under the `refs/tags/*` ruleset and both registries refuse to reuse a version number, so a tag that
says one thing while the manifest says another publishes the manifest's version under the tag's
name, permanently, with no way to correct either. That is why this is a tested script and not a
line of shell inside a workflow: the shell in a workflow is the least reviewed code in any
repository and this is the most consequential.

Three tag forms exist and one of them is an error:

    refs/tags/python-v0.1.0      -> publish python/ to PyPI at 0.1.0
    refs/tags/typescript-v0.1.0  -> publish typescript/ to npm at 0.1.0
    refs/tags/v0.1.0             -> refused, with both valid forms named

**The bare form is refused rather than ignored**, and that is deliberate. `v0.1.0` is what a person
types from habit, and a tag that triggers nothing is a release that looks done: the tag is in the
repository, it is protected by the ruleset, and nothing was published. A claim of absence is the
one kind that cannot fail on its own, and a silent no-op is that failure with a version number
attached. So the workflow triggers on `v*` too, purely so this script can say what to do instead.

**Per-language tags rather than one tag for both**, because the alternative publishes an artefact
identical to its predecessor with an empty changelog every time the other language changes, at a
version number neither registry will ever hand back. What makes the two libraries agree is the
conformance suite and `conformance/contract-version.txt`, not a shared version number; making the
version number mean it as well would be a second mechanism for something already held, and a
duplicated guarantee cannot be mutated separately.

The Python version is read from wherever `pyproject.toml` tells Hatchling to read it, rather than
from a path written here. A second copy of that path is how this script comes to check a file the
build backend no longer uses - and it would fail in the direction that publishes.

Usage:

    python3 tools/release_tag.py refs/tags/python-v0.1.0

Prints `package=` and `version=` for `$GITHUB_OUTPUT` on success. Exit status 1 on any refusal.
"""

from __future__ import annotations

import re
import sys
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

#: `<package>-v<version>`, and the package name is the directory name so there is nothing to map.
TAG = re.compile(r"^(?:refs/tags/)?(?P<package>[a-z]+)-v(?P<version>.+)$")

#: The bare form, recognised only so that it can be refused by name.
BARE = re.compile(r"^(?:refs/tags/)?v(?P<version>.+)$")

PACKAGES = ("python", "typescript")


def python_version() -> tuple[str, Path]:
    """The version Hatchling will put in the wheel, read from the file `pyproject.toml` names."""
    pyproject = ROOT / "python" / "pyproject.toml"
    config = tomllib.loads(pyproject.read_text(encoding="utf-8"))

    dynamic = config["project"].get("dynamic", [])
    if "version" not in dynamic:
        # A static `version` would mean this function is reading a file the build no longer
        # consults.
        declared = config["project"].get("version")
        if declared is None:
            raise SystemExit(
                "python/pyproject.toml declares neither a static `version` nor `version` in "
                "`dynamic`, so there is no version for a tag to agree with."
            )
        return str(declared), pyproject

    try:
        relative = config["tool"]["hatch"]["version"]["path"]
    except KeyError:
        raise SystemExit(
            "python/pyproject.toml says the version is dynamic but does not say where Hatchling "
            "should read it (`[tool.hatch.version] path`). This script refuses to guess: guessing "
            "here means checking a file the build ignores, and the mistake only shows up as a "
            "published artefact carrying a version nobody chose."
        ) from None

    source = ROOT / "python" / relative
    text = source.read_text(encoding="utf-8")
    match = re.search(r'^__version__\s*=\s*["\'](?P<version>[^"\']+)["\']', text, re.MULTILINE)
    if match is None:
        raise SystemExit(f"no `__version__ = \"...\"` assignment in {source.relative_to(ROOT)}")
    return match.group("version"), source


def typescript_version() -> tuple[str, Path]:
    manifest = ROOT / "typescript" / "package.json"
    import json

    return str(json.loads(manifest.read_text(encoding="utf-8"))["version"]), manifest


def manifest_version(package: str) -> tuple[str, Path]:
    if package == "python":
        return python_version()
    if package == "typescript":
        return typescript_version()
    raise AssertionError(package)


def resolve(ref: str) -> tuple[str, str]:
    """The package and version this tag publishes.

    Raises `SystemExit` saying why it publishes nothing.
    """
    bare = BARE.match(ref)
    if bare is not None:
        version = bare.group("version")
        raise SystemExit(
            f"the tag {ref!r} does not say which package to publish, so it publishes nothing.\n"
            f"This repository holds two distributions on two registries and they version "
            f"independently, so a release names one:\n"
            f"    git tag python-v{version}      # smart-data-engine-sdk on PyPI\n"
            f"    git tag typescript-v{version}  # @smart-data-engines/sde on npm\n"
            f"The tag you pushed is protected by the ruleset and cannot be deleted or moved, so "
            f"pick the next version number rather than trying to reuse this one."
        )

    match = TAG.match(ref)
    if match is None:
        raise SystemExit(
            f"the tag {ref!r} is not a release tag. Release tags are `python-v<version>` or "
            f"`typescript-v<version>`."
        )

    package, version = match.group("package"), match.group("version")
    if package not in PACKAGES:
        raise SystemExit(
            f"the tag {ref!r} names a package {package!r} that does not exist here. "
            f"This repository publishes {', '.join(PACKAGES)}.\n"
            f"If a third library is being released, it needs a directory, an entry in this "
            f"script, a trigger in the release workflow and a pattern in the tag ruleset - and "
            f"the last one is the one that is easy to forget, which would leave the tag driving "
            f"a publish while remaining deletable and movable."
        )

    declared, source = manifest_version(package)
    if declared != version:
        raise SystemExit(
            f"the tag {ref!r} says version {version!r} and "
            f"{source.relative_to(ROOT)} says {declared!r}.\n"
            f"Publishing would put {declared!r} on the registry under a tag claiming {version!r}, "
            f"and neither can be corrected afterwards: the registry refuses to reuse a version "
            f"number and the ruleset refuses to move or delete the tag. Bump the manifest, commit "
            f"it to main, and tag that commit."
        )

    return package, version


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print(__doc__)
        return 2
    package, version = resolve(argv[1])
    print(f"package={package}")
    print(f"version={version}")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
