"""The marks of a map's tables, read where a library writes to them (format contract §7r).

``prepare_schema`` creates a copy's tables with a mark, and the operator reads it before it writes
to a copy. An application's session wrote to a map's copies - its fan-out - without reading it, so
a forged map could still direct those writes to a table of the project of the copy's shape: another
group's staged copy, a copy a previous map named as a source, a copy another engine name of the map
reaches. A session of a map with write generations now reads, when it opens, every table of the
map through its binding, once a binding: a copy's table carries its own mark, a source's carries no
copy's, and no two names of the map reach one table - a copy's or a source's. ``prepare_schema``
reads the same before its first statement and again after its last.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from .errors import MigrationRefused
from .ownership import COPY_MARKER, copy_refusal, owned_copy_marker, source_refusal
from .placement import PlacementMap

__all__ = ["check_table_marks"]

_Named = tuple[str, str, str, str, bool]
"""A name of the map: group, materialisation id, entity, table, and whether it is a copy's."""


def check_table_marks(
    placement: PlacementMap,
    engines: Mapping[str, Any],
    project_id: str,
    *,
    copies_present: bool = True,
) -> None:
    """Refuse a map whose tables are not what it calls them (format contract §7r).

    For the groups with a write generation: each copy's table carries this copy's mark, each
    source's table carries no copy's mark, and no copy's or source's table is reached by another
    name of the map. One ``table_facts`` a binding. ``copies_present`` is false before
    ``prepare_schema`` creates the copies: a copy's table that is not there yet is not refused
    then. A copy on an engine whose adapter cannot read marks is refused; a source there is taken,
    as sources are.
    Groups in name order, materialisations in document order, entities in name order; the marks
    before the names.
    """
    if placement.contract < 4:
        return
    named: dict[str, list[_Named]] = {}
    for group, spot in sorted(placement.groups.items()):
        if spot.write_epoch is None:
            continue
        for material in spot.all():
            copy = material is not spot.source
            for entity, table in sorted(material.layout.tables.items()):
                named.setdefault(material.engine, []).append(
                    (group, material.id, entity, table, copy)
                )
    facts: dict[str, Mapping[str, tuple[frozenset[tuple[str, ...]], str | None]]] = {}
    for engine, names in sorted(named.items()):
        read = getattr(engines[engine], "table_facts", None)
        if callable(read):
            facts[engine] = read([table for *_, table, _copy in names])
        elif any(copy for *_, copy in names):
            raise MigrationRefused(
                f"engine {engine} holds a copy, and its adapter cannot read the mark a copy's "
                f"table carries (table_facts, format contract §7r)"
            )
    ordered = [
        (engine, entry)
        for group, spot in sorted(placement.groups.items())
        for material in spot.all()
        for engine in [material.engine]
        for entry in named.get(engine, ())
        if entry[0] == group and entry[1] == material.id
    ]
    reach: dict[tuple[str, ...], list[tuple[str, _Named]]] = {}
    for engine, entry in ordered:
        if engine not in facts:
            continue
        group, _material, _entity, table, copy = entry
        keys, mark = facts[engine][table]
        if copy:
            if keys and not owned_copy_marker(project_id, group, table, mark):
                raise MigrationRefused(copy_refusal(table, mark))
            if not keys and copies_present:
                raise MigrationRefused(
                    f"the copy's table {table!r} of {group} is not there: provision the map "
                    f"(prepare_schema) before a session writes to it"
                )
        elif mark is not None and COPY_MARKER.fullmatch(mark):
            raise MigrationRefused(source_refusal(table, group))
        for key in sorted(keys):
            reach.setdefault(key, []).append((engine, entry))
    for engine, entry in ordered:
        group, materialisation, _entity, table, copy = entry
        if engine not in facts:
            continue
        for key in sorted(facts[engine][table][0]):
            for other_engine, other in reach.get(key, ()):
                if (other[0], other[1], other[3]) != (group, materialisation, table):
                    whose, alone = ("copy's", "that copy") if copy else ("source's", "its group")
                    raise MigrationRefused(
                        f"the {whose} table {table!r} of {group} is also {other[0]}'s table "
                        f"{other[3]!r} for {other[2]} in {other_engine}: a {whose} table holds "
                        f"{alone} alone, and two names of the map reach this one"
                    )
