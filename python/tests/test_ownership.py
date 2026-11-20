"""The mark that makes a table a copy of this project's group (format contract §7r), alone.

The paths that write the mark and read it are tested against live engines elsewhere; this holds
the rule they share: which marks make a table a copy of which group, and which do not.
"""

from __future__ import annotations

import pytest

from sde.ownership import (
    COPY_MARKER,
    SOURCE_MARKER,
    STAGE_MARKER,
    copy_marker,
    copy_refusal,
    owned_copy_marker,
    source_marker,
    stage_marker,
)

PROJECT = "0123456789abcdef0123456789abcdef"
OTHER_PROJECT = "fedcba9876543210fedcba9876543210"
STAGE = "aa" * 16
STAGED = f"sde_m_{STAGE}_000001"


def test_a_copy_mark_names_project_group_and_table() -> None:
    mark = copy_marker(PROJECT, "Order", "order_copy")
    assert COPY_MARKER.fullmatch(mark)
    assert mark.startswith(f"sde.copy1.{PROJECT}.")
    # Each of the three changes it.
    assert mark != copy_marker(OTHER_PROJECT, "Order", "order_copy")
    assert mark != copy_marker(PROJECT, "Payment", "order_copy")
    assert mark != copy_marker(PROJECT, "Order", "order")


def test_a_copy_is_ours_by_its_own_mark_and_by_no_other() -> None:
    mark = copy_marker(PROJECT, "Order", "order_copy")
    assert owned_copy_marker(PROJECT, "Order", "order_copy", mark)
    # The control first: no mark is not ours - the table a hostile map names by a client's name.
    assert not owned_copy_marker(PROJECT, "Order", "order_copy", None)
    assert not owned_copy_marker(PROJECT, "Order", "order_copy", "")
    # Another group's copy, named as this group's: the mark says which group it was made for.
    assert not owned_copy_marker(PROJECT, "Payment", "order_copy", mark)
    # Another project's, and another table's.
    assert not owned_copy_marker(OTHER_PROJECT, "Order", "order_copy", mark)
    assert not owned_copy_marker(PROJECT, "Order", "order", mark)


def test_a_staged_copy_is_ours_by_its_stage_mark_under_its_stage_name() -> None:
    mark = stage_marker(PROJECT, STAGE, STAGED)
    assert STAGE_MARKER.fullmatch(mark)
    assert owned_copy_marker(PROJECT, "Order", STAGED, mark)
    assert not owned_copy_marker(OTHER_PROJECT, "Order", STAGED, mark)
    # A stage's mark on a table not named after that stage is not a stage's copy.
    elsewhere = stage_marker(PROJECT, STAGE, "order_copy")
    assert not owned_copy_marker(PROJECT, "Order", "order_copy", elsewhere)
    other_stage = f"sde_m_{'bb' * 16}_000001"
    assert not owned_copy_marker(PROJECT, "Order", other_stage, mark)


def test_a_mark_needs_the_project_s_id() -> None:
    with pytest.raises(ValueError, match="32 lowercase hexadecimal"):
        copy_marker("not-a-project", "Order", "order_copy")


def test_a_cutover_s_source_mark_is_never_a_copy_s() -> None:
    """A copy a cutover made the source carries a source's mark (format contract §7r): the same
    project, group and table, and no longer taken for the copy - its rows are the application's."""
    project = "1" * 32
    mark = source_marker(project, "Event", "copy_events")
    assert mark == "sde.source1.11111111111111111111111111111111.4e1f49a9c8ae8a15.d956589b44907f75"
    assert SOURCE_MARKER.fullmatch(mark)
    assert not owned_copy_marker(project, "Event", "copy_events", mark)
    refusal = copy_refusal("copy_events", mark)
    assert "is not one this library created for it" in refusal
    assert "carries a source's mark" in refusal
    with pytest.raises(ValueError):
        source_marker("NOT-A-PROJECT", "Event", "copy_events")

