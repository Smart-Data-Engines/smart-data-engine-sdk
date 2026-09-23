"""The physical design vocabulary a placement map can carry, and the rules each element obeys.

Placement map contract 5 lets a layout say three things about storage beyond tables and column
types: the physical order of an entity's key, a time partition, and indexes of a named method. They
are chosen by the control plane - since 23 September 2026 by a model whose proposal is validated -
and a library only renders them. Every element is a closed vocabulary rendered by this library;
nothing in a map is pasted into DDL.

This module is the one place the vocabulary lives. The map parser, the DDL renderer, the adapters'
verification and the control plane's description of what a dialect can render all read it, because
a second copy of "which index methods ClickHouse has" is how a map gets signed that no library can
apply.

Two rules here are about data rather than performance, and both were measured before they were
written (PostgreSQL 15.19, ClickHouse 24.8.14.39, 23 September 2026):

- **A partition follows the key.** `ReplacingMergeTree` collapses rows with one key only inside a
  partition. Partitioned on a column outside the key, two writes of one key that land in different
  partitions stay two rows *physically forever*: ``FINAL`` hid the duplicate only because
  ``do_not_merge_across_partitions_select_final`` was 0, and after ``OPTIMIZE ... FINAL`` both rows
  remained. Partitioned on a key column, one key always lands in one partition.
- **The logical key is the identity; `key_order` only reorders it.** A permutation of the declared
  key keeps the same deduplication and the same uniqueness, measured in both engines, while choosing
  which prefix the engine can prune or seek by.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Final, TypeGuard

from .errors import MapError

__all__ = [
    "CLICKHOUSE_METHODS",
    "GRANULARITIES",
    "INDEX_METHODS",
    "PHYSICAL_DESIGN_SINCE",
    "POSTGRES_METHODS",
    "TEMPORAL_TYPES",
    "capabilities",
    "index_method",
]

PHYSICAL_DESIGN_SINCE: Final = 5
"""The placement map contract that introduced ``key_order``, ``partition_by`` and index methods."""

GRANULARITIES: Final[tuple[str, ...]] = ("day", "month", "year")
"""Time partition sizes.

No week: ClickHouse's ``toStartOfWeek`` depends on a mode (Sunday or Monday first), and a closed
vocabulary has no modes. A week that meant two different things in two deployments would be exactly
the ambiguity this list exists to remove.
"""

TEMPORAL_TYPES: Final[tuple[str, ...]] = ("date", "timestamptz")
"""Neutral types a partition may be derived from.

``timestamp`` is absent on purpose. Its ClickHouse column carries no zone, so the day, month or
year a value falls into is computed in the server's configured timezone at insert time - a server
reconfigured later would put the same key into another partition, and two partitions of one key
are two rows for ever. A session's ``session_timezone`` does not move it (measured, 24.8); the
server's zone is documented to. ``date`` and ``timestamptz`` (``DateTime64(6, 'UTC')``) do not
depend on any configuration.
"""

POSTGRES_METHODS: Final[tuple[str, ...]] = ("brin", "btree")
CLICKHOUSE_METHODS: Final[tuple[str, ...]] = ("bloom_filter", "minmax", "set")
"""ClickHouse data-skipping index types. It has no B-tree; its primary index is ``ORDER BY``."""

INDEX_METHODS: Final[tuple[str, ...]] = tuple(sorted(POSTGRES_METHODS + CLICKHOUSE_METHODS))

GRANULARITY_RANGE: Final[tuple[int, int]] = (1, 1024)
"""Granules summarised by one entry of a data-skipping index. Bounded so a typo cannot ask for an
index that summarises nothing (0) or the whole table."""

SET_ROWS_RANGE: Final[tuple[int, int]] = (1, 65536)
"""``set(N)`` keeps up to N distinct values per entry. ``set(0)`` means unlimited, which is an
unbounded memory commitment, so zero is outside the range rather than a default."""

LEGACY_INDEX_KEYS: Final = frozenset({"entity", "name", "columns"})
INDEX_KEYS: Final = LEGACY_INDEX_KEYS | {"method", "granularity", "max_rows"}

PARTITION_FUNCTIONS: Final[Mapping[str, str]] = {
    "day": "toDate",
    "month": "toYYYYMM",
    "year": "toYear",
}
"""ClickHouse partition expressions, as its catalogue reports them back."""

PARTITIONING_DIALECTS: Final = frozenset({"clickhouse"})
"""Dialects that render ``partition_by``.

