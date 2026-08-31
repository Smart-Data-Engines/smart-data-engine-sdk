"""DDL as a value, not as a side effect.

The statements that create a group's tables used to exist only inside ``ensure_schema``, built and
executed in the same breath. That made them impossible to read without a database and impossible to
show to anybody - which matters, because "here is the schema we chose for you" is one of the things
the control plane hands a client, and the only honest way to produce it is to ask the library that
would apply it. Rebuilding the DDL at the other end would be a second implementation of the type
mapping, the quoting and the key handling, and two implementations of one thing is the failure the
byte contract exists to prevent.

So this module is pure: a layout and a set of keys in, statements out, no connection anywhere. It
lives outside ``sde.engines`` deliberately. That package is the part of the library that opens
connections - the control plane's import allowlist refuses it by name for exactly that reason - and
rendering DDL needs a driver about as much as printing a receipt needs a bank.

Being a pure function also makes it the natural home for the `schema/` conformance vectors, which
section 10 of the format contract records as missing. Two libraries that agree on the map and
disagree on the DDL place the same entity in tables with different columns.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence

from .errors import EngineError
from .layout import DIALECTS, FIXED_SCHEMA
from .placement import PhysicalLayout


def _quote_ansi(identifier: str) -> str:
    return '"' + identifier.replace('"', '""') + '"'


def _quote_backtick(identifier: str) -> str:
    """Backticks, doubled to escape.

    ClickHouse accepts double quotes too. Backticks are the idiomatic form and, more usefully, they
    make a generated statement obviously ClickHouse when it turns up in a log next to a PostgreSQL
    one.
    """
    return "`" + identifier.replace("`", "``") + "`"


# Quoting is a property of the dialect rather than of DDL, and it is here because this is the only
# module that needs more than one dialect at a time. The adapters bind their own from this mapping
# instead of keeping a copy: two implementations of an escaping rule is how one of them ends up
# missing the doubling.
QUOTE: Mapping[str, Callable[[str], str]] = {
    "postgres": _quote_ansi,
    "clickhouse": _quote_backtick,
}


def _columns_and_key(
    layout: PhysicalLayout, entity: str, keys: Mapping[str, Sequence[str]]
) -> tuple[Mapping[str, str], list[str]]:
    cols = layout.columns.get(entity, {})
    if not cols:
        raise EngineError(f"the layout gives no columns for {entity!r}")
    key = list(keys.get(entity, ()))
    if not key:
        raise EngineError(f"no key for {entity!r}; a table without one cannot be addressed")
    return cols, key


def _postgres_statements(
    layout: PhysicalLayout, keys: Mapping[str, Sequence[str]]
) -> tuple[str, ...]:
    statements: list[str] = []
    for entity, table in sorted(layout.tables.items()):
        cols, key = _columns_and_key(layout, entity, keys)
        defs = ", ".join(f"{_quote_ansi(c)} {t}" for c, t in sorted(cols.items()))
        pk = ", ".join(_quote_ansi(c) for c in key)
        statements.append(
            f"CREATE TABLE IF NOT EXISTS {_quote_ansi(table)} ({defs}, PRIMARY KEY ({pk}))"
        )

    for index in layout.indexes:
        index_entity = str(index["entity"])
        index_table = layout.tables.get(index_entity)
        if index_table is None:
            continue
        index_name = str(index["name"])
        index_cols = ", ".join(_quote_ansi(str(c)) for c in index["columns"])
        statements.append(
            f"CREATE INDEX IF NOT EXISTS {_quote_ansi(index_name)} "
            f"ON {_quote_ansi(index_table)} ({index_cols})"
        )
    return tuple(statements)


def _clickhouse_statements(
    layout: PhysicalLayout, keys: Mapping[str, Sequence[str]]
) -> tuple[str, ...]:
    statements: list[str] = []
    for entity, table in sorted(layout.tables.items()):
        cols, key = _columns_and_key(layout, entity, keys)
        missing = [column for column in key if column not in cols]
        if missing:
            raise EngineError(
                f"the key of {entity!r} names columns the layout does not have: {missing}. In "
                f"ClickHouse the key becomes ORDER BY, so this would produce a table that cannot "
                f"be created rather than one with a missing constraint."
            )
        defs = ", ".join(f"{_quote_backtick(c)} {t}" for c, t in sorted(cols.items()))
        # ORDER BY is the declared key, in declared order. That order is positional and carries
        # meaning: it decides which prefixes of the key can prune granules, so sorting it would
        # change the physical performance of the table while leaving the map looking identical.
        order = ", ".join(_quote_backtick(c) for c in key)
        statements.append(
            f"CREATE TABLE IF NOT EXISTS {_quote_backtick(table)} ({defs}) "
            f"ENGINE = ReplacingMergeTree ORDER BY ({order})"
        )

    if layout.indexes:
        raise EngineError(
            f"the layout carries {len(layout.indexes)} index definitions and this engine has no "
            f"B-tree to put them in. A ClickHouse index is a data-skipping index with a type and a "
            f"granularity, so this map was built for another dialect."
        )
    return tuple(statements)


def _no_statements(layout: PhysicalLayout, keys: Mapping[str, Sequence[str]]) -> tuple[str, ...]:
    """No DDL, because this engine's schema is not ours to create.

    An empty tuple rather than a raise, and the difference matters. "Run nothing" is the *correct*
    action for a caller preparing a fixed-schema engine: the storage exists the moment the engine
    opens its data directory, and the client's obligation is that their model matches the shape -
    which ``default_layout`` has already enforced by the time a layout exists to render.

    Raising would push the branch out to every caller, which is the shape of the defect this module
    was extracted to remove. What the empty tuple loses is the *explanation*, and
    :func:`schema_is_fixed` supplies that to anybody printing one - the control plane's placement
    report does, because "here is the schema we chose for you: (nothing)" needs a sentence after it.
    """
    return ()


_BY_DIALECT = {
    "postgres": _postgres_statements,
    "clickhouse": _clickhouse_statements,
    "orderbook": _no_statements,
}

# A dialect this library types columns for and cannot render DDL for would be a map it can build and
# not apply, so the two lists have to be the same list.
assert set(_BY_DIALECT) == set(DIALECTS), sorted(set(_BY_DIALECT) ^ set(DIALECTS))

# Quoting is not the same list, and the exception is named rather than absorbed: a fixed-schema
# engine never has an identifier of ours to escape. It emits no DDL, and its query language takes
# the symbol and the exchange as string *literals* rather than identifiers. A no-op quoting function
# entered here to satisfy a symmetry would look usable and silently escape nothing the day somebody
# reached for it.
_QUOTED = set(DIALECTS) - FIXED_SCHEMA
assert set(QUOTE) == _QUOTED, sorted(set(QUOTE) ^ _QUOTED)


def schema_is_fixed(dialect: str) -> bool:
    """Whether this engine imposes its own schema, so an empty statement list means "nothing to do".

    The one public way to tell that apart from "no tables in this layout". A caller that never asks
    still behaves correctly - creating nothing is right - but a caller *reporting* what the client
    must do has a different sentence to write, and guessing which by testing the dialect string
    would put the set of fixed-schema engines in two places.
    """
    if dialect not in _BY_DIALECT:
        raise EngineError(
            f"unknown dialect {dialect!r}; this library renders {sorted(_BY_DIALECT)}. Answering "
            f"False would say 'that engine takes DDL from us' about an engine it has never heard "
            f"of."
        )
    return dialect in FIXED_SCHEMA


def schema_statements(
    layout: PhysicalLayout, *, keys: Mapping[str, Sequence[str]], dialect: str
) -> tuple[str, ...]:
    """The statements that would create this layout, in the order they must run.

    Idempotent by construction - every statement is ``IF NOT EXISTS`` - because an application
    restarting must not reapply DDL and two instances starting at once must not race. Anything
    beyond creation is a migration, which carries a rollback path and a safety classification, and a
    library that quietly altered a live column would be doing the one thing this product promises
    never to do without one.

    An unknown dialect raises. Falling back to ANSI would emit a statement that looks right, runs on
    the wrong engine, and creates a table with the wrong storage semantics.
    """
    try:
        build = _BY_DIALECT[dialect]
    except KeyError:
        raise EngineError(
            f"no DDL for dialect {dialect!r}; this library renders "
            f"{sorted(_BY_DIALECT)}. Refusing rather than falling back to ANSI: a statement that "
            f"looks right on the wrong engine creates a table with the wrong storage semantics."
        ) from None
    return build(layout, keys)
