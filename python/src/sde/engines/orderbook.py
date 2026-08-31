"""Orderbook engine adapter, and the three things it will not pretend to be.

This is the first engine here whose physical schema is not ours. PostgreSQL and ClickHouse take a
schema we derive from the client's model; this one stores L2 depth in a shape fixed in C++, so the
relationship inverts - either the client's model *is* that shape or the group cannot be placed here.
``sde.ORDERBOOK_SHAPE`` is the shape and ``default_layout`` refuses anything else, naming the whole
expected shape so the refusal is actionable in one read.

Three differences from a general-purpose store are **named rather than smoothed over**, because each
one is a promise this engine does not make and a client planning around it needs to know which:

**No transactions.** ``transaction()`` refuses. One group is one engine's transaction semantics, so
a client who declared ``atomic_with`` gets this engine excluded at planning time rather than
discovering it here - but the refusal exists anyway, because a context manager that silently did
nothing would turn a declared atomicity requirement into a comment.

**No key enforcement, and no way to get it.** Two writes with the same
``(symbol, exchange, timestamp_ns, side, level)`` both persist - measured, with the same
``sequence_number`` too. That is not a gap to work around: the engine is an append-only log of depth
updates, which is what makes it fast. ClickHouse has the same absence and a way out (``FINAL`` over
``ReplacingMergeTree``); here there is none, so :meth:`get` **refuses** when a key matches more than
one row rather than picking one. Returning either would be a read that lies about uniqueness, and
the condition can only arise from a key violation this engine could not have prevented.

**Writes are updates of N levels, not rows.** ``level`` is not a parameter of the engine's write API
- it is the index of a price within one update. So a single-row insert can only ever produce ``level
= 0``, and :meth:`insert` refuses any other value rather than writing it to 0 and letting the read
disagree with the write. The engine's real granularity is available as :meth:`insert_levels`, in the
same spirit as PostgreSQL's ``range`` and ``count``: beyond the session protocol, because it is
beyond what the protocol can say.

One more property that is a cost rather than a refusal. A write is invisible to a query until
``flush()``, and ``flush()`` in local mode tears the engine down and reopens it - **3.4 ms
measured** on an i3-7100U with one row. So reads flush lazily, only when there is something
unflushed, and the cost lands on the first read after a write rather than on every write. The
alternative - not flushing and returning nothing for a row that was just written - is a silent wrong
answer, which is never the cheaper option.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping, Sequence
from typing import Any

from ..errors import EngineError
from ..layout import ORDERBOOK_KEY, ORDERBOOK_SHAPE, ORDERBOOK_TABLE
from ..logging import log
from ..placement import PhysicalLayout

__all__ = ["OrderbookEngine"]

SIDES = ("ask", "bid")
"""The two values ``side`` may take. Sorted, so the error message is stable."""

_UNKNOWN_SEQUENCE = 0
"""What the engine returns when it has no sequence number for a row.

