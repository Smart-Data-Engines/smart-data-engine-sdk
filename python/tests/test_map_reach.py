"""What every name of a map reaches, looked up once a binding (format contract §7r).

The operator refuses a copy that another name of the map reaches - two engine names bound to one
database, an Elasticsearch alias. Asked per copy and per name, that was a quadratic count of round
trips (minutes for a map of two hundred groups); it is one look-up per binding a command, and each
copy is then a lookup in what it found. Offline, with bindings that count what they are asked.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import pytest

import sde
from sde.engines._operator import TableIdentity
from sde.errors import MigrationRefused
from sde.local_cutover import map_reach, refuse_another_name
from sde.testing.loader import model_from_neutral

PROJECT = "1" * 32
GROUPS = 40


def _map() -> Any:
    entities = []
    for number in range(GROUPS):
        entities.append(
            {"name": f"E{number:02d}", "fields": [{"name": "id", "type": "int64"}], "key": ["id"]}
        )
    model = model_from_neutral({"entities": entities, "relations": []})
    groups = {}
    for number in range(GROUPS):
        entity = f"E{number:02d}"
        groups[entity] = {
            "write_epoch": 1,
            "source": {"id": f"{entity}@a", "engine": "a",
                       "layout": {"tables": {entity: f"s{number:02d}"}}},
            "derived": [{"id": f"{entity}@b", "engine": "b",
                         "layout": {"tables": {entity: f"c{number:02d}"}},
                         "lag_budget_ms": 1000}],
            "also_write": [f"{entity}@b"],
        }
    return sde.load_map(
        {"contract": 4, "project_id": PROJECT, "model_version": model.version, "map_version": 1,
         "groups": groups},
        model=model,
    )


class _Binding:
    def __init__(self, database: str, aliases: dict[str, str] | None = None) -> None:
        self.database, self.aliases, self.calls = database, aliases or {}, 0

    def object_keys_many(self, tables: Sequence[str]) -> dict[str, frozenset[Any]]:
        self.calls += 1
        return {
            table: frozenset(
                {("postgres", "server", self.database, self.aliases.get(table, table))}
            )
            for table in tables
        }


def _identity(database: str, table: str) -> TableIdentity:
    return TableIdentity("postgres", "server", database, table, "public", table)


def test_each_binding_is_looked_up_once_for_every_copy_of_the_map() -> None:
    current = _map()
    bindings = {"a": _Binding("one"), "b": _Binding("two")}
    reach = map_reach(current, bindings)
    assert [binding.calls for binding in bindings.values()] == [1, 1]
    for number in range(GROUPS):
        entity = f"E{number:02d}"
        refuse_another_name(reach, entity, f"{entity}@b", f"c{number:02d}",
                            _identity("two", f"c{number:02d}"))
    assert [binding.calls for binding in bindings.values()] == [1, 1]


def test_a_copy_another_name_reaches_is_refused_and_named() -> None:
    current = _map()
    # Engine `a` bound to the copies' database, where source s07 is another name for copy c03.
    bindings = {"a": _Binding("two", {"s07": "c03"}), "b": _Binding("two")}
    reach = map_reach(current, bindings)
    with pytest.raises(MigrationRefused, match="is also E07's table 's07' for E07 in a"):
        refuse_another_name(reach, "E03", "E03@b", "c03", _identity("two", "c03"))
    # The control: every other copy is reached by its own name alone.
    refuse_another_name(reach, "E04", "E04@b", "c04", _identity("two", "c04"))