PostgreSQL is absent on purpose. Declarative partitioning there needs every partition created
before a row can arrive, which is a lifecycle this product does not manage; a map that declared it
and got an unpartitioned table would be the silent drop this vocabulary refuses everywhere else.
"""

METHODS_BY_DIALECT: Final[Mapping[str, tuple[str, ...]]] = {
    "clickhouse": CLICKHOUSE_METHODS,
    "postgres": POSTGRES_METHODS,
}


def index_method(index: Mapping[str, Any]) -> str:
    """An index's method. Absent means ``btree``, what every map before contract 5 meant."""
    return str(index.get("method", "btree"))


def _integer(value: Any) -> TypeGuard[int]:
    return isinstance(value, int) and not isinstance(value, bool)


def parse_key_order(
    raw: Any, where: str, *, tables: Mapping[str, str]
) -> dict[str, tuple[str, ...]]:
    """``key_order``: entity -> the physical order of that entity's key columns."""
    if not isinstance(raw, dict):
        raise MapError(f"{where}: key_order maps an entity to a list of its key columns")
    out: dict[str, tuple[str, ...]] = {}
    for entity in sorted(raw):
        columns = raw[entity]
        if entity not in tables:
            raise MapError(
                f"{where}: key_order names {entity!r}, which has no table in this layout. It has "
                f"{sorted(tables)}."
            )
        if (
            not isinstance(columns, list)
            or not columns
            or not all(isinstance(column, str) and column for column in columns)
        ):
            raise MapError(
                f"{where}: key_order[{entity!r}] must be a non-empty list of column names"
            )
        if len(set(columns)) != len(columns):
            raise MapError(
                f"{where}: key_order[{entity!r}] names a column twice: {columns}. A key order is a "
                f"permutation of the key, so each key column appears exactly once."
            )
        out[entity] = tuple(columns)
    return out


def parse_partition_by(
    raw: Any, where: str, *, tables: Mapping[str, str]
) -> dict[str, dict[str, str]]:
    """``partition_by``: entity -> ``{"field": ..., "granularity": ...}``, and nothing else."""
    if not isinstance(raw, dict):
        raise MapError(f"{where}: partition_by maps an entity to a {{field, granularity}} object")
    out: dict[str, dict[str, str]] = {}
    for entity in sorted(raw):
        spec = raw[entity]
        if entity not in tables:
            raise MapError(
                f"{where}: partition_by names {entity!r}, which has no table in this layout. It "
                f"has {sorted(tables)}."
            )
        if not isinstance(spec, dict) or set(spec) != {"field", "granularity"}:
            raise MapError(
                f"{where}: partition_by[{entity!r}] must be exactly "
                f'{{"field": ..., "granularity": ...}}; found keys '
                f"{sorted(spec) if isinstance(spec, dict) else type(spec).__name__}. The "
                f"vocabulary is closed so that nothing in a map is pasted into DDL."
            )
        field_name, granularity = spec["field"], spec["granularity"]
        if not isinstance(field_name, str) or not field_name:
            raise MapError(f"{where}: partition_by[{entity!r}].field must name a column")
        if granularity not in GRANULARITIES:
            raise MapError(
                f"{where}: partition_by[{entity!r}].granularity is {granularity!r}; it is one of "
                f"{list(GRANULARITIES)}."
            )
        out[entity] = {"field": field_name, "granularity": str(granularity)}
    return out


