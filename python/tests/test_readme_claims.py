"""The front page's claims about what this product does, audited against the product.

``README.md`` is the first thing a client reads and the last thing anybody rereads. Every other
document here has a test over it - the format contract has the vectors, the implementer's guide and
the list of implementations have :mod:`test_implementer_documents`, the failure page has one row per
failure with a cited test - and the one document that decides whether a reader keeps reading had a
single assertion over it: that it links to the failure page.

It rotted in the gap. On 2 September 2026 the migration engine landed in both halves of the product
- ``sde.backfill`` and ``sde.verify`` here, the state machine and the gate in the control plane -
and this page went on saying **"What does not exist yet: no migration engine"** for four days, four
lines above a table claiming Tier 2 for two libraries, which §9 of the format contract defines as
*"schema creation, and participation in migration"*. The page contradicted itself, understated the
hardest part of the product, and no test could tell.

**A claim of absence is the only kind that cannot fail by itself.** Claim a capability you do not
have and the first reader who tries it finds out; claim you lack one you have and the sentence keeps
reading correctly forever. So an absence claim in the status section has to cite what is absent, the
way a row on the failure page cites the test that pins it, and the citation is checked *for not
resolving*: the day ``rust/`` exists, this fails and the page has to change.

Scoped to the status section on purpose. Elsewhere the page says "it has no transactions" about an
engine and "there is no code path that sends a query through us" about the boundary, and those are
statements about the world rather than about our backlog. Inside a section headed **Status**, "does
not exist yet" is a statement a reader plans around.

The rest of this file is about the other way a landing page goes wrong: **counts of our own
artefacts, restated in several documents, none of which counts anything.** Measured from the
history, the Go measurement produced eleven vectors and the Rust one nine; four documents said ten,
ten, ten and eight, and one of them contradicted itself two hundred lines apart. What is checkable
is checked here - the size of the suite, and every vector id a page cites - and what is not (how
many vectors a past exercise produced, which only `git` knows) is stated in one document and pointed
at from the others.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from _claims import MEASUREMENTS, absence_claims, sentences

import sde

ROOT = Path(__file__).resolve().parents[2]
README = ROOT / "README.md"

_CITATION = re.compile(r"`([^`]+)`")


def _status_section() -> str:
    text = README.read_text(encoding="utf-8")
    start = text.index("## Status")
    end = text.find("\n## ", start + 1)
    return text[start:] if end == -1 else text[start:end]


def _still_here(citation: str) -> str | None:
    """Why a cited thing is not absent after all, or ``None`` if the claim holds.

    Two vocabularies, because a page like this cites in two: a path in the repository and a name in
    the library's public API. Both are read out of the tree rather than out of a list kept here -
    the list would be the thing that goes stale, which is the defect this file exists for.
    """
    name = citation.strip().removeprefix("sde.")
    if citation.endswith("/"):
        if (ROOT / citation).is_dir():
            return f"{citation} is a directory in this repository"
        return None
    if name in sde.__all__:
        return f"sde.{name} is exported by the library"
    return None


def test_the_status_section_exists_and_is_where_the_claims_live() -> None:
    """Guards the guard: a heading rename would make every check below pass over an empty string."""
    section = _status_section()
    assert section.startswith("## Status")
    assert len(section) > 500, "the status section is too short to be the one this file audits"


def test_an_absence_claim_in_the_status_section_cites_what_is_absent() -> None:
    """Every "does not exist yet" on the front page, checked against the tree.

    A check that passes by finding nothing has to be shown a case it finds, and it is shown two:
    a claim with no citation at all, which is the shape the real defect had, and a claim citing
    something that does exist, which is the shape it takes next.
    """
    uncited = "What does not exist yet: **no migration engine**."
    assert absence_claims(uncited) and not _CITATION.findall(uncited), (
        "the detector no longer sees an uncited claim of absence, so this test would pass the "
        "sentence it was written for"
    )
    assert _still_here("sde.backfill") is not None, "a real export reads as absent"
    assert _still_here("python/") is not None, "a real directory reads as absent"
    assert _still_here("rust/") is None, "a directory that is not here reads as present"

    for claim in absence_claims(_status_section()):
        cited = _CITATION.findall(claim)
        assert cited, (
            "the status section says something does not exist and does not say what:\n  "
            f"{claim}\nCite the path or the export that is missing, so that the day it arrives "
            "this test fails and the sentence gets rewritten."
        )
        for citation in cited:
            reason = _still_here(citation)
            assert reason is None, (
                f"the status section says this is not built:\n  {claim}\n...and {reason}. The page "
                "is denying something the product ships."
            )


@pytest.mark.parametrize(("language", "_suffix"), MEASUREMENTS)
def test_every_measurement_is_reported_on_the_front_page(language: str, _suffix: str) -> None:
    """A measurement the reader is not told about is a defect list they cannot weigh.

    ``docs/implementations.md`` records both, and :mod:`test_implementer_documents` keeps that page
    honest. This is the front page owing the same reader the same paragraph: the whole output of
    the exercise is *what it found in shipped code*, and a client deciding whether to trust these
    libraries is entitled to the list. The page fell behind by one on the day the second was run.

    Asked of a section rather than of the whole file, because the bare word is not evidence: this
    page lists `rust/` as a language a library may arrive in, and a check for "Rust" anywhere would
    have read that row as a report of the Rust measurement and passed.

    **What it does not assert is the heading**, and the first mutation run is why this docstring
    says so. Renaming the section survived, correctly: the reader had still been told. The
    obligation is the report, not its title, and a test named for the title claimed more than it
    checked.
    """
    sections = README.read_text(encoding="utf-8").split("\n### ")
    named = [s for s in sections if language in s and "measurement" in s.lower()]
    assert named, (
        f"no section of the front page reports the {language} measurement. It is recorded in "
        "docs/implementations.md, so the page understates both what has been checked and what "
        "was found in shipped code."
    )


VECTORS = ROOT / "conformance" / "vectors"

#: Pages a reader lands on, plus the two documents that describe the suite to an implementer.
PAGES = [
    README,
    ROOT / "conformance" / "README.md",
    ROOT / "docs" / "implementing.md",
    ROOT / "docs" / "format-contract.md",
]

# Our prose spells small numbers out, so the check has to read them the same way.
_WORDS = [
    "zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten",
    "eleven", "twelve", "thirteen", "fourteen", "fifteen", "sixteen", "seventeen", "eighteen",
    "nineteen", "twenty",
]

_CITED_VECTOR = re.compile(r"`([a-z]+)/(\d{3})[^`]*`")
_RANGE = re.compile(r"`(\d{3})` through `(\d{3})`")


def _families() -> dict[str, set[str]]:
    """Each vector family, and the three-digit ids in it, read out of the tree."""
    return {
        family.name: {d.name[:3] for d in family.iterdir() if d.is_dir()}
        for family in VECTORS.iterdir()
        if family.is_dir()
    }


def test_the_front_page_states_the_size_of_the_suite_and_the_tree_agrees() -> None:
    """The one count on this page that a machine can settle, so it is settled.

    A reader uses this number to judge how much the byte contract is actually held to, which makes
    it worth stating - and a stated number nothing checks is the defect this file was written for.
    Adding a vector now fails this test until the page says so, which is the same discipline
    ``docs/implementations.md`` is already kept to.
    """
    families = _families()
    total = sum(len(ids) for ids in families.values())
    page = README.read_text(encoding="utf-8")
    assert f"**{total} of them" in page, (
        f"the front page does not say the suite is {total} vectors. It is, counted in "
        "conformance/vectors."
    )
    assert f"in {_WORDS[len(families)]} families**" in page, (
        f"the front page does not say there are {len(families)} vector families: "
        f"{sorted(families)}"
    )


@pytest.mark.parametrize("page", PAGES, ids=lambda p: str(p.relative_to(ROOT)))
def test_every_vector_a_page_cites_exists(page: Path) -> None:
    """A citation to a vector that is not there reads as evidence and is not.

    Both forms these pages use are checked, and the limit of the second one is worth stating.
    `conformance/`'s own page said the Go measurement added ``026`` through ``035`` when it had
    added ``036`` as well - the vector for the confusion that measurement's own paragraph
    describes, three lines above. **No check over this tree can see that**: every id in the shorter
    range exists too, and which vectors one past exercise produced is a fact only the history
    holds. So the range is checked for the two things that are derivable - every id in it exists,
    and the number word beside it equals its length - and the fact itself is stated in one document
    with the others pointing there.
    """
    text = page.read_text(encoding="utf-8")
    families = _families()

    for family, ident in _CITED_VECTOR.findall(text):
        if family not in families:
            continue
        assert ident in families[family], (
            f"{page.name} cites `{family}/{ident}` and there is no such vector. The ids in that "
            f"family are {sorted(families[family])}"
        )

    for sentence in sentences(text):
        for low, high in _RANGE.findall(sentence):
            span = int(high) - int(low) + 1
            spelled = [w for w in _WORDS if re.search(rf"\b{w}\b", sentence, re.IGNORECASE)]
            if len(spelled) == 1:
                # One number word in the sentence can be attributed to the range; two cannot, and
                # guessing which is worse than not checking.
                assert _WORDS.index(spelled[0]) == span, (
                    f"{page.name} says {spelled[0]} and names a range of {span}: "
                    f"`{low}` through `{high}`"
                )
            for family in [f for f in families if f"`{f}/`" in sentence]:
                ids = {f"{n:03d}" for n in range(int(low), int(high) + 1)}
                missing = sorted(ids - families[family])
                assert not missing, (
                    f"{page.name} claims {family}/{low} through {high} and {missing} are not "
                    "there"
                )
