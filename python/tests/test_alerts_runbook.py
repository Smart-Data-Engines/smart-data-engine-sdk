"""docs/alerts.md says what to do about every event the library can emit, and about nothing else.

The vocabulary is closed so that an alert built on a name keeps working; a runbook that missed a
name, or kept one the library no longer emits, would be the place that stopped being true first.
"""

from __future__ import annotations

import re
from pathlib import Path

from sde.logging import EVENTS

RUNBOOK = Path(__file__).resolve().parents[2] / "docs" / "alerts.md"


def test_every_event_has_a_row_and_every_row_is_an_event() -> None:
    text = RUNBOOK.read_text(encoding="utf-8")
    rows = re.findall(r"^\| `(sde\.[a-z_]+\.[a-z_]+)` \|", text, re.M)
    assert len(rows) == len(set(rows)), "an event has two rows"
    assert set(rows) == set(EVENTS)


def test_each_row_says_whether_to_page() -> None:
    for line in RUNBOOK.read_text(encoding="utf-8").splitlines():
        if line.startswith("| `sde."):
            page = line.split("|")[3].strip()
            assert page.startswith(("yes", "no", "on its rate")), line
