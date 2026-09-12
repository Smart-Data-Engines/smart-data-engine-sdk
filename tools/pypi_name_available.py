#!/usr/bin/env python3
"""Is a distribution name actually usable on PyPI? Two questions, because one is not enough.

This exists because of a measurement that cost an evening. `smart-data-engine` was recorded as
available on four separate days, on the strength of `GET /pypi/<name>/json` answering 404 - and
PyPI refused the upload with 400, "the name is too similar to an existing project".

**Three endpoints, and two of them cannot answer the question.** Measured on 12 September 2026
against a name that is taken, a name that is free, and a name nobody would ever register:

| endpoint                | free | registered, zero releases | invented | verdict          |
|-------------------------|------|---------------------------|----------|------------------|
| `GET /pypi/<name>/json` | 404  | **404**                   | 404      | blind            |
| `GET /project/<name>/`  | 200  | 200                       | **200**  | entirely blind   |
| `GET /simple/<name>/`   | 404  | **200**                   | 404      | answers          |

The JSON API 404s when a project has no *release*, so a name somebody has registered and never used
reads exactly like a free one - and that is the common case for a squatted name. `/project/`
returns 200 for anything at all, including a name invented on the spot.

**And `/simple/` alone is still not the answer**, because a name can be free and still refused.
PyPI compares `ultranormalize_name(candidate)` against every existing project, exact equality, and
that function - defined in warehouse migration `d18d443f89f0` - strips `.`, `_` and `-`, folds
`l|L|i|I` to `1` and `o|O` to `0`, and lowercases. So `smart-data-engine` and `smartdata-engine`
both reduce to `smartdataeng1ne` and only one of them can exist. That check needs the whole index,
which is why this script downloads it rather than asking about one name.

Usage:

    python tools/pypi_name_available.py smart-data-engine-sdk [more-names ...]

Exit status is 0 only if every name given is usable. The index download is ~42 MB and is cached in
the system temporary directory for an hour, because picking a name means trying several.
"""

from __future__ import annotations

import json
import re
import sys
import tempfile
import time
import urllib.request
from pathlib import Path

SIMPLE_INDEX = "https://pypi.org/simple/"
CACHE = Path(tempfile.gettempdir()) / "pypi-simple-index.json"
CACHE_SECONDS = 3600

#: PyPI's own rule, transcribed from warehouse migration `d18d443f89f0`:
#:
#:     lower(regexp_replace(regexp_replace(regexp_replace($1,'(\.|_|-)','','ig'),
#:                                         '(l|L|i|I)','1','ig'), '(o|O)','0','ig'))
#:
#: Transcribed rather than approximated: an approximation here would reproduce the failure this
#: script exists to prevent, and would do it while printing "available".
_STRIP = re.compile(r"[._-]")
_ONES = re.compile(r"[lLiI]")
_ZEROS = re.compile(r"[oO]")


def ultranormalize(name: str) -> str:
    """PyPI's similarity key. Two names collide exactly when these are equal."""
    return _ZEROS.sub("0", _ONES.sub("1", _STRIP.sub("", name))).lower()


def all_project_names() -> list[str]:
    """Every name on PyPI, from the PEP 691 simple index.

    The index includes projects with **zero releases**, which is the whole reason it is used here:
    that is the population the JSON API cannot see, and the one a squatted name lives in.
    """
    if CACHE.is_file() and time.time() - CACHE.stat().st_mtime < CACHE_SECONDS:
        payload = json.loads(CACHE.read_text(encoding="utf-8"))
    else:
        request = urllib.request.Request(
            SIMPLE_INDEX,
            headers={
                "Accept": "application/vnd.pypi.simple.v1+json",
                "User-Agent": "smart-data-engine-sdk name check (contact@smartdataengines.com)",
            },
        )
        with urllib.request.urlopen(request, timeout=120) as response:
            payload = json.loads(response.read().decode("utf-8"))
        CACHE.write_text(json.dumps(payload), encoding="utf-8")
    return [project["name"] for project in payload["projects"]]


def exact_name_exists(name: str) -> bool:
    """Whether `/simple/<name>/` resolves, which is the one endpoint that tells the truth."""
    request = urllib.request.Request(
        f"{SIMPLE_INDEX}{name}/",
        headers={"Accept": "application/vnd.pypi.simple.v1+json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=30) as response:
            return bool(200 <= response.status < 300)
    except urllib.error.HTTPError as exc:
        if exc.code == 404:
            return False
        raise


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print(__doc__)
        return 2

    names = argv[1:]
    print("downloading the simple index (cached for an hour) ...", file=sys.stderr)
    everything = all_project_names()
    by_key: dict[str, list[str]] = {}
    for existing in everything:
        by_key.setdefault(ultranormalize(existing), []).append(existing)

    # A control in the same run, because an index that failed to load would report every name as
    # available - which is the shape of good news that this script exists to distrust. `pip` has
    # existed since 2008; if it is missing, the population is wrong, not the answer.
    if "p1p" not in by_key:
        print("CONTROL FAILED: 'pip' is not in the downloaded index, so it is not the index.")
        return 3
    print(f"index: {len(everything):,} names, control passed\n", file=sys.stderr)

    worst = 0
    for name in names:
        key = ultranormalize(name)
        collisions = [other for other in by_key.get(key, []) if other != name]
        taken = exact_name_exists(name)

        if taken:
            print(f"{name}: TAKEN — /simple/{name}/ resolves")
            worst = 1
        elif collisions:
            print(
                f"{name}: REFUSED — too similar to {collisions}. "
                f"Both reduce to {key!r} under PyPI's own comparison, and it demands they differ."
            )
            worst = 1
        else:
            print(f"{name}: usable — not registered, and {key!r} collides with nothing")
    return worst


if __name__ == "__main__":
    sys.exit(main(sys.argv))
