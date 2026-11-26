"""Whose table a copy is: the marker a library writes on a copy's table when it creates it.

A placement map names a copy's tables, and the library writes to them: the operator backfills,
repairs, empties and refills a copy and deletes a person's rows from it, and an application writes
to it alongside the source. A map is the control plane's word, so the name alone cannot be what
makes a table a copy: a copy named like a table the client already has - another group's, or one
outside SDE - would be written over. So the library that creates a copy's table marks it, and every
path that writes to a copy reads the mark first (format contract §7r).

Two marks make a copy's table ours. ``prepare_schema`` writes
``sde.copy1.<project>.<sha256(group)[:16]>.<sha256(table)[:16]>`` on the copies of the map it
provisions; a staging writes ``sde.stage1.<project>.<stage>.<sha256(table)[:16]>`` on the copy it
creates, which it names ``sde_m_<stage>_<NNNNNN>``. Both are computed from the project, the map and
the table name alone, so any path can check them without local state.

A cutover that makes a copy the source writes, in place of the copy's mark,
``sde.source1.<project>.<sha256(group)[:16]>.<sha256(table)[:16]>``: a copy's mark says the library
created the table as a copy, and left there it would still say so of a table holding the
application's rows. A table with a source's mark is never taken for a copy again.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING, Final, Protocol

if TYPE_CHECKING:
    from .physical import PhysicalFinding
    from .placement import PhysicalLayout

__all__ = [
    "COPY_MARKER",
    "SOURCE_MARKER",
    "STAGE_MARKER",
    "MarksCopies",
    "copy_marker",
    "copy_refusal",
    "owned_copy_marker",
    "source_marker",
    "source_refusal",
    "stage_marker",
]

COPY_MARKER: Final = re.compile(r"sde\.copy1\.[0-9a-f]{32}\.[0-9a-f]{16}\.[0-9a-f]{16}")
"""What ``prepare_schema`` writes on a copy's table it creates: its project, group and name."""

STAGE_MARKER: Final = re.compile(r"sde\.stage1\.[0-9a-f]{32}\.[0-9a-f]{32}\.[0-9a-f]{16}")
"""What a staging writes on the table it creates: its project, its stage and the table's name."""

SOURCE_MARKER: Final = re.compile(r"sde\.source1\.[0-9a-f]{32}\.[0-9a-f]{16}\.[0-9a-f]{16}")
"""What a cutover writes on the copy it makes the source, in place of the copy's mark."""

_STAGED_NAME: Final = re.compile(r"sde_m_([0-9a-f]{32})_[0-9]{6}")
_PROJECT: Final = re.compile(r"[0-9a-f]{32}")


def _digest(text: str, length: int) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:length]


def copy_marker(project_id: str, group: str, table: str) -> str:
    """The mark ``prepare_schema`` writes on ``table``, a copy of ``group`` in ``project_id``."""
    if _PROJECT.fullmatch(project_id) is None:
        raise ValueError("a copy's mark needs the project's id: 32 lowercase hexadecimal digits")
    return f"sde.copy1.{project_id}.{_digest(group, 16)}.{_digest(table, 16)}"


def source_marker(project_id: str, group: str, table: str) -> str:
    """The mark a cutover writes on ``table`` when it makes that copy of ``group`` the source."""
    if _PROJECT.fullmatch(project_id) is None:
        raise ValueError("a source's mark needs the project's id: 32 lowercase hexadecimal digits")
    return f"sde.source1.{project_id}.{_digest(group, 16)}.{_digest(table, 16)}"


def stage_marker(project_id: str, stage_id: str, table: str) -> str:
    """The mark a staging writes on ``table``, the copy it creates for stage ``stage_id``."""
    return f"sde.stage1.{project_id}.{stage_id}.{_digest(table, 16)}"


def copy_refusal(table: str, found: str | None) -> str:
    """Why ``table`` is not taken as a copy's: what every library says, with what it found."""
    if found and SOURCE_MARKER.fullmatch(found):
        return (
            f"the copy's table {table!r} exists and is not one this library created for it: it "
            f"carries a source's mark. A cutover made it the source, and a table that has been a "
            f"source holds the application's rows: it is never a copy again."
        )
    what = "no mark" if not found else "another mark"
    return (
        f"the copy's table {table!r} exists and is not one this library created for it: it "
        f"carries {what}. A copy is written over, emptied and refilled, so a table of its name and "
        f"shape is not taken for it. A copy provisioned before copies were marked is made again "
        f"(docs/upgrading.md); a table of yours is given another name in the map."
    )


def source_refusal(table: str, group: str) -> str:
    """Why a source's table that carries a copy's mark is not taken as ``group``'s source."""
    return (
        f"the source's table {table!r} of {group} carries a copy's mark: a copy becomes the source "
        f"only through a cutover, which writes a source's mark in place of the copy's, so this is "
        f"a copy a map names as a source (format contract §7r)"
    )


def owned_copy_marker(
    project_id: str, group: str, table: str, found: str | None
) -> bool:
    """Whether ``found``, the mark read on ``table``, makes it a copy of ``group`` in this project.

    The mark ``prepare_schema`` writes for exactly this group and table, or the one a staging of
    this project writes on the copy it names after its stage. A staging's mark does not name the
    group: the operator, which knows which group each stage was for, checks that beside this.
    """
    if found is None:
        return False
    if found == copy_marker(project_id, group, table):
        return True
    staged = _STAGED_NAME.fullmatch(table)
    return staged is not None and found == stage_marker(project_id, staged.group(1), table)


class MarksCopies(Protocol):
    """An adapter that creates a copy's tables marked and refuses one without its mark (§7r).

    A protocol of its own rather than an argument of ``ensure_schema``, because ``Engine`` is what
    a third party implements to plug a database in: growing it would break each of those adapters on
    upgrade. ``prepare_schema`` asks for this by name and refuses a copy whose adapter has none.
    """

    def ensure_copy(
        self,
        layout: PhysicalLayout,
        *,
        keys: Mapping[str, Sequence[str]],
        project_id: str,
        group: str,
    ) -> tuple[PhysicalFinding, ...]: ...
