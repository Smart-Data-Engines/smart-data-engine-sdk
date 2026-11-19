#!/usr/bin/env python3
"""A release's bill of materials describes the artefact it is attested beside.

The release workflow builds a CycloneDX bill for each artefact it publishes, and the publishing job
attests it beside the artefact - `actions/attest` with `sbom-path` - before the upload, so that a
client can read it back with `gh attestation verify`. An attestation binds whatever file it is
handed. This is what makes the file it is handed the bill of this release:

- **python**: the bill of a fresh environment holding the built wheel with every extra a client may
  choose. It names `smart-data-engine-sdk` at the tagged version, with the SHA-256 of the very wheel
  that was installed - so it describes this file and not a rebuild - and every distribution the
  extras bring.
- **npm**: the bill `npm sbom` writes for the package. Its root component carries the package URL
  of `@smart-data-engines/sde` at the tagged version; npm fills the component's *name* from the
  directory, so the package URL is what identifies it.

**There is a control in the same run**, as in `check_artefact.py`. The Python bill is matched by
normalised names and by hashes read from two places in a component, so it is also asked for a name
and a digest that cannot be in it, and must find neither: a matcher stuck on "found" fails there,
one stuck on "missing" fails on the bill's own wheel. The npm bill is matched by one exact package
URL, which has no such failure to catch.

**requirements** writes what the Python bill names as an audit reads it: one `name==version` line
per distribution, sorted, our own wheel left out - it has no advisory before it is published, and
an audit that looks for it on PyPI only reports it missing. The release audits that list and
nothing else (`pip-audit --disable-pip --no-deps`), so what is audited is what the bill says a
client installs, version for version, with no resolution of its own. A component without a
version, one that is not a PyPI distribution, a name at two versions, or a list with nothing in it
is refused: each would make the audit pass over something it did not look at.

Usage:

    python3 tools/check_sbom.py python sbom-python.cdx.json dist/sde-0.3.0-py3-none-any.whl 0.3.0 \
        --requires cryptography psycopg clickhouse-connect orderbook-dbengine
    python3 tools/check_sbom.py npm sbom-npm.cdx.json 0.3.0
    python3 tools/check_sbom.py requirements sbom-python.cdx.json > bill-requirements.txt

Exit status 0 only if the bill is CycloneDX, names the artefact as above, holds every required
distribution, and, for Python, the control holds; for requirements, only if every line could be
written.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from collections.abc import Iterator
from pathlib import Path
from typing import Any

PYTHON_NAME = "smart-data-engine-sdk"
NPM_PURL = "pkg:npm/%40smart-data-engines/sde"
CONTROL = "__this_component_cannot_exist__"
NOTHING = hashlib.sha256(b"").hexdigest()


def _normal(name: str) -> str:
    """A distribution name as PyPI compares them (PEP 503)."""
    return re.sub(r"[-_.]+", "-", name).lower()


def components(bom: dict[str, Any]) -> Iterator[dict[str, Any]]:
    """Every component of the bill, the root one and nested ones included."""
    pending = [((bom.get("metadata") or {}).get("component"))]
    pending.extend(bom.get("components") or [])
    while pending:
        component = pending.pop()
        if isinstance(component, dict):
            yield component
            pending.extend(component.get("components") or [])


def named(bom: dict[str, Any], name: str) -> list[dict[str, Any]]:
    return [c for c in components(bom) if _normal(str(c.get("name", ""))) == _normal(name)]


def _purl(component: dict[str, Any]) -> str:
    """A package URL without its qualifiers and subpath: `pkg:type/name@version`."""
    return re.split(r"[?#]", str(component.get("purl") or ""), maxsplit=1)[0]


def _hashes(component: dict[str, Any]) -> set[str]:
    found = {
        str(h.get("content", "")).lower()
        for h in component.get("hashes") or []
        if str(h.get("alg", "")).upper() == "SHA-256"
    }
    for reference in component.get("externalReferences") or []:
        if reference.get("type") == "distribution":
            found |= {
                str(h.get("content", "")).lower()
                for h in reference.get("hashes") or []
                if str(h.get("alg", "")).upper() == "SHA-256"
            }
    return found


def _format(bom: Any) -> list[str]:
    if not isinstance(bom, dict) or bom.get("bomFormat") != "CycloneDX":
        found = bom.get("bomFormat") if isinstance(bom, dict) else type(bom).__name__
        return [f"not a CycloneDX bill: bomFormat is {found!r}"]
    if not isinstance(bom.get("specVersion"), str):
        return ["the bill states no CycloneDX specVersion"]
    if not bom.get("serialNumber"):
        # `actions/attest` takes a bill for CycloneDX only with all three of bomFormat,
        # specVersion and serialNumber (src/sbom.ts, v4.2.2), and refuses it otherwise - in the
        # publishing job, after the reviewer has approved. A reproducible bill leaves the serial
        # number out (`cyclonedx-py --output-reproducible`), so this is not hypothetical.
        return ["the bill has no serialNumber, which `actions/attest` requires of CycloneDX"]
    return []


def check_python(
    bom: Any, wheel: Path, version: str, requires: list[str] | None = None
) -> list[str]:
    problems = _format(bom)
    if problems:
        return problems
    digest = hashlib.sha256(wheel.read_bytes()).hexdigest()
    ours = named(bom, PYTHON_NAME)
    if not ours:
        problems.append(f"the bill names no {PYTHON_NAME}")
    elif not any(str(c.get("version")) == version for c in ours):
        versions = sorted(str(c.get("version")) for c in ours)
        problems.append(f"the bill has {PYTHON_NAME} at {versions}, not at {version}")
    elif not any(digest in _hashes(c) for c in ours if str(c.get("version")) == version):
        problems.append(
            f"the bill's {PYTHON_NAME} {version} is not {wheel.name}: no SHA-256 {digest} - it "
            f"describes another build of the same version"
        )
    for name in requires or []:
        if not named(bom, name):
            problems.append(f"the bill does not hold {name}, which the installed extras bring")
    if named(bom, CONTROL) or any(NOTHING in _hashes(c) for c in ours):
        problems.append(
            f"CONTROL FAILED: the bill reports {CONTROL!r} or the digest of nothing as present, "
            f"so a matcher is broken and nothing above can be believed"
        )
    return problems


def check_npm(bom: Any, version: str) -> list[str]:
    problems = _format(bom)
    if problems:
        return problems
    root = (bom.get("metadata") or {}).get("component") or {}
    if _purl(root) != f"{NPM_PURL}@{version}":
        problems.append(
            f"the bill's root component is {_purl(root) or 'without a package URL'}, not "
            f"{NPM_PURL}@{version}"
        )
    return problems


def requirements(bom: Any) -> tuple[list[str], list[str]]:
    """The distributions a Python bill names, pinned and sorted, without our own; and the problems
    that leave no list an audit could be trusted with."""
    problems = _format(bom)
    if problems:
        return [], problems
    root = (bom.get("metadata") or {}).get("component")
    pinned: dict[str, str] = {}
    for component in components(bom):
        if component is root:
            continue  # the environment the bill describes, not a distribution in it
        name = str(component.get("name") or "")
        if _normal(name) == _normal(PYTHON_NAME):
            continue
        version = component.get("version")
        if not name or not isinstance(version, str) or not version:
            problems.append(f"{name or 'a component'} has no version to audit")
            continue
        if not _purl(component).startswith("pkg:pypi/"):
            found = _purl(component) or "no package URL"
            problems.append(f"{name} is not a PyPI distribution ({found})")
            continue
        if pinned.get(_normal(name), version) != version:
            first, second = sorted((pinned[_normal(name)], version))
            problems.append(f"{name} is in the bill at {first} and at {second}")
            continue
        pinned[_normal(name)] = version
    if not pinned and not problems:
        problems.append(
            "the bill names no distribution besides ours: an audit would look at nothing"
        )
    return sorted(f"{name}=={version}" for name, version in pinned.items()), problems


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = parser.add_subparsers(dest="kind", required=True)
    python = sub.add_parser("python")
    python.add_argument("bom", type=Path)
    python.add_argument("wheel", type=Path)
    python.add_argument("version")
    python.add_argument("--requires", nargs="*", default=[])
    npm = sub.add_parser("npm")
    npm.add_argument("bom", type=Path)
    npm.add_argument("version")
    pinned = sub.add_parser("requirements")
    pinned.add_argument("bom", type=Path)
    args = parser.parse_args(argv)
    bom = json.loads(args.bom.read_text(encoding="utf-8"))
    if args.kind == "requirements":
        lines, problems = requirements(bom)
        if problems:
            print(f"{args.bom.name}:", *problems, sep="\n  ", file=sys.stderr)
            return 1
        print(*lines, sep="\n")
        return 0
    if args.kind == "python":
        problems = check_python(bom, args.wheel, args.version, args.requires)
    else:
        problems = check_npm(bom, args.version)
    if problems:
        print(f"{args.bom.name}:", *problems, sep="\n  ", file=sys.stderr)
        return 1
    print(f"{args.bom.name}: {sum(1 for _ in components(bom))} components, {args.version}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
