"""Task 19.3 and 19.7: the two documents an outsider needs, kept true by machine.

``docs/implementing.md`` says how to write an SDE library and ``docs/implementations.md`` says which
ones exist and what each one does. Both are the kind of document that rots in one direction: a
vector family is added and the guide's step list does not grow, or a library gains an engine adapter
and the table still says it has none. Prose does not fail; these tests do.

Requirement 16.7 says a contract that needs a conversation is a bug to fix rather than a property,
and 17.6 says the list of implementations has to be current, because "we support Java" meaning
something other than what it means for Python is misleading in the direction that costs a client a
migration. Neither requirement can be met by a document nobody checks.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

import sde

ROOT = Path(__file__).resolve().parents[2]
GUIDE = ROOT / "docs" / "implementing.md"
LIST = ROOT / "docs" / "implementations.md"
CONTRACT = ROOT / "docs" / "format-contract.md"
VECTORS = ROOT / "conformance" / "vectors"


def _families() -> set[str]:
    return {d.name for d in VECTORS.iterdir() if d.is_dir()}


# `conformance/` is a path in this repository, not a vector family.
NOT_A_FAMILY = frozenset({"conformance"})


def _flat(text: str) -> str:
    """One line, single-spaced.

    Every parser in this file reads a Markdown document, and a Markdown document is hard-wrapped at
    a column nobody promised to keep. A pattern written against the wrapping matches until somebody
    edits a word earlier in the paragraph - which happened to the failure-semantics checks and cost
    a confusing red run, so it is collapsed here before anything is matched.
    """
    return " ".join(text.split())


def _unwritten_families() -> set[str]:
    """The families the contract itself admits do not exist yet.

    Parsed out of §10 rather than listed here, so that the day one of them is written the contract
    sentence changes and this test starts insisting the guide be updated too.

    Two forms are accepted and one is not: the sentence naming what is missing, or the sentence
    saying nothing is. Removing both is what this refuses, because a contract that stopped saying
    either would silently switch this check off - and §9 says a tier claim is one the vectors can
    check, which is the sentence the missing sets make false.
    """
    text = _flat(CONTRACT.read_text())
    sentence = re.search(r"do not exist yet:\*\* (.+?)\. That is worth stating", text)
    if sentence is None:
        assert "**Every set named in the tier table exists.**" in text, (
            "format-contract §10 says neither which vector sets are missing nor that none are. One "
            "of the two has to be there: §9 promises a tier claim the vectors can check, and this "
            "is the only place that promise is audited."
        )
        return set()
    return set(re.findall(r"`([a-z]+)/`", sentence.group(1)))


def test_the_guide_names_every_vector_family_and_no_others() -> None:
    """Both directions. A family the guide does not mention is a step an implementer has to
    discover; a family the guide mentions and the tree does not have is a step they cannot take -
    unless it is one of the sets the contract says is not written yet, which the guide names on
    purpose because a tier claim nothing can check is worth saying out loud."""
    guide = GUIDE.read_text()
    named = {m for m in re.findall(r"`([a-z]+)/`", guide)} - NOT_A_FAMILY
    families = _families()
    missing = families - named
    assert missing == set(), f"vector families the guide does not mention: {missing}"
    assert named - families == _unwritten_families(), (
        f"the guide names families that do not exist: {named - families - _unwritten_families()}"
    )


def test_the_guide_names_the_step_each_family_belongs_to() -> None:
    """A family listed somewhere in the prose is not the same as a family with a place in the
    order. The ordered list is the part of this guide that is worth more than the contract, because
    the contract is a reference and this is a path through it."""
    steps = [
        line for line in GUIDE.read_text().splitlines() if re.match(r"\*\*\d+\. ", line)
    ]
    assert len(steps) >= 8, f"the ordered path has {len(steps)} steps"
    ordered = "\n".join(steps)
    for family in _families():
        assert f"`{family}/" in ordered, f"{family}/ has no step in the ordered path"


def test_the_measurement_says_when_and_against_which_contract() -> None:
    """A measurement without a date and a version is an anecdote. This one is the whole argument
    that requirement 16.7 is met, so it carries both."""
    guide = GUIDE.read_text()
    assert "## The measurement" in guide
    assert re.search(r"\d{1,2} September 2026", guide), "the measurement has no date"
    version = (ROOT / "conformance" / "contract-version.txt").read_text().strip()
    assert f"contract version {version}" in guide, "the measurement does not say which contract"


def test_the_measurement_states_what_it_is_not() -> None:
    """The isolation was of the source tree, not of the person who wrote it. A page claiming an
    outsider passed the vectors would be claiming the one thing this measurement cannot show."""
    guide = GUIDE.read_text()
    assert "is not an outsider" in guide


def test_every_tier_the_list_claims_is_one_the_contract_defines() -> None:
    """The tiers come from format-contract §9. A row claiming Tier 4 is claiming something no
    vector can check."""
    tiers = set(re.findall(r"^\| (\d) \|", CONTRACT.read_text(), re.MULTILINE))
    assert tiers == {"0", "1", "2", "3"}, tiers
    claimed = set(re.findall(r"^\| [^|]+ \| [^|]+ \| (\d) \|", LIST.read_text(), re.MULTILINE))
    assert claimed, "no implementation rows found"
    assert claimed <= tiers, f"tiers claimed that the contract does not define: {claimed - tiers}"


def _row(library: str) -> list[str]:
    for line in LIST.read_text().splitlines():
        if line.startswith(f"| `{library}`"):
            return [cell.strip() for cell in line.strip("|").split("|")]
    raise AssertionError(f"{library} has no row in docs/implementations.md")


def test_the_python_row_agrees_with_the_library() -> None:
    row = _row("smart-data-engine")
    _, language, tier, hashing, ir_contract, map_contract, engines = row
    assert tier == "2"
    assert hashing == "yes" and hasattr(sde, "hash_identifiers")
    assert ir_contract == str(sde.CONTRACT)
    en_dash = "\N{EN DASH}"
    assert map_contract == f"{sde.MAP_CONTRACT_FLOOR}{en_dash}{sde.MAP_CONTRACT}", map_contract

    # Against docs/platforms.md rather than against `requires-python` directly: that document
    # already names the versions run in CI, and `test_platforms.py` already pins it to the metadata
    # and the workflow. Reading the exclusive `<3.14` ceiling here and subtracting one would be a
    # second place that knows what a version range means.
    platforms = (ROOT / "docs" / "platforms.md").read_text()
    supported = re.search(r"\| Supported \| \*\*(.+?)\*\* \|", platforms)
    assert supported, "docs/platforms.md no longer says which Python versions are supported"
    versions = [v.strip() for v in supported.group(1).split(",")]
    assert language == f"Python {versions[0]}{en_dash}{versions[-1]}", (language, versions)

    # Tier 2 is per engine, and "supported" means round-trips. The row names the adapters, so the
    # adapters have to be there - and an adapter added without the row growing is a capability the
    # list does not claim, which is the harmless direction only until somebody relies on the list.
    # Dialect identifiers, compared against the library's own vocabulary rather than against
    # module filenames: the row is a claim about what this library can store, and `sde.DIALECTS` is
    # where that claim is enforced.
    named = tuple(sorted(name.strip().strip("`") for name in engines.split(",")))
    assert named == sde.DIALECTS, (named, sde.DIALECTS)
    present = {
        path.stem for path in (ROOT / "python" / "src" / "sde" / "engines").glob("*.py")
        if not path.stem.startswith("_")
    }
    assert len(present) == len(named), (present, named)


def test_the_typescript_row_claims_no_engines_and_has_none() -> None:
    """A ratchet, not a description. The day that library grows an engine adapter this test fails,
    and the table is the thing that has to change - because at that moment the word "Tier 0" in it
    stops being true and a client reading it would size their deployment on it."""
    row = _row("@smart-data-engines/sde")
    assert row[2] == "0", row
    assert row[6] == "none", row
    source = ROOT / "typescript" / "src"
    assert not (source / "engines").exists(), "the TypeScript library has an engines directory"
    drivers = [
        path.name
        for path in source.rglob("*.ts")
        if re.search(r"from '(pg|clickhouse|mysql|mongodb)", path.read_text())
    ]
    assert drivers == [], drivers


def test_the_list_says_who_fixes_a_defect_in_each_kind() -> None:
    """The distinction between the two lists is not the tier, it is who is on the hook. A page that
    ranked them without saying that would be the misleading version requirement 17.6 refuses."""
    text = LIST.read_text()
    assert "**Supported by us.**" in text and "**Community.**" in text
    ours = text.index("**Supported by us.**")
    theirs = text.index("**Community.**")
    assert "a defect in it is ours" in text[ours:theirs]
    assert "belongs to its author" in text[theirs:]


def test_the_go_implementation_is_named_as_a_measurement_and_is_not_here() -> None:
    """It found six defects and it is still not a library. Keeping it would be a support claim we
    cannot hold and a fourth implementation to keep in sync with every contract change."""
    text = LIST.read_text()
    assert "**Go**" in text
    assert "not in this repository" in text
    assert list(ROOT.glob("**/*.go")) == [], "there is Go source in this repository"


@pytest.mark.parametrize("document", [GUIDE, LIST])
def test_every_document_this_page_links_to_exists(document: Path) -> None:
    """A guide whose links are dead is a guide that sends an implementer to ask us."""
    for target in re.findall(r"\]\((?!https?:)([^)#]+)", document.read_text()):
        assert (document.parent / target).exists(), f"{document.name} links to missing {target}"
