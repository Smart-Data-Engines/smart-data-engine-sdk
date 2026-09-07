"""One detector for the idiom our documents use to say a thing is not built yet.

Three documents make that claim, to three different readers: `README.md` to a client deciding
whether to depend on this, `docs/implementing.md` to someone writing a fifth library, and
`docs/implementations.md` to whoever has to know who fixes a defect. **A claim of absence is the one
kind that cannot fail on its own.** A claim of presence fails the moment somebody tries to use the
thing; "there is no migration engine" keeps reading correctly for as long as nobody rereads it,
which was four days - the migration engine landed on 2 September 2026 and the front page of the
public repository still denied it on the 6th, four lines above a table claiming the tier that
*is* migration.

The detector lives here rather than inside either test because two copies of it is how the phrasing
drifts. A README that started saying "does not yet exist" would slip past a checker that only knew
the other word order, and it would slip past it in the document where the stakes are highest. There
is one regex, and both callers get whatever it learns.

**Past tense is deliberately not matched.** "Until 6 September the `schema/` vectors did not exist"
is a true sentence about history and both documents need to be able to say it; the claim worth
auditing is the one in the present tense, because that is the one a reader acts on.
"""

from __future__ import annotations

import re

#: The forward-looking absence idiom, in the word orders our prose actually uses.
ABSENCE = re.compile(r"\b(?:do|does) not (?:yet )?exist\b")

#: Sentence boundary, coarse on purpose: a full stop, or a paragraph break. Prose in these files
#: puts one claim per sentence, and reading a whole paragraph as one claim was the first version's
#: defect - it let a true sentence vouch for a false one standing next to it.
_BREAK = re.compile(r"(?<=[.!?])\s+|\n{2,}|\n(?=[|#])")

#: The implementations that were written as measurements and deliberately kept out of the tree.
#:
#: This one is a written list and it has to be: the point of a measurement is that its source is
#: *not* here, so there is nothing in the tree to derive it from. What single-sourcing it buys is
#: that the day a third one is run, one tuple changes and every document that owes the reader a
#: paragraph about it fails until it has one.
MEASUREMENTS = (("Go", ".go"), ("Rust", ".rs"))


def sentences(text: str) -> list[str]:
    """The text as claims, one per sentence."""
    return [part.strip() for part in _BREAK.split(text) if part.strip()]


def absence_claims(text: str) -> list[str]:
    """Every sentence that says, in the present tense, that something is not built yet."""
    return [sentence for sentence in sentences(text) if ABSENCE.search(sentence)]
