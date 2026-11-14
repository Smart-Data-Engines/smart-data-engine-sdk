"""``docs/upgrading.md`` names every change the changelog marks as breaking, and nothing it lacks.

A client upgrading reads one page, and the changelog is where a change is written down when it is
made. The two drift in one direction: an entry gains "**Breaking** for ..." and the page a client
acts on never hears of it. So the page cites each entry by its title, in italics, and this file
compares the two both ways:

- an entry of a release's changelog section that says ``breaking`` or ``may break`` has to be cited
  in the page's section for that release;
- a title the page cites has to be a title in that changelog section, so an entry renamed or removed
  does not leave the page pointing at nothing.

Italics on that page are changelog titles and nothing else. That is the convention this parser
depends on, and the second check is what enforces it.

The page's sections follow the releases: ``## From 0.2.0 to the next release`` is held to the
changelog's ``## Unreleased``, and ``## From 0.2.0 to 0.3.0`` to the section whose heading names
0.3.0. A release therefore renames the heading in the same commit as it renames the changelog's, and
the first breaking entry after it asks for a new section.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CHANGELOG = ROOT / "CHANGELOG.md"
PAGE = ROOT / "docs" / "upgrading.md"

BREAKING = re.compile(r"\bbreaking\b|\bmay break\b", re.IGNORECASE)
NEXT = "the next release"


def _flat(text: str) -> str:
    return " ".join(text.split())


def _sections(text: str) -> dict[str, str]:
    """A Markdown document's ``## `` sections, heading to body."""
    found: dict[str, str] = {}
    heading: str | None = None
    lines: list[str] = []
    for line in text.splitlines():
        if line.startswith("## "):
            if heading is not None:
                found[heading] = "\n".join(lines)
            heading, lines = line[3:].strip(), []
        elif heading is not None:
            lines.append(line)
    if heading is not None:
        found[heading] = "\n".join(lines)
    return found


def _entries(section: str) -> dict[str, str]:
    """A changelog section's entries, title to flattened text.

    An entry is a block that begins, after a blank line, with a bold title at the start of a line.
    A line that merely starts with bold inside a paragraph - ``**Breaking** for ...`` after a hard
    wrap - follows text, not a blank line, and stays in its entry.
    """
    blocks: list[list[str]] = []
    blank = True
    for line in section.splitlines():
        if blank and line.startswith("**"):
            blocks.append([])
        if blocks:
            blocks[-1].append(line)
        blank = not line.strip()
    found: dict[str, str] = {}
    for block in blocks:
        text = _flat("\n".join(block))
        title = re.match(r"\*\*(.+?)\*\*", text)
        assert title is not None, text[:80]
        assert title.group(1) not in found, f"two changelog entries are titled {title.group(1)!r}"
        found[title.group(1)] = text
    return found


def _cited(section: str) -> set[str]:
    return set(re.findall(r"(?<![*\w])\*([^*]+)\*(?![*\w])", _flat(section)))


def _released(changelog: dict[str, str]) -> list[str]:
    """Final versions the changelog has a section for, newest first, as it lists them."""
    versions: list[str] = []
    for heading in changelog:
        for version in re.findall(r"(?<![\w.])(\d+\.\d+\.\d+)(?![\w.-])", heading):
            if version not in versions:
                versions.append(version)
    return versions


def _pairs() -> list[tuple[str, str, str, str]]:
    """Each section of the page: its heading, from, to, and the changelog section that holds it."""
    changelog = _sections(CHANGELOG.read_text(encoding="utf-8"))
    pairs: list[tuple[str, str, str, str]] = []
    for heading in _sections(PAGE.read_text(encoding="utf-8")):
        match = re.fullmatch(r"From (\d+\.\d+\.\d+) to (.+)", heading)
        assert match is not None, f"a section of the page is not 'From X to Y': {heading!r}"
        start, end = match.groups()
        if end == NEXT:
            pairs.append((heading, start, end, "Unreleased"))
            continue
        named = [h for h in changelog if re.search(rf"(?<![\w.]){re.escape(end)}(?![\w.-])", h)]
        assert len(named) == 1, f"the changelog has no one section for {end}: {named}"
        pairs.append((heading, start, end, named[0]))
    return pairs


