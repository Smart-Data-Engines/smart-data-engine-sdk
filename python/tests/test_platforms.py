"""Requirement 15.3: the platforms this library runs on are recorded, not presumed.

Three documents have to agree and none of them is the authority: ``docs/platforms.md`` is what a
client reads, the packaging metadata is what ``pip`` and ``npm`` enforce, and the CI matrix is what
is actually run. A list is only "recorded rather than presumed" if the three cannot drift, so this
file compares them and fails on any disagreement.

**The defect this found is worth stating.** ``requires-python`` was ``>=3.11`` with no upper bound
while CI tested three versions - so the package claimed 3.14 and 4.0 and tested neither. For most
libraries that is the friendly default. For this one it is a hazard with a name: the whole job here
is a **byte-identical canonical encoding**, ``model_version`` is a digest over it, and two services
of the same client that compute different digests reject each other's maps. The CI workflow already
said why both ends of the matrix are run - *"a canonical encoding that drifts with a Python release
is exactly the failure the contract exists to prevent"* - and the open ceiling handed that failure
to whoever upgraded first: ``pip install`` succeeds on an untested interpreter, and the drift
surfaces as a rejected map in production. Both ceilings are closed now, and this test is what makes
moving them one action instead of three.

The check on the *workflow* is the same shape as ``.github/rulesets/check_contexts.py``: a matrix
entry added without the document being updated is a version tested and unclaimed, and a version
claimed without a matrix entry is a claim nothing stands behind. Requirement 15.3 refuses the
second; the first is how the second happens by accident later.
"""

from __future__ import annotations

import json
import re
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DOC = ROOT / "docs" / "platforms.md"
WORKFLOW = ROOT / ".github" / "workflows" / "ci.yml"


def _documented(language: str) -> list[str]:
    """The versions the document says are supported, from its own table.

    Parsed from the ``| Supported | **...** |`` row rather than from a separate machine-readable
    list, and that is deliberate: a second copy for the test to read is a copy that can agree with
    the test while the prose a client reads says something else.
    """
    text = DOC.read_text(encoding="utf-8")
    section = text.split(f"## {language}", 1)[1].split("\n## ", 1)[0]
    row = next(line for line in section.splitlines() if line.startswith("| Supported "))
    return re.findall(r"\d+(?:\.\d+)?", row)


def _matrix(key: str) -> list[str]:
    """One matrix line from the CI workflow, without a YAML parser.

    A regex over the one line rather than ``yaml.safe_load``: this file has to run in the plain
    test environment, and adding a dependency to read three version numbers would put a package in
    the way of a check whose whole point is that it always runs.
    """
    text = WORKFLOW.read_text(encoding="utf-8")
    row = next(
        line for line in text.splitlines() if line.strip().startswith(f"{key}: [")
    )
    return re.findall(r'"([^"]+)"', row)


def test_the_python_versions_in_the_document_are_the_ones_ci_runs() -> None:
    assert _documented("Python") == _matrix("python")


def test_the_node_versions_in_the_document_are_the_ones_ci_runs() -> None:
    assert _documented("Node") == _matrix("node")


def test_requires_python_claims_exactly_what_is_tested() -> None:
    """No open ceiling, on a package whose output is a digest.

    The floor has to be the lowest tested version and the ceiling has to exclude the first
    untested one. Derived from the matrix rather than pinned to a literal, so bumping the matrix
    and forgetting the metadata fails here rather than in a client's production.
    """
    tested = _matrix("python")
    metadata = tomllib.loads((ROOT / "python" / "pyproject.toml").read_text(encoding="utf-8"))
    declared = metadata["project"]["requires-python"]

    floor = min(tested, key=lambda v: tuple(int(p) for p in v.split(".")))
    highest = max(tested, key=lambda v: tuple(int(p) for p in v.split(".")))
    major, minor = (int(part) for part in highest.split("."))
    assert declared == f">={floor},<{major}.{minor + 1}", declared


def test_the_node_engines_range_claims_exactly_what_is_tested() -> None:
    tested = [int(version) for version in _matrix("node")]
    package = json.loads((ROOT / "typescript" / "package.json").read_text(encoding="utf-8"))
    declared = package["engines"]["node"]
    assert declared == f">={min(tested)} <{max(tested) + 1}", declared


def test_the_document_says_which_operating_systems_are_tested_and_which_are_not() -> None:
    """Requirement 15.3 refuses "should work", so the document has to separate the two claims.

    "Tested: Linux" and "expected to work and not tested by us: macOS, Windows" are different
    sentences, and a page that ran them together would be presuming exactly what this requirement
    is about. The escape is named too, because "we could add a matrix entry" is the honest answer
    and claiming coverage without adding one is not.
    """
    text = DOC.read_text(encoding="utf-8")
    assert "**Tested:** Linux" in text
    assert "**Expected to work and not tested by us:**" in text
    assert "it is a matrix entry" in text
    assert "ubuntu-latest" in text


def test_the_document_says_why_the_ceiling_is_closed() -> None:
    """The reason is the interesting part, and a table of numbers would lose it.

    An open upper bound is the friendly default everywhere else. Here it hands a byte-contract
    drift to whoever upgrades first, and that sentence is what stops somebody reopening it as a
    convenience.
    """
    text = DOC.read_text(encoding="utf-8")
    assert "byte-identical canonical encoding" in text
    assert "rejected map" in text
    assert "the ceiling moves when the matrix does" in text.lower()


def test_the_engine_versions_are_deliberately_not_on_this_list() -> None:
    """A page about where the library runs is not a page about what it connects to.

    Those are the adapters' live slices and the vendors' own support matrices, and merging the two
    lists is how a client reads "PostgreSQL 15 tested" as a statement about their Python.
    """
    text = DOC.read_text(encoding="utf-8")
    assert "## What is not on this list" in text
    assert "Anything about your engines" in text
