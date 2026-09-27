#!/usr/bin/env python3
"""Which npm dist-tag does this version get, given what the registry already holds?

npm needs the answer spelled out. Since npm 11 `npm publish` of a prerelease without `--tag` is an
error ("You must specify a tag using --tag when publishing a prerelease version", in
`lib/commands/publish.js` of npm 11.15.0, the version the release workflow installs), so the first
release-candidate run of `release.yml` would have failed at the publish step, after the gate and the
reviewer had both said yes. A literal `--tag latest` would publish every candidate over the last
final, and a literal `--tag next` would leave an unpinned `npm install` on whatever was published by
hand before any candidate existed.

So the tag follows what an unpinned install should get, the way `pip` behaves on PyPI:

- a final version is `latest`;
- a prerelease is `latest` while no final version exists - pip installs the newest prerelease of a
  project that has no final release, and an npm user should get the same version;
- a prerelease is `next` once a final exists, so `latest` stays on the final.

**Refused, because a person has to decide:**
- a version already on the registry, which npm will not accept again;
- any version lower than one already published. As `latest` or as `next` it would move the tag
  backwards, and a maintenance release of an older line needs a tag of its own that nobody has
  chosen yet;
- a registry that lists nothing. The package exists before this runs - trusted publishing is
  configured on an existing package, so the first publish is by hand (docs/publishing.md §5.3) -
  and an empty list here means the lookup failed, not that the package is new.

Usage, with the output of `npm view <package> versions --json` (a list, or a string when the
registry holds one version):

    python3 tools/npm_dist_tag.py 0.1.0-rc.1 '["0.1.0-dev.0"]'

Prints `tag=` for `$GITHUB_OUTPUT` and says why on stderr. Exit status 1 on any refusal.
"""

from __future__ import annotations

import json
import re
import sys
from typing import Any

#: Semantic Versioning 2.0.0, as npm uses it: no leading zeros in numbers, build metadata allowed.
SEMVER = re.compile(
    r"^(?P<major>0|[1-9]\d*)\.(?P<minor>0|[1-9]\d*)\.(?P<patch>0|[1-9]\d*)"
    r"(?:-(?P<pre>(?:0|[1-9]\d*|\d*[A-Za-z-][0-9A-Za-z-]*)"
    r"(?:\.(?:0|[1-9]\d*|\d*[A-Za-z-][0-9A-Za-z-]*))*))?"
    r"(?:\+(?P<build>[0-9A-Za-z-]+(?:\.[0-9A-Za-z-]+)*))?$"
)


class Refused(Exception):
    """The version gets no tag from this script, and the message says why."""


def parse(version: str) -> tuple[tuple[int, int, int], tuple[str, ...]]:
    match = SEMVER.match(version)
    if match is None:
        raise Refused(f"{version!r} is not a semantic version, so npm would not accept it either.")
    core = (int(match["major"]), int(match["minor"]), int(match["patch"]))
    pre = tuple(match["pre"].split(".")) if match["pre"] else ()
    return core, pre


def _identifier_key(identifier: str) -> tuple[int, int, str]:
    # Numeric identifiers compare numerically and rank below alphanumeric ones (SemVer 11.4).
    if identifier.isdigit():
        return (0, int(identifier), "")
    return (1, 0, identifier)


def precedence(version: str) -> tuple[Any, ...]:
    """A sort key with Semantic Versioning precedence; build metadata does not take part."""
    core, pre = parse(version)
    if not pre:
        # Without a prerelease a version ranks above every prerelease of its core (SemVer 11.3).
        return (core, 1, ())
    return (core, 0, tuple(_identifier_key(item) for item in pre))


def published_versions(raw: str) -> list[str]:
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        raise Refused(f"the registry's version list is not JSON: {raw[:200]!r}") from None
    if isinstance(value, str):
        value = [value]
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise Refused(f"the registry's version list is not a list of strings: {raw[:200]!r}")
    if not value:
        raise Refused(
            "the registry lists no version of this package. It exists before any tag publishes "
            "it - the first publish is by hand, because trusted publishing is configured on an "
            "existing package (docs/publishing.md 5.3) - so an empty list means the lookup failed."
        )
    for item in value:
        parse(item)
    return value


def choose(version: str, published: list[str]) -> tuple[str, str]:
    """The dist-tag for `version`, and the sentence that explains it."""
    _, pre = parse(version)
    if version in published:
        raise Refused(
            f"{version} is already on the registry, and npm never accepts a version number twice. "
            f"Bump the manifest, merge it, and tag that commit."
        )
    key = precedence(version)
    higher = sorted((v for v in published if precedence(v) > key), key=precedence)
    if higher:
        raise Refused(
            f"{version} is lower than {higher[-1]}, already published. As `latest` or `next` it "
            f"would move the tag backwards; a release of an older line needs a dist-tag of its "
            f"own, and choosing it is not this script's decision."
        )
    finals = sorted((v for v in published if not parse(v)[1]), key=precedence)
    if not pre:
        return "latest", f"{version} is a final version, so it becomes `latest`."
    if not finals:
        return "latest", (
            f"{version} is a prerelease and no final version is published, so it becomes `latest`: "
            f"an unpinned install gets the newest prerelease, as pip does on PyPI."
        )
    return "next", (
        f"{version} is a prerelease and {finals[-1]} is the final on `latest`, so it becomes "
        f"`next`."
    )


def main(argv: list[str]) -> int:
    if len(argv) != 3:
        print("usage: npm_dist_tag.py VERSION PUBLISHED_VERSIONS_JSON", file=sys.stderr)
        return 2
    try:
        tag, why = choose(argv[1], published_versions(argv[2]))
    except Refused as refusal:
        print(f"::error::{refusal}", file=sys.stderr)
        return 1
    print(why, file=sys.stderr)
    print(f"tag={tag}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