def parse_indexes(
    raw: Any,
    where: str,
    *,
    tables: Mapping[str, str],
    columns: Mapping[str, Mapping[str, str]],
    contract: int,
) -> tuple[dict[str, Any], ...]:
    """Index definitions, validated where the map arrives rather than when DDL is rendered.

    The structural half applies to every contract - an index without an entity, a name or columns
    used to surface as a bare ``KeyError`` from the renderer, in the client's process - and is a
    tightening. The method, granularity and ``max_rows`` keys are contract 5: an earlier library
    ignores them, so a contract-4 document carrying ``"method": "brin"`` would be applied as a
    B-tree by one library and as BRIN by another.
    """
    if raw is None:
        return ()
    if not isinstance(raw, list):
        raise MapError(f"{where}: indexes is a list of index definitions")
    allowed = INDEX_KEYS if contract >= PHYSICAL_DESIGN_SINCE else LEGACY_INDEX_KEYS
    out: list[dict[str, Any]] = []
    names: set[str] = set()
    for position, index in enumerate(raw):
        at = f"{where}: indexes[{position}]"
        if not isinstance(index, dict):
            raise MapError(f"{at} must be an object")
        unknown = sorted(set(index) - allowed)
        if unknown:
            later = sorted(set(unknown) & (INDEX_KEYS - LEGACY_INDEX_KEYS))
            if later:
                raise MapError(
                    f"{at} uses {later}, which placement map contract {PHYSICAL_DESIGN_SINCE} "
                    f"introduced, in a document declaring contract {contract}. A library of that "
                    f"contract would ignore the key and create a different index from the same "
                    f"document, which is the difference the version exists to prevent."
                )
            raise MapError(f"{at} has keys this format does not define: {unknown}")
        entity, name, cols = index.get("entity"), index.get("name"), index.get("columns")
        if not isinstance(entity, str) or entity not in tables:
            raise MapError(
                f"{at} names entity {entity!r}, which has no table in this layout. It has "
                f"{sorted(tables)}."
            )
        if not isinstance(name, str) or not name:
            raise MapError(f"{at} needs a non-empty name")
        if name in names:
            raise MapError(f"{at} reuses the index name {name!r}")
        names.add(name)
        if (
            not isinstance(cols, list)
            or not cols
            or not all(isinstance(column, str) and column for column in cols)
            or len(set(cols)) != len(cols)
        ):
            raise MapError(f"{at} needs a non-empty list of distinct column names")
        declared = columns.get(entity)
        if declared:
            missing = [column for column in cols if column not in declared]
            if missing:
                raise MapError(
                    f"{at} indexes {missing}, which {entity!r} does not have in this layout. An "
                    f"index on a missing column is refused by the engine at CREATE INDEX, in the "
                    f"client's process, rather than here."
                )
        method = index.get("method", "btree")
        if method not in INDEX_METHODS:
            raise MapError(f"{at} has method {method!r}; the methods are {list(INDEX_METHODS)}")
        granularity, max_rows = index.get("granularity"), index.get("max_rows")
        if method in CLICKHOUSE_METHODS:
            low, high = GRANULARITY_RANGE
            if not _integer(granularity) or not low <= granularity <= high:
                raise MapError(
                    f"{at}: a {method} index needs an integer granularity from {low} to {high}; "
                    f"found {granularity!r}"
                )
            if len(cols) != 1:
                raise MapError(
                    f"{at}: a {method} index summarises exactly one column, found {cols}"
                )
        elif "granularity" in index:
            # Presence, not `is not None`: a `null` here is still a key this method does not
            # have, and TypeScript cannot tell the two apart the way `dict.get` blurs them.
            raise MapError(f"{at}: granularity belongs to data-skipping indexes, not {method}")
        if method == "set":
            low, high = SET_ROWS_RANGE
            if not _integer(max_rows) or not low <= max_rows <= high:
                raise MapError(
                    f"{at}: a set index needs an integer max_rows from {low} to {high}; found "
                    f"{max_rows!r}. Zero means unlimited in ClickHouse, which is not a choice this "
                    f"format offers."
                )
        elif "max_rows" in index:
            raise MapError(f"{at}: max_rows belongs to set indexes, not {method}")
        out.append(dict(index))
    return tuple(out)