A safe sentinel there - its own numbering starts at 1, so 0 is unreachable as a real value - and not
safe here, because a client comparing sequence numbers cannot tell a sentinel from a datum.
Converted to ``None`` on the way out, which is the same rule the rest of this library follows:
unknown is not zero, and flattening the two leads to opposite decisions.
"""


def _quote_literal(value: str) -> str:
    """A single-quoted string literal for the engine's query language.

    Not in ``sde.schema.QUOTE``, and deliberately: that maps dialects to *identifier* quoting, and
    this engine has no identifier of ours to escape. ``symbol`` and ``exchange`` arrive in the FROM
    clause as literals.

    A symbol containing a quote is refused rather than escaped. The engine's tokeniser has no escape
    sequence inside a string literal, so there is nothing to escape *to* - and a doubled quote,
    which is what one would reach for, would silently address a different symbol.
    """
    if "'" in value or "\\" in value or "\n" in value:
        raise EngineError(
            f"{value!r} cannot be used as a symbol or exchange: this engine's query language has "
            f"no escape sequence inside a string literal, so a quote or a backslash cannot be "
            f"expressed. Refused rather than escaped, because the escaping one would reach for "
            f"would address a different symbol without saying so."
        )
    return f"'{value}'"


class OrderbookEngine:
    """A thin adapter over the orderbook engine's Python client.

    Local mode takes a data directory and talks to the shared library in-process; TCP mode takes a
    host and a port. Both are the client's own deployment: this library connects, the control plane
    never does.
    """

    dialect = "orderbook"

    def __init__(
        self,
        data_dir: str | None = None,
        *,
        host: str | None = None,
        port: int | None = None,
    ) -> None:
        if (data_dir is None) == (host is None):
            raise EngineError(
                "give either a data directory, for in-process access through the shared library, "
                "or a host and port, for a running ob_tcp_server. Not both and not neither: the "
                "two are different deployments with different durability, and defaulting to one of "
                "them would pick a durability guarantee on the client's behalf."
            )
        if host is not None and port is None:
            raise EngineError("a host needs a port; this engine has no default port worth guessing")
        self._data_dir = data_dir
        self._host = host
        self._port = port
        self._engine: Any = None
        self._unflushed = 0

    # --- connection ------------------------------------------------------------------------

    def connect(self) -> None:
        if self._engine is not None:
            return
        try:
            import orderbook_engine
        except ImportError as exc:  # pragma: no cover - depends on a separate install
            raise EngineError(
                "the orderbook adapter needs the engine's own Python client, which is not on PyPI: "
                "install it from https://github.com/Smart-Data-Engines/"
                "low-cost-and-low-latency-orderbook-dbengine (its `python/` directory) and point "
                "OB_LIB_PATH at liborderbook_shared.so. It is not declared as an extra here "
                "because an extra resolving to a git URL cannot be published, and a dependency you "
                "cannot install from an index is worse than one you were told about."
            ) from exc
        try:
            if self._data_dir is not None:
                self._engine = orderbook_engine.OrderbookEngine(self._data_dir)
            else:
                self._engine = orderbook_engine.OrderbookEngine(host=self._host, port=self._port)
        except Exception as exc:
            raise EngineError(f"could not open the orderbook engine: {exc}") from exc

    def close(self) -> None:
        if self._engine is not None:
            self._engine.close()
            self._engine = None
            self._unflushed = 0

    def __enter__(self) -> OrderbookEngine:
        self.connect()
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    @property
    def _ob(self) -> Any:
        if self._engine is None:
            raise EngineError("not connected; call connect() first")
        return self._engine

    # --- schema ----------------------------------------------------------------------------

    def ensure_schema(self, layout: PhysicalLayout, *, keys: Mapping[str, Sequence[str]]) -> None:
        """Verify, because there is nothing to create.

        The storage exists the moment the engine opens its data directory. What can still be wrong
        is the map: a document built for another engine, or for a model that is not this shape,
        would route writes here and fail on the first one. So this checks the layout against the
        fixed shape and against the key, and refuses before a single row is written.

        Checked here as well as in ``default_layout`` on purpose. That function is ours and runs
        where the map is built; this one runs in the client's process against the document they
        actually hold, which is the only place a map built by an older version of us gets caught.
        """
        entities = sorted(layout.tables)
        if len(entities) != 1:
            raise EngineError(
                f"this engine stores one thing and the map gives it {entities}. A colocation group "
                f"is what shares an engine, so a group of two cannot be placed here."
            )
        entity = entities[0]
        table = layout.tables[entity]
        if table != ORDERBOOK_TABLE:
            raise EngineError(
                f"the map calls the table {table!r} and this engine's storage is "
                f"{ORDERBOOK_TABLE!r}. The name is the engine's, not ours: there is no CREATE "
                f"TABLE to send it, so a map naming something else was built for another engine."
            )

        declared = dict(layout.columns.get(entity, {}))
        expected_names = set(ORDERBOOK_SHAPE)
        missing = sorted(expected_names - set(declared))
        extra = sorted(set(declared) - expected_names)
        if missing or extra:
            raise EngineError(
                f"the map's layout for {entity} does not match this engine's fixed shape: "
                f"{f'missing {missing}' if missing else ''}"
                f"{'; ' if missing and extra else ''}"
                f"{f'unexpected {extra}' if extra else ''}. The shape is fixed in the engine and "
                f"the whole of it is {sorted(ORDERBOOK_SHAPE)}."
            )

        key = tuple(keys.get(entity, ()))
        if key != ORDERBOOK_KEY:
            raise EngineError(
                f"the map keys {entity} by {list(key)} and this engine addresses rows by "
                f"{list(ORDERBOOK_KEY)}. The order is positional and it is load-bearing: the "
                f"symbol and the exchange are how a query reaches the data at all."
            )
        log("sde.schema.applied", engine=self.dialect, statements=0)

    # --- data ------------------------------------------------------------------------------

    def insert(self, table: str, values: Mapping[str, Any]) -> None:
        """One depth level, at level 0, or a refusal.

        ``level`` is not something the engine's write API accepts - it is the index of a price
        inside one update - so a row declaring level 3 would be stored at 0 and read back at 0.
        Refused rather than written, because a write the read disagrees with is the one failure a
        storage adapter must never produce quietly. :meth:`insert_levels` writes an update of
        several levels, which is the granularity the engine actually has.
        """
        missing = sorted(set(ORDERBOOK_SHAPE) - set(values))
        if missing:
            raise EngineError(
                f"insert into {table} is missing {missing}. Every field of the fixed shape is "
                f"required: this engine has no defaults to fall back on and no nullable columns "
                f"except the sequence number."
            )
        level = int(values["level"])
        if level != 0:
            raise EngineError(
                f"insert into {table} declares level {level}, and this engine's write API has no "
                f"level parameter - a price's level is its index within one update. Writing this "
                f"would store it at level 0 and the read would disagree with the write. Use "
                f"insert_levels() to write an update of several levels, which is the granularity "
                f"this engine has."
            )
        self.insert_levels(
            table,
            symbol=str(values["symbol"]),
            exchange=str(values["exchange"]),
            side=str(values["side"]),
            timestamp_ns=int(values["timestamp_ns"]),
            levels=((int(values["price"]), int(values["quantity"]), int(values["order_count"])),),
            sequence_number=(
                None if values.get("sequence_number") is None else int(values["sequence_number"])
            ),
        )

    def insert_levels(
        self,
        table: str,
        *,
        symbol: str,
        exchange: str,
        side: str,
        timestamp_ns: int,
        levels: Sequence[tuple[int, int, int]],
        sequence_number: int | None = None,
    ) -> None:
        """One update: N levels of one side of one book at one instant, in order.

        ``levels`` is (price, quantity, order_count) from the top of the book down, and the position
        in that sequence *is* the level. Beyond the session protocol, because the protocol speaks in
        rows and this engine's unit of work is an update - the same reason PostgreSQL's adapter has
        ``range`` and ``count`` here rather than in the protocol.
        """
        if table != ORDERBOOK_TABLE:
            raise EngineError(f"this engine has one table, {ORDERBOOK_TABLE!r}, not {table!r}")
        if side not in SIDES:
            raise EngineError(f"side must be one of {list(SIDES)}, not {side!r}")
        if not levels:
            raise EngineError(
                "an update with no levels is not an empty update, it is a write that would report "
                "success without storing anything"
            )
        _quote_literal(symbol)
        _quote_literal(exchange)
        prices = [price for price, _, _ in levels]
        quantities = [quantity for _, quantity, _ in levels]
        counts = [count for _, _, count in levels]
        try:
            self._ob.insert(
                symbol,
                exchange,
                side,
                prices,
                quantities,
                counts,
                timestamp_ns=timestamp_ns,
                seq=sequence_number,
            )
        except Exception as exc:
            # Surfaced, not swallowed and not rerouted, exactly as in the PostgreSQL adapter: a
            # write that did not happen is not our internal problem.
            log("sde.write.failed", table=table, error=type(exc).__name__)
            raise EngineError(f"insert into {table} failed: {exc}") from exc
        self._unflushed += len(levels)

    def flush(self) -> None:
        """Make everything written so far queryable.

        Exposed because the cost is real and a client ingesting a feed wants to decide when to pay
        it. Reads call it themselves when there is something unflushed, so correctness does not
        depend on anybody remembering.
        """
        if self._unflushed == 0:
            return
        try:
            self._ob.flush()
        except Exception as exc:
            raise EngineError(f"flush failed: {exc}") from exc
        log("sde.orderbook.flushed", rows=self._unflushed)
        self._unflushed = 0

    def get(self, table: str, key: Mapping[str, Any]) -> dict[str, Any] | None:
        """One row by key, ``None`` if there is none, and a refusal if there are two.

        The refusal is the interesting half. This engine does not enforce the key - two writes with
        the same one both persist, measured - so "fetch the row with this key" is a question it can
        answer with more than one row. Returning either would be a read that lies about uniqueness,
        and the client cannot see that it happened. The condition arises only from a key violation
        the engine could not have prevented, so the honest response is to say so.
        """
        missing = sorted(set(ORDERBOOK_KEY) - set(key))
        if missing:
            raise EngineError(
                f"get from {table} is missing {missing} from the key. This engine addresses rows "
                f"by {list(ORDERBOOK_KEY)} and cannot scan for a partial one: the symbol and the "
                f"exchange are how a query reaches the data at all."
            )
        timestamp = int(key["timestamp_ns"])
        rows = [
            row
            for row in self.levels(
                symbol=str(key["symbol"]),
                exchange=str(key["exchange"]),
                start_ns=timestamp,
                end_ns=timestamp,
            )
            if row["side"] == key["side"] and row["level"] == int(key["level"])
        ]
        if not rows:
            return None
        if len(rows) > 1:
            raise EngineError(
                f"{len(rows)} rows in {table} share the key "
                f"{ {name: key[name] for name in ORDERBOOK_KEY} }. This engine is an append-only "
                f"log of depth updates and does not enforce a key, so this is a key violation it "
                f"could not have prevented. Refused rather than answered with one of them: picking "
                f"either would be a read that lies about uniqueness, and you would not see it."
            )
        return rows[0]

    def levels(
        self,
        *,
        symbol: str,
        exchange: str,
        start_ns: int | None = None,
        end_ns: int | None = None,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        """Every stored level for one book, optionally within a timestamp range.

        The symbol and the exchange are not optional and cannot be. The engine's query language
        takes them in the FROM clause, so there is no such thing as a scan across books here - which
        is a property of an engine built for one workload, not a limitation to route around.

        ``end_ns`` is **inclusive**, because the engine's ``BETWEEN`` is, and translating a
        half-open range into it would need an off-by-one that only shows up at the boundary.
        """
        self.flush()
        where = ""
        if start_ns is not None or end_ns is not None:
            low = 0 if start_ns is None else start_ns
            # The engine's BETWEEN takes two uint64s, so "no upper bound" has to be a number.
            # The largest uint64 rather than a large-looking constant: a timestamp past it cannot
            # exist in a field that holds it.
            high = (1 << 64) - 1 if end_ns is None else end_ns
            where = f" WHERE timestamp BETWEEN {low} AND {high}"
        cap = "" if limit is None else f" LIMIT {int(limit)}"
        query = (
            f"SELECT * FROM {_quote_literal(symbol)}.{_quote_literal(exchange)}{where}{cap}"
        )
        try:
            rows = self._ob.query(query)
        except Exception as exc:
            raise EngineError(f"query failed: {query}: {exc}") from exc
        return [
            {
                "symbol": symbol,
                "exchange": exchange,
                "timestamp_ns": int(row.timestamp_ns),
                "side": str(row.side),
                "level": int(row.level),
                "price": int(row.price),
                "quantity": int(row.quantity),
                "order_count": int(row.order_count),
                # Unknown, not zero. See _UNKNOWN_SEQUENCE.
                "sequence_number": (
                    None
                    if int(row.sequence_number) == _UNKNOWN_SEQUENCE
                    else int(row.sequence_number)
                ),
            }
            for row in rows
        ]

    # --- transactions ----------------------------------------------------------------------

    def transaction(self) -> Iterator[OrderbookEngine]:
        """Refused, out loud.

        A context manager that silently did nothing would turn a declared atomicity requirement into
        a comment. The planner excludes this engine from any group that declared ``atomic_with``, so
        reaching here means the map and the model disagree - and that is worth an exception rather
        than a shrug.

        Undecorated, like the ClickHouse one and for the same reason: a decorated generator needs a
        ``yield`` after the ``raise`` to keep the type honest, and that statement is unreachable -
        ``mypy --strict`` says so, correctly. A plain method that raises fails one frame earlier.
        """
        raise EngineError(
            "this engine has no multi-statement transactions, so there is nothing here to give "
            "you. One group is one engine's transaction semantics: if two entities must change "
            "together, declare that with atomic_with and the planner will place them somewhere "
            "that can. Refused rather than quietly doing nothing, because a transaction that is "
            "not one is worse than not having the method."
        )
