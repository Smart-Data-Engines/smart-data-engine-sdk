"""Whether a table the operator is about to write to is a copy of this group (contract §7r).

A copy is written over, emptied, refilled and deleted from, so before any of that the operator reads
the mark its creator wrote on it: ``prepare_schema``'s for this project, group and table, or the one
a staging of this project wrote on the copy it named - a staging this project completed for this
group. A table of the copy's name without either is somebody else's.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from ..errors import MigrationRefused
from ..ownership import STAGE2_MARKER, STAGE_MARKER, copy_refusal, owned_copy_marker
from ._operator import TableIdentity
from ._staging import native_staging


def copy_owned(
    native: Any,
    *,
    project_id: str,
    group: str,
    table: str,
    stages: Mapping[str, Any],
) -> TableIdentity:
    """The identity of ``table``, a copy of ``group``, once its mark says so; or a refusal."""
    found = native_staging(native).marker(table)
    mark = None if found is None else found[1]
    if found is None or not owned_copy_marker(project_id, group, table, mark):
        raise MigrationRefused(copy_refusal(table, mark))
    assert mark is not None
    if STAGE_MARKER.fullmatch(mark) or STAGE2_MARKER.fullmatch(mark):
        receipt = (stages.get(mark.split(".")[3]) or {}).get("receipt") or {}
        if receipt.get("outcome") != "prepared" or receipt.get("group") != group:
            raise MigrationRefused(
                f"the copy's table {table!r} carries the mark of a staging this project did not "
                f"complete for {group}: it is not this group's copy"
            )
    identity: TableIdentity = native.identity(table)
    if identity.object != found[0]:
        raise MigrationRefused(f"the copy's table {table!r} changed while its mark was read")
    return identity