def check_against_model(
    where: str,
    *,
    entity: str,
    key: Sequence[str],
    field_types: Mapping[str, str],
    key_order: Sequence[str] | None,
    partition: Mapping[str, str] | None,
) -> None:
    """The two rules that need the model: a permutation of the key, and a partition on a key column.

    Run when a map is loaded with its model. A renderer without the model repeats the key half
    against the keys it is given (:func:`effective_key`, :func:`partition_expression`), which is
    what keeps the data rule true for a caller that skipped the model.
    """
    if key_order is not None and sorted(key_order) != sorted(key):
        raise MapError(
            f"{where}: key_order[{entity!r}] is {list(key_order)} and the declared key is "
            f"{list(key)}. A key order reorders the key; it cannot add, drop or replace a column, "
            f"because the key is what makes a row the same row in every engine."
        )
    if partition is not None:
        field_name = partition["field"]
        if field_name not in key:
            raise MapError(
                f"{where}: partition_by[{entity!r}] partitions on {field_name!r}, which is not in "
                f"the key {list(key)}. ClickHouse collapses rows of one key only inside one "
                f"partition, so two writes of one key could land in two partitions and stay two "
                f"rows physically for ever - measured: after OPTIMIZE FINAL both remained."
            )
        kind = field_types.get(field_name)
        if kind not in TEMPORAL_TYPES:
            raise MapError(
                f"{where}: partition_by[{entity!r}] partitions on {field_name!r}, which is "
                f"{kind!r}; a time partition needs one of {list(TEMPORAL_TYPES)}."
            )


def effective_key(
    where: str,
    *,
    entity: str,
    key: Sequence[str],
    key_order: Mapping[str, Sequence[str]],
    error: type[Exception] = MapError,
) -> list[str]:
    """The key in physical order: ``key_order`` when present, the declared order otherwise."""
    ordered = key_order.get(entity)
    if ordered is None:
        return list(key)
    if sorted(ordered) != sorted(key):
        raise error(
            f"{where}: key_order[{entity!r}] is {list(ordered)} and the key is {list(key)}. A key "
            f"order must be a permutation of the key."
        )
    return list(ordered)


def partition_expression(
    where: str,
    *,
    entity: str,
    key: Sequence[str],
    partition: Mapping[str, str] | None,
    error: type[Exception] = MapError,
) -> tuple[str, str] | None:
    """``(function, field)`` for a ClickHouse partition, after the key rule is checked again."""
    if partition is None:
        return None
    field_name = partition["field"]
    if field_name not in key:
        raise error(
            f"{where}: partition_by[{entity!r}] partitions on {field_name!r}, outside the key "
            f"{list(key)}; duplicates of one key would survive merges in two partitions."
        )
    return PARTITION_FUNCTIONS[partition["granularity"]], field_name


def capabilities(dialect: str) -> dict[str, Any]:
    """What a dialect renders, for whoever has to propose a physical design for it.

    The control plane hands this to a model so the vocabulary it is offered is the vocabulary this
    library will render - derived here, not restated there.
    """
    if dialect not in METHODS_BY_DIALECT:
        return {"physical_design": False}
    methods: dict[str, Any] = {}
    for method in METHODS_BY_DIALECT[dialect]:
        entry: dict[str, Any] = {}
        if method in CLICKHOUSE_METHODS:
            entry["granularity"] = list(GRANULARITY_RANGE)
            entry["columns"] = 1
        if method == "set":
            entry["max_rows"] = list(SET_ROWS_RANGE)
        methods[method] = entry
    return {
        "physical_design": True,
        "key_order": True,
        "partition_granularities": (
            list(GRANULARITIES) if dialect in PARTITIONING_DIALECTS else []
        ),
        "partition_on": "a key column of type date or timestamptz",
        "index_methods": methods,
    }


# ── Reading the physical design back from an engine ──────────────────────────────────────────────
#
# `CREATE TABLE IF NOT EXISTS` keeps an existing table whatever its sort key or partition, and
# PostgreSQL's `CREATE INDEX IF NOT EXISTS ... USING brin` keeps an existing B-tree of that name -
# both measured. So what an engine holds is read from its catalogue after the statements run, and
# compared with what the layout declares. Where the catalogue speaks a formatted expression
# (ClickHouse), it is parsed into names rather than compared as a string we predict: ClickHouse
# 24.8 leaves `select` and `order` bare and quotes `null` and `ząb`, and a formatting rule that
# changes between releases would turn a correct table into a refusal.


@dataclass(frozen=True)
class PhysicalFinding:
    """One way an existing table differs from the physical design its layout declares."""

    table: str
    aspect: str
    declared: str
    found: str

    def __str__(self) -> str:
        return f"{self.table}: {self.aspect} is {self.found} and the map declares {self.declared}"


@dataclass(frozen=True)
class DeclaredIndex:
    name: str
    method: str
    columns: tuple[str, ...]
    granularity: int | None = None
    type_full: str = ""