def test_the_parser_reads_the_changelog_it_is_given() -> None:
    """The control: on a changelog whose titles it could not read, every check below would pass."""
    changelog = _sections(CHANGELOG.read_text(encoding="utf-8"))
    assert "Unreleased" in changelog
    assert {"0.1.0", "0.1.1", "0.2.0"} <= set(_released(changelog)), _released(changelog)
    candidates = "## `a` 0.3.0 and `b` 0.3.0\n\n## `a` 0.3.0rc1 and `b` 0.3.0-rc.1\n"
    assert _released(_sections(candidates)) == ["0.3.0"]
    sample = _entries(
        "\n**One: a title** (Python). Text.\n**Breaking** for nobody, after a hard wrap.\n\n"
        "**Two: over\ntwo lines** - plain.\n- **bold** in a list is not an entry\n"
    )
    assert list(sample) == ["One: a title", "Two: over two lines"]
    assert BREAKING.search(sample["One: a title"])
    assert not BREAKING.search(sample["Two: over two lines"])
    assert not BREAKING.search("a break-even of 11 µs, and the rule it breaks")
    assert _cited("Changelog: *One: a title* and *Two: over\ntwo lines*. **Bold** and `a*b`.") == {
        "One: a title",
        "Two: over two lines",
    }


def test_every_breaking_change_of_a_release_is_on_the_page() -> None:
    changelog = _sections(CHANGELOG.read_text(encoding="utf-8"))
    page = _sections(PAGE.read_text(encoding="utf-8"))
    held = {section for _, _, _, section in _pairs()}
    for heading, _, _, section in _pairs():
        cited = _cited(page[heading])
        breaking = [t for t, text in _entries(changelog[section]).items() if BREAKING.search(text)]
        assert breaking, f"{section!r} marks nothing as breaking, and the page has a section for it"
        missing = [title for title in breaking if title not in cited]
        assert not missing, (
            f"the changelog marks these as breaking and {PAGE.name}, {heading!r}, does not cite "
            f"them: {missing}"
        )
    unreleased = _entries(changelog["Unreleased"])
    if any(BREAKING.search(text) for text in unreleased.values()):
        assert "Unreleased" in held, (
            f"the changelog's Unreleased section marks a change as breaking, and {PAGE.name} has "
            f"no section 'From <latest release> to {NEXT}'"
        )


def test_every_title_the_page_cites_is_in_the_changelog() -> None:
    changelog = _sections(CHANGELOG.read_text(encoding="utf-8"))
    page = _sections(PAGE.read_text(encoding="utf-8"))
    for heading, _, _, section in _pairs():
        titles = set(_entries(changelog[section]))
        cited = _cited(page[heading])
        assert cited, f"{heading!r} cites no changelog entry"
        unknown = sorted(cited - titles)
        assert not unknown, (
            f"{PAGE.name}, {heading!r}, cites titles the changelog's {section!r} does not have "
            f"(italics on that page are changelog titles): {unknown}"
        )


def test_each_section_starts_from_the_release_before_it() -> None:
    """``From X``: X is the release the changelog lists just before the one the section reaches."""
    changelog = _sections(CHANGELOG.read_text(encoding="utf-8"))
    released = _released(changelog)
    for heading, start, end, _ in _pairs():
        if end == NEXT:
            assert start == released[0], (
                f"{heading!r}: the latest release in the changelog is {released[0]}. A release "
                f"renames this section to 'From {start} to <its number>'."
            )
        else:
            assert end in released and start in released, heading
            assert released.index(start) > released.index(end), heading