@dataclass(frozen=True)
class DeclaredTable:
    """What one table should look like physically, derived from a layout and the model's keys."""

    table: str
    key: tuple[str, ...]
    partition: tuple[str, str] | None
    indexes: tuple[DeclaredIndex, ...]


def declared_tables(layout: Any, keys: Mapping[str, Sequence[str]]) -> tuple[DeclaredTable, ...]:
    """The physical expectations of every table a layout names, in table order."""
    out: list[DeclaredTable] = []
    for entity, table in sorted(layout.tables.items(), key=lambda item: item[1]):
        key = list(keys.get(entity, ()))
        ordered = effective_key(
            f"table {table!r}", entity=entity, key=key, key_order=layout.key_order
        )
        partition = partition_expression(
            f"table {table!r}", entity=entity, key=key, partition=layout.partition_by.get(entity)
        )
        indexes = []
        for index in sorted(layout.indexes, key=lambda item: str(item["name"])):
            if str(index["entity"]) != entity:
                continue
            method = index_method(index)
            granularity = index.get("granularity")
            type_full = f"set({index['max_rows']})" if method == "set" else method
            indexes.append(
                DeclaredIndex(
                    name=str(index["name"]),
                    method=method,
                    columns=tuple(str(column) for column in index["columns"]),
                    granularity=int(granularity) if granularity is not None else None,
                    type_full=type_full,
                )
            )
        out.append(DeclaredTable(str(table), tuple(ordered), partition, tuple(indexes)))
    return tuple(out)


_BARE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def parse_identifier_list(text: str) -> tuple[str, ...]:
    """``a, `b c`, d`` → ``("a", "b c", "d")``: names as ClickHouse's catalogue writes them.

    A name is either bare or backtick-quoted with ``\\`` escaping ``\\`` and the backtick - the
    same rule this library writes (``_quote_backtick``). Anything else raises ``ValueError``: an
    expression the parser does not understand is not evidence that the table matches.
    """
    names: list[str] = []
    position, length = 0, len(text)
    while position < length:
        if text[position] == "`":
            position += 1
            name: list[str] = []
            while True:
                if position >= length:
                    raise ValueError(f"unterminated quoted name in {text!r}")
                char = text[position]
                if char == "\\":
                    if position + 1 >= length:
                        raise ValueError(f"dangling escape in {text!r}")
                    name.append(text[position + 1])
                    position += 2
                    continue
                if char == "`":
                    position += 1
                    break
                name.append(char)
                position += 1
            names.append("".join(name))
        else:
            match = _BARE.match(text, position)
            if match is None:
                raise ValueError(f"not a list of column names: {text!r}")
            names.append(match.group(0))
            position = match.end()
        if position < length:
            # A separator is only ever between two names: ", " at the end is not a list.
            if not text.startswith(", ", position) or position + 2 >= length:
                raise ValueError(f"not a list of column names: {text!r}")
            position += 2
    return tuple(names)


def parse_partition_key(text: str) -> tuple[str, str] | None:
    """``toYYYYMM(`at`)`` → ``("toYYYYMM", "at")``; an empty key → ``None``."""
    if text == "":
        return None
    match = re.fullmatch(r"([A-Za-z][A-Za-z0-9]*)\((.*)\)", text)
    if match is None:
        raise ValueError(f"not a single-function partition key: {text!r}")
    (field_name,) = parse_identifier_list(match.group(2))
    return match.group(1), field_name


def refuse_findings(findings: Sequence[PhysicalFinding], error: type[Exception]) -> None:
    """Raise when a table differs physically. The provisioning paths call this; sessions do not.

    A session only reports (``Session.physical``): the difference is performance, and requirement
    3.6 forbids turning it into the application's outage. Provisioning is where a person applying a
    map can act on it, so there it is a refusal naming the table, the aspect and both values.
    """
    if findings:
        details = "; ".join(str(finding) for finding in findings)
        raise error(
            f"existing tables differ from the physical design this map declares: {details}. "
            f"`CREATE ... IF NOT EXISTS` keeps whatever table or index already has the name, so "
            f"this came from an earlier map or from outside SDE. A new layout needs fresh tables "
            f"(staging), not the old ones under the new declaration."
        )
