"""Orderbook engine adapter, and the things it will not pretend to be.

This is the first engine here whose physical schema is not ours. PostgreSQL and ClickHouse take a
schema we derive from the client's model; this one stores L2 depth in a shape fixed in C++, so the
relationship inverts - either the client's model *is* that shape or the group cannot be placed here.
``sde.ORDERBOOK_SHAPE`` is the shape and ``default_layout`` refuses anything else, naming the whole
expected shape so the refusal is actionable in one read.

Several differences from a general-purpose store are **named rather than smoothed over**, because
each one is a promise this engine does not make and a client planning around it needs to know which:

**No transactions.** ``transaction()`` refuses. One group is one engine's transaction semantics, so
a client who declared ``atomic_with`` gets this engine excluded at planning time rather than
discovering it here - but the refusal exists anyway, because a context manager that silently did
nothing would turn a declared atomicity requirement into a comment.

**No key enforcement, and no way to get it.** Two writes with the same
``(symbol, exchange, timestamp_ns, side, level)`` both persist - measured. That is not a gap to work
around: the engine is an append-only log of depth updates, which is what makes it fast. ClickHouse
has the same absence and a way out (``FINAL`` over ``ReplacingMergeTree``); here there is none, so
:meth:`get` and :meth:`select_rows` **refuse** when a key matches more than one row rather than
picking one. Returning either would be a read that lies about uniqueness, and the condition can only
arise from a key violation this engine could not have prevented.

**Writes are updates of N levels, not rows.** ``level`` is not a parameter of the engine's write API
- it is the index of a price within one update. So a single-row insert can only ever produce ``level
= 0``, and :meth:`insert` refuses any other value rather than writing it to 0 and letting the read
disagree with the write. ``Session.save_many`` writes whole updates: the rows of one side of one
book at one instant are one update, levels 0 to n-1 (:meth:`insert_many`).

**Over TCP the sequence number is the server's.** The server numbers every update per book, and a
number the client chose is refused before it is sent. Measured on 2 October 2026 against the
engine at ``971dda2``: the server itself refuses one ("seq cannot be chosen over the wire"), and
before that release it accepted and discarded it, so the row read back carried a different number
from the one written. Refusing here makes the answer the same against every server version. Write
``sequence_number`` as ``None`` and read the server's number back; in local mode you choose it.

**The engine answers in arrival order, not in time order.** Measured the same day: three updates
written with event times 3000, 1000 and 2000 come back in that order, and a ``LIMIT`` keeps the
first rows to arrive. So the engine's ``LIMIT`` cannot be a page in key order, and
:meth:`select_rows` reads bounded windows of time, sorts each one and assembles the page itself.

**It counts nothing over its history.** The engine's aggregates read the live book, not the stored
rows (its query language refuses a time filter on an aggregate), so ``Session.count`` and
``Session.summarize`` are refused here by name rather than computed from a full scan.

One more property that is a cost rather than a refusal. A write is invisible to a query until
``flush()``, and ``flush()`` in local mode tears the engine down and reopens it - **3.4 ms
measured** on an i3-7100U with one row. So reads flush lazily, only when there is something
unflushed, and the cost lands on the first read after a write rather than on every write. The
alternative - not flushing and returning nothing for a row that was just written - is a silent wrong
answer, which is never the cheaper option.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping, Sequence
from dataclasses import dataclass
from itertools import pairwise
from typing import Any
from urllib.parse import parse_qsl, unquote, urlsplit

from ..errors import EngineError
from ..explain import QueryPlan
from ..layout import ORDERBOOK_KEY, ORDERBOOK_SHAPE, ORDERBOOK_TABLE
from ..logging import log
from ..placement import PhysicalLayout
from ..query import QueryRefused, ReadPlan

__all__ = ["OrderbookEngine"]

SIDES = ("ask", "bid")
"""The two values ``side`` may take. Sorted, so the error message is stable - and sorted the way
``str`` compares them, which is the order a scan in key order returns them in."""

_UNKNOWN_SEQUENCE = 0
"""What the engine returns when it has no sequence number for a row.

A safe sentinel there - its own numbering starts at 1, so 0 is unreachable as a real value - and not
safe here, because a client comparing sequence numbers cannot tell a sentinel from a datum.
Converted to ``None`` on the way out, which is the same rule the rest of this library follows:
unknown is not zero, and flattening the two leads to opposite decisions.
"""

MAX_LEVEL = 999
"""The deepest level an update may have. The engine stores at most 1000 levels per side
(``MAX_LEVELS`` in its ``data_model.hpp``) and an update of more is refused by its parser."""

MAX_TIMESTAMP_NS = (1 << 63) - 1
"""The model's ``int64``. The engine stores ``uint64`` nanoseconds, so a negative time - a moment
before 1970 - cannot be stored at all, and a value above this one cannot be declared."""

MAX_QUANTITY = (1 << 63) - 1
"""The model's ``int64`` again, against the engine's ``uint64``: a quantity is not negative."""

MAX_ORDER_COUNT = (1 << 31) - 1
"""The model's ``int32``, against the engine's ``uint32``."""

MIN_PRICE, MAX_PRICE = -(1 << 63), (1 << 63) - 1
"""``int64`` in both. Negative prices are real - an oil future settled below zero in 2020 - and the
engine stores them."""

BATCH_UPDATES = 64
"""Updates per round trip in :meth:`insert_many`. The engine session measured the pipelined batch
on an m9g.xlarge and its rate stopped improving after 64 updates per call."""

SCAN_CHUNK_ROWS = 100_000
"""The most rows one query of :meth:`select_rows` may bring into this process. A window of time that
holds more is split; one instant that holds more is refused."""

SCAN_FIRST_WINDOW_NS = 1_000_000_000
"""The first window of time :meth:`select_rows` reads: one second. It doubles while the windows are
sparse and halves when one holds more than :data:`SCAN_CHUNK_ROWS`, so the width follows the data
rather than a guess about it."""

_LAST_INSTANT = (1 << 64) - 1
"""The engine's ``BETWEEN`` takes two uint64s, so "no upper bound" has to be a number. The largest
uint64 rather than a large-looking constant: a timestamp past it cannot exist in a field that holds
it."""

_FORBIDDEN_IN_NAMES = ("'", "\\")


def _name(value: Any, what: str) -> str:
    """A symbol or an exchange, refused when the engine could not store or address it faithfully.

    Two separate reasons. The query language has no escape sequence inside a string literal, so a
    quote or a backslash cannot be expressed - and the doubled quote one would reach for would
    silently address a different symbol. And the wire protocol splits a command on whitespace, so a
    symbol with a space in it shifts every later field: measured, ``INSERT BTC USD ...`` is refused
    by the server as "unexpected token", and a value crafted to line up would be a write to another
    book. Refused here, with the field named, rather than as whatever the server makes of it.
    """
    if not isinstance(value, str) or not value:
        raise EngineError(f"{what} must be a non-empty string, not {value!r}")
    if any(character in value for character in _FORBIDDEN_IN_NAMES) or any(
        character.isspace() or not character.isprintable() for character in value
    ):
        raise EngineError(
            f"{value!r} cannot be used as a {what}: this engine's query language has no escape "
            f"sequence inside a string literal and its wire protocol separates fields by "
            f"whitespace, so a quote, a backslash, a space or a control character cannot be "
            f"stored or addressed faithfully. Refused rather than escaped, because the escaping "
            f"one would reach for would address a different book without saying so."
        )
    return value


def _quote_literal(value: str) -> str:
    """A single-quoted string literal for the engine's query language.

    Not in ``sde.schema.QUOTE``, and deliberately: that maps dialects to *identifier* quoting, and
    this engine has no identifier of ours to escape. ``symbol`` and ``exchange`` arrive in the FROM
    clause as literals, checked by :func:`_name` first.
    """
    return f"'{_name(value, 'symbol or exchange')}'"


def _integer(value: Any, field: str, low: int, high: int) -> int:
    """An integer in ``[low, high]``, or a refusal naming the field and both bounds."""
    if isinstance(value, bool) or not isinstance(value, int):
        raise EngineError(f"{field} must be an integer, not {type(value).__name__}")
    if not low <= value <= high:
        raise EngineError(
            f"{field} {value} is outside what this engine stores ({low} to {high}). Refused before "
            f"sending: the server would refuse it with a message about tokens, or store something "
            f"else."
        )
    return value


def _not_found(exc: BaseException) -> bool:
    """Whether the server said "no such book" - which, for a book nobody has written to yet, is not
    an error but an empty book. The client carries the server's code in the message (measured:
    ``query error: OB_ERR_NOT_FOUND: symbol 'NOPE' exchange 'X' not found``).

    Over TCP only. In-process the C API's ``ob_query`` returns no result for *every* failure and
    keeps no reason, and the client raises the same ``OB_ERR_PARSE`` "Query failed" for an unknown
    book as for a broken segment (measured, engine ``971dda2``). Reading either as an empty book
    would turn a failure into a silent wrong answer, so in local mode a failed read stays a failure
    and says why it cannot be anything else."""
    return "OB_ERR_NOT_FOUND" in str(exc)


def _key(row: Mapping[str, Any]) -> tuple[int, str, int]:
    """A row's position within one book: time, then side, then level - its key without the book."""
    return (int(row["timestamp_ns"]), str(row["side"]), int(row["level"]))


@dataclass(frozen=True)
class _Update:
    symbol: str
    exchange: str
    side: str
    timestamp_ns: int
    levels: tuple[tuple[int, int, int], ...]
    sequence_number: int | None
    rows: tuple[int, ...]


@dataclass(frozen=True)
class _Scan:
    """One logical read, translated into what this engine can be asked."""

    symbol: str
    exchange: str
    side: str | None
    level: int | None
    price_low: int | None
    price_high: int | None
    low: int
    high: int
    descending: bool
    after: tuple[int, str, int] | None

    def keeps(self, row: Mapping[str, Any]) -> bool:
        if self.side is not None and row["side"] != self.side:
            return False
        if self.level is not None and row["level"] != self.level:
            return False
        if self.after is not None:
            position = _key(row)
            return position < self.after if self.descending else position > self.after
        return True


_EQUAL_FIELDS = frozenset({"symbol", "exchange", "side", "level", "price"})
_RANGE_FIELDS = frozenset({"timestamp_ns", "price"})
_ORDER = ("timestamp_ns", "side", "level")


def _scan_of(plan: ReadPlan) -> _Scan | None:
    """The read, or ``None`` when it provably matches nothing. Every refusal is before any I/O."""
    equal: dict[str, Any] = {}
    bounds: dict[str, dict[str, Any]] = {}
    for item in plan.filters:
        name = item.column.name
        if item.operation == "eq":
            equal[name] = item.value
        elif item.operation in ("ge", "lt"):
            bounds.setdefault(name, {})[item.operation] = item.value
        else:  # pragma: no cover - plan_read builds only these three
            raise QueryRefused(f"this engine has no {item.operation!r} comparison")
    unsupported = sorted(set(equal) - _EQUAL_FIELDS)
    if unsupported:
        raise QueryRefused(
            f"this engine cannot filter on {unsupported}: its query language selects one book by "
            f"symbol and exchange and narrows it by time and price. Reading a book's history to "
            f"filter it here would be a full scan dressed as a query."
        )
    ranged = sorted(set(bounds) - _RANGE_FIELDS)
    if ranged:
        raise QueryRefused(
            f"this engine has no range over {ranged}; it bounds a read by timestamp_ns or price"
        )
    if "symbol" not in equal or "exchange" not in equal:
        raise QueryRefused(
            "a read here names one book: where= must fix both symbol and exchange. The engine's "
            "query language takes them in its FROM clause, so there is no scan across books - a "
            "property of an engine built for one workload, not a limitation to route around."
        )
    fixed = set(equal)
    remaining = [column.name for column in plan.order if column.name not in fixed]
    expected = [name for name in _ORDER if name not in fixed]
    if remaining != expected:
        raise QueryRefused(
            f"this engine answers one book in time order (then side, then level), and this read "
            f"asks for {remaining}. Ordering a book's history by anything else would mean reading "
            f"all of it first."
        )
    low, high = 0, _LAST_INSTANT
    times = bounds.get("timestamp_ns", {})
    if "ge" in times:
        low = max(low, int(times["ge"]))
    if "lt" in times:
        high = min(high, int(times["lt"]) - 1)
    price_low: int | None = None
    price_high: int | None = None
    prices = bounds.get("price", {})
    if "ge" in prices:
        price_low = int(prices["ge"])
    if "lt" in prices:
        price_high = int(prices["lt"]) - 1
    if "price" in equal:
        value = int(equal["price"])
        price_low = value if price_low is None else max(price_low, value)
        price_high = value if price_high is None else min(price_high, value)
    side = None if "side" not in equal else str(equal["side"])
    level = None if "level" not in equal else int(equal["level"])
    if side is not None and side not in SIDES:
        return None
    after = None
    if plan.after is not None:
        position = {
            column.name: value for column, value in zip(plan.order, plan.after, strict=True)
        }
        after = (
            int(position["timestamp_ns"]),
            str(side if side is not None else position["side"]),
            int(level if level is not None else position["level"]),
        )
        if plan.descending:
            high = min(high, after[0])
        else:
            low = max(low, after[0])
    if low > high or (price_low is not None and price_high is not None and price_low > price_high):
        return None
    return _Scan(
        symbol=_name(equal["symbol"], "symbol"),
        exchange=_name(equal["exchange"], "exchange"),
        side=side,
        level=level,
        price_low=price_low,
        price_high=price_high,
        low=low,
        high=high,
        descending=plan.descending,
        after=after,
    )


_DSN_PARAMETERS = frozenset({"tls", "ca", "verify", "timeout"})
_SWITCH = {"on": True, "off": False}


class OrderbookEngine:
    """A thin adapter over the orderbook engine's Python client.

    Local mode takes a data directory and talks to the shared library in-process; TCP mode takes a
    host and a port, and optionally the server's client credentials and TLS. Both are the client's
    own deployment: this library connects, the control plane never does.
    """

    dialect = "orderbook"

    count_refusal = (
        "this engine counts nothing over its history: its aggregates read the live book, not the "
        "stored rows, and its query language refuses a time filter on an aggregate. Refused rather "
        "than computed from a full scan, which is what a count here would have to be."
    )
    """Why ``Session.count`` is refused for a group on this engine (``sde.query.count_engine``)."""

    summary_refusal = (
        "this engine summarizes nothing over its history: its aggregates read the live book, not "
        "the stored rows. Refused rather than computed from a full scan."
    )
    """Why ``Session.summarize`` is refused for a group on this engine."""

    def __init__(
        self,
        data_dir: str | None = None,
        *,
        host: str | None = None,
        port: int | None = None,
        auth: tuple[str, str] | None = None,
        tls: bool = False,
        tls_ca_file: str | None = None,
        tls_verify: bool = True,
        timeout: float = 10.0,
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
        if data_dir is not None and (
            auth is not None or tls or tls_ca_file is not None or tls_verify is not True
        ):
            raise EngineError(
                "credentials and TLS belong to a connection, and local mode opens none: the shared "
                "library runs in this process. Refused rather than ignored, because a setting that "
                "does nothing reads as protection."
            )
        if auth is not None and (
            not isinstance(auth, tuple)
            or len(auth) != 2
            or not all(isinstance(part, str) and part for part in auth)
            or any(character.isspace() for character in auth[0])
        ):
            raise EngineError(
                "auth is (identity, secret): two non-empty strings, the identity without "
                "whitespace, as in the server's --auth-secret-file"
            )
        if tls_ca_file is not None and not tls:
            raise EngineError("tls_ca_file without tls=True verifies nothing: the connection would "
                              "be plain text")
        if tls_verify is not True and not tls:
            raise EngineError("tls_verify=False without tls=True: there is no certificate to "
                              "decline to check")
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not timeout > 0:
            raise EngineError("timeout is a positive number of seconds")
        self._data_dir = data_dir
        self._host = host
        self._port = port
        self._auth = auth
        self._tls = bool(tls)
        self._tls_ca_file = tls_ca_file
        self._tls_verify = tls_verify
        self._timeout = float(timeout)
        self._engine: Any = None
        self._module: Any = None
        self._unflushed = 0

    @classmethod
    def from_dsn(cls, dsn: str) -> OrderbookEngine:
        """A TCP connection from ``orderbook://[identity[:secret]@]host:port[?tls=on&ca=PATH&verify=off&timeout=S]``.

        The form a deployment keeps in an environment variable, like the PostgreSQL and ClickHouse
        DSNs beside it. The identity and the secret are percent-decoded; ``verify=off`` is accepted
        only together with ``tls=on`` and only when written out. Every refusal names the part of the
        DSN that is wrong and never repeats the DSN, which carries the secret.
        """
        if not isinstance(dsn, str):
            raise EngineError("an orderbook DSN is a string")
        try:
            parts = urlsplit(dsn)
            port = parts.port
        except ValueError:
            raise EngineError("the orderbook DSN's port is not a number") from None
        if parts.scheme != "orderbook":
            raise EngineError("an orderbook DSN starts with orderbook://")
        if not parts.hostname or port is None:
            raise EngineError("an orderbook DSN names a host and a port: orderbook://host:port")
        if parts.path not in ("", "/") or parts.fragment:
            raise EngineError("an orderbook DSN has no path or fragment; local mode takes data_dir")
        identity = None if parts.username is None else unquote(parts.username)
        secret = None if parts.password is None else unquote(parts.password)
        if (identity is None) != (secret is None) or identity == "":
            raise EngineError("an orderbook DSN's identity needs its secret: identity:secret@host")
        options: dict[str, str] = {}
        for key, value in parse_qsl(parts.query, keep_blank_values=True):
            if key not in _DSN_PARAMETERS:
                raise EngineError(
                    f"the orderbook DSN parameter {key!r} is unknown; it takes "
                    f"{sorted(_DSN_PARAMETERS)}"
                )
            if key in options:
                raise EngineError(f"the orderbook DSN gives {key!r} twice")
            options[key] = value
        for key in ("tls", "verify"):
            if key in options and options[key] not in _SWITCH:
                raise EngineError(f"the orderbook DSN parameter {key!r} is on or off")
        try:
            timeout = float(options.get("timeout", "10"))
        except ValueError:
            raise EngineError(
                "the orderbook DSN parameter 'timeout' is a number of seconds"
            ) from None
        return cls(
            host=parts.hostname,
            port=port,
            auth=None if identity is None or secret is None else (identity, secret),
            tls=_SWITCH[options.get("tls", "off")],
            tls_ca_file=options.get("ca"),
            tls_verify=_SWITCH[options.get("verify", "on")],
            timeout=timeout,
        )

    @property
    def mode(self) -> str:
        """``local`` (the shared library, in this process) or ``tcp`` (an ``ob_tcp_server``)."""
        return "local" if self._data_dir is not None else "tcp"

    def __repr__(self) -> str:
        if self._data_dir is not None:
            return f"OrderbookEngine(local {self._data_dir!r})"
        identity = "" if self._auth is None else f", identity={self._auth[0]!r}"
        return (
            f"OrderbookEngine(tcp {self._host}:{self._port}{identity}, "
            f"tls={'on' if self._tls else 'off'})"
        )

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
                "low-cost-and-low-latency-orderbook-dbengine (its `python/` directory) and, for "
                "local mode, point OB_LIB_PATH at liborderbook_shared.so. It is not declared as an "
                "extra here because an extra resolving to a git URL cannot be published, and a "
                "dependency you cannot install from an index is worse than one you were told about."
            ) from exc
        try:
            if self._data_dir is not None:
                engine = orderbook_engine.OrderbookEngine(self._data_dir)
            else:
                engine = orderbook_engine.OrderbookEngine(
                    host=self._host,
                    port=self._port,
                    timeout=self._timeout,
                    auth=self._auth,
                    tls=self._tls,
                    tls_ca_file=self._tls_ca_file,
                    tls_verify=self._tls_verify,
                )
        except Exception as exc:
            raise EngineError(f"could not open the orderbook engine: {exc}") from exc
        if self._data_dir is None:
            try:
                capabilities = set(engine.server_capabilities())
            except Exception as exc:
                engine.close()
                raise EngineError(
                    f"could not read the orderbook server's capabilities: {exc}"
                ) from exc
            if "insert_event_time" not in capabilities:
                engine.close()
                raise EngineError(
                    "this orderbook server cannot store a write's event time, so every update "
                    "would be stamped with its arrival instead of the time it happened and could "
                    "not be found by the time it carries. Upgrade the server; nothing was written."
                )
        self._engine = engine
        self._module = orderbook_engine

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

    def _lose_connection(self) -> None:
        """Close after a write whose outcome is unknown, so nothing is sent on a connection whose
        remaining replies would be read as the answers to the next command."""
        engine, self._engine = self._engine, None
        self._unflushed = 0
        if engine is not None:
            try:
                engine.close()
            except Exception:  # pragma: no cover - closing a broken connection
                log("sde.orderbook.close_failed")

    # --- schema ----------------------------------------------------------------------------

    def _check_layout(self, layout: PhysicalLayout, keys: Mapping[str, Sequence[str]]) -> None:
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

    def ensure_schema(
        self, layout: PhysicalLayout, *, keys: Mapping[str, Sequence[str]]
    ) -> tuple[Any, ...]:
        """Verify, because there is nothing to create.

        The storage exists the moment the engine opens its data directory. What can still be wrong
        is the map: a document built for another engine, or for a model that is not this shape,
        would route writes here and fail on the first one. So this checks the layout against the
        fixed shape and against the key, and refuses before a single row is written.

        Checked here as well as in ``default_layout`` on purpose. That function is ours and runs
        where the map is built; this one runs in the client's process against the document they
        actually hold, which is the only place a map built by an older version of us gets caught.
        """
        self._check_layout(layout, keys)
        log("sde.schema.applied", engine=self.dialect, statements=0)
        return ()

    def validate_schema(
        self, layout: PhysicalLayout, *, keys: Mapping[str, Sequence[str]]
    ) -> tuple[Any, ...]:
        """The same check, for a session opening on a generation-bearing map, with no findings.

        A finding reports a physical design that differs from the declared one - a key order, a
        partition, an index. This engine has no physical design to differ, so there is nothing to
        report; what can be wrong is the shape, and that refuses.
        """
        self._check_layout(layout, keys)
        return ()

    # --- data ------------------------------------------------------------------------------

    def explain_plan(self, sql: str) -> QueryPlan:
        """Refuses. There is no query planner here, and that is a property of the engine.

        Requirement 19.4 asks for an execution plan and a cost estimate from a live engine. This
        engine has neither to give, and the reason is the same one that makes it worth having: it
        was built for one access path over one fixed shape, so there is nothing for a planner to
        choose between and no alternative whose cost would need estimating. A plan saying "read the
        book for this symbol" would be true, uninformative, and one more thing in this adapter that
        pretends a difference away.

        Named rather than returned empty. An empty plan reads as "nothing to worry about", which
        is a stronger claim than this adapter can make about a query it cannot see.
        """
        raise EngineError(
            "the orderbook engine has no query planner, so there is no plan and no cost estimate "
            "to give you. That is what it is for: one fixed shape and one access path, so nothing "
            "is chosen at query time and nothing needs estimating. Refused rather than answered "
            "with an empty plan, because an empty plan reads as 'nothing to worry about'."
        )

    def _table(self, table: str) -> None:
        if table != ORDERBOOK_TABLE:
            raise EngineError(f"this engine has one table, {ORDERBOOK_TABLE!r}, not {table!r}")

    def _sequence(self, value: Any) -> int | None:
        if value is None:
            return None
        if self._data_dir is None:
            raise EngineError(
                "over TCP the sequence number is the server's: it numbers every update per book, "
                "and a number chosen here would be refused by a current server and silently "
                "replaced by an older one, so the row read back would disagree with the row "
                "written. Write sequence_number as None and read the server's number back; choose "
                "it only in local mode."
            )
        return _integer(value, "sequence_number", 1, (1 << 63) - 1)

    def _levels(
        self, levels: Sequence[tuple[int, int, int]], where: str
    ) -> tuple[tuple[int, int, int], ...]:
        if not levels:
            raise EngineError(
                "an update with no levels is not an empty update, it is a write that would report "
                "success without storing anything"
            )
        if len(levels) > MAX_LEVEL + 1:
            raise EngineError(
                f"{where}has {len(levels)} levels and this engine stores at most {MAX_LEVEL + 1} "
                f"per side"
            )
        return tuple(
            (
                _integer(price, f"{where}price", MIN_PRICE, MAX_PRICE),
                _integer(quantity, f"{where}quantity", 0, MAX_QUANTITY),
                _integer(count, f"{where}order_count", 0, MAX_ORDER_COUNT),
            )
            for price, quantity, count in levels
        )

    def insert(self, table: str, values: Mapping[str, Any]) -> None:
        """One depth level, at level 0, or a refusal.

        ``level`` is not something the engine's write API accepts - it is the index of a price
        inside one update - so a row declaring level 3 would be stored at 0 and read back at 0.
        Refused rather than written, because a write the read disagrees with is the one failure a
        storage adapter must never produce quietly. ``Session.save_many`` writes whole updates of
        several levels (:meth:`insert_many`), which is the granularity this engine has.
        """
        self._table(table)
        missing = sorted(set(ORDERBOOK_SHAPE) - set(values) - {"sequence_number"})
        if missing:
            raise EngineError(
                f"insert into {table} is missing {missing}. Every field of the fixed shape is "
                f"required: this engine has no defaults to fall back on and no nullable columns "
                f"except the sequence number."
            )
        extra = sorted(set(values) - set(ORDERBOOK_SHAPE))
        if extra:
            raise EngineError(
                f"insert into {table} carries {extra}, which this engine has nowhere to store: its "
                f"shape is fixed. A write generation in particular is never stamped here: a group "
                f"on this engine carries no write generation."
            )
        level = _integer(values["level"], "level", 0, MAX_LEVEL)
        if level != 0:
            raise EngineError(
                f"insert into {table} declares level {level}, and this engine's write API has no "
                f"level parameter - a price's level is its index within one update. Writing this "
                f"would store it at level 0 and the read would disagree with the write. Write the "
                f"whole update with Session.save_many (levels 0 to n-1 of one side of one book at "
                f"one instant), which is the granularity this engine has."
            )
        self.insert_levels(
            table,
            symbol=values["symbol"],
            exchange=values["exchange"],
            side=values["side"],
            timestamp_ns=values["timestamp_ns"],
            levels=((values["price"], values["quantity"], values["order_count"]),),
            sequence_number=values.get("sequence_number"),
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
        rows and this engine's unit of work is an update - ``Session.save_many`` reaches it through
        :meth:`insert_many`.
        """
        self._table(table)
        if side not in SIDES:
            raise EngineError(f"side must be one of {list(SIDES)}, not {side!r}")
        symbol = _name(symbol, "symbol")
        exchange = _name(exchange, "exchange")
        stamp = _integer(timestamp_ns, "timestamp_ns", 0, MAX_TIMESTAMP_NS)
        checked = self._levels(levels, "")
        sequence = self._sequence(sequence_number)
        try:
            self._ob.insert(
                symbol,
                exchange,
                side,
                [price for price, _, _ in checked],
                [quantity for _, quantity, _ in checked],
                [count for _, _, count in checked],
                timestamp_ns=stamp,
                seq=sequence,
            )
        except Exception as exc:
            # Surfaced, not swallowed and not rerouted, exactly as in the PostgreSQL adapter: a
            # write that did not happen is not our internal problem.
            log("sde.write.failed", table=table, error=type(exc).__name__)
            if self._data_dir is None:
                raise EngineError(
                    f"the engine did not confirm the write to {table}: {exc}. If the connection "
                    f"dropped after the update was sent, it may still have been stored: read the "
                    f"book before writing it again."
                ) from exc
            raise EngineError(f"insert into {table} failed: {exc}") from exc
        self._unflushed += len(checked)

    def _updates(self, rows: Sequence[Mapping[str, Any]]) -> list[_Update]:
        """Group rows into the engine's updates, refusing every malformed batch before sending.

        One side of one book at one instant is one update, and its rows must be levels 0 to n-1,
        each once: a gap would be stored as a shorter update, and a repeat would be two prices at
        one level of one update, which the engine has no way to say.
        """
        grouped: dict[tuple[str, str, str, int], dict[str, Any]] = {}
        shape = set(ORDERBOOK_SHAPE)
        for index, row in enumerate(rows):
            where = f"row {index}: "
            missing = sorted(shape - set(row) - {"sequence_number"})
            if missing:
                raise EngineError(f"{where}missing {missing}; every field of the shape is required")
            extra = sorted(set(row) - shape)
            if extra:
                raise EngineError(
                    f"{where}carries {extra}, which this engine has nowhere to store; a group "
                    f"on it carries no write generation"
                )
            side = row["side"]
            if side not in SIDES:
                raise EngineError(f"{where}side must be one of {list(SIDES)}, not {side!r}")
            book = (
                _name(row["symbol"], "symbol"),
                _name(row["exchange"], "exchange"),
                side,
                _integer(row["timestamp_ns"], f"{where}timestamp_ns", 0, MAX_TIMESTAMP_NS),
            )
            level = _integer(row["level"], f"{where}level", 0, MAX_LEVEL)
            value = self._levels(
                ((row["price"], row["quantity"], row["order_count"]),), where
            )[0]
            sequence = self._sequence(row.get("sequence_number"))
            entry = grouped.setdefault(book, {"levels": {}, "rows": {}, "sequence": sequence})
            if level in entry["levels"]:
                raise EngineError(
                    f"rows {entry['rows'][level]} and {index} are both level {level} of one update "
                    f"({book[0]} {book[1]} {side} at {book[3]}). An update holds one price per "
                    f"level, so the second would have nowhere to go."
                )
            if sequence != entry["sequence"]:
                raise EngineError(
                    f"{where}one update carries one sequence number, and this row gives "
                    f"{sequence} where the rows before it in the same update gave "
                    f"{entry['sequence']}"
                )
            entry["levels"][level] = value
            entry["rows"][level] = index
        updates = []
        for (symbol, exchange, side, stamp), entry in grouped.items():
            depth = len(entry["levels"])
            if sorted(entry["levels"]) != list(range(depth)):
                raise EngineError(
                    f"the update for {symbol} {exchange} {side} at {stamp} has levels "
                    f"{sorted(entry['levels'])}. An update is levels 0 to n-1 without a gap: the "
                    f"engine numbers levels by position, so a missing one would renumber every "
                    f"level after it."
                )
            updates.append(
                _Update(
                    symbol=symbol,
                    exchange=exchange,
                    side=side,
                    timestamp_ns=stamp,
                    levels=tuple(entry["levels"][level] for level in range(depth)),
                    sequence_number=entry["sequence"],
                    rows=tuple(entry["rows"][level] for level in range(depth)),
                )
            )
        return updates

    def insert_many(self, table: str, rows: Sequence[Mapping[str, Any]]) -> None:
        """A batch of rows, written as the engine's updates.

        Every refusal that can be decided before sending is decided for the whole batch. Over TCP
        the updates go in pipelined round trips of :data:`BATCH_UPDATES`.

        **This is not a transaction**, and the engine's batch says so too: some updates may land
        and others be refused. A refusal by the server raises with the updates it refused and the
        number that were stored. A failed connection raises with the outcome named as unknown -
        the rest may have landed in full, in part or not at all - and closes this adapter, so a
        reply still on its way is not read as the answer to the next write.
        """
        self._table(table)
        updates = self._updates(rows)
        stored = 0
        if self._data_dir is not None:
            for number, update in enumerate(updates):
                try:
                    self._ob.insert(
                        update.symbol,
                        update.exchange,
                        update.side,
                        [price for price, _, _ in update.levels],
                        [quantity for _, quantity, _ in update.levels],
                        [count for _, _, count in update.levels],
                        timestamp_ns=update.timestamp_ns,
                        seq=update.sequence_number,
                    )
                except Exception as exc:
                    log("sde.write.failed", table=table, error=type(exc).__name__)
                    raise EngineError(
                        f"update {number} of {len(updates)} (rows {list(update.rows)}) was "
                        f"refused: {exc}. The {number} updates before it were stored: a batch "
                        f"here is not a transaction."
                    ) from exc
                stored += 1
                self._unflushed += len(update.levels)
            return
        book_update = self._module.BookUpdate
        for start in range(0, len(updates), BATCH_UPDATES):
            part = updates[start : start + BATCH_UPDATES]
            try:
                outcomes = self._ob.insert_batch(
                    [
                        book_update(
                            update.symbol,
                            update.exchange,
                            update.side,
                            [price for price, _, _ in update.levels],
                            [quantity for _, quantity, _ in update.levels],
                            [count for _, _, count in update.levels],
                            timestamp_ns=update.timestamp_ns,
                        )
                        for update in part
                    ]
                )
            except Exception as exc:
                log("sde.write.failed", table=table, error=type(exc).__name__)
                self._lose_connection()
                raise EngineError(
                    f"the outcome of this batch is unknown: {stored} of its {len(updates)} "
                    f"updates were confirmed, and the connection failed on the next {len(part)} "
                    f"({type(exc).__name__}). Those may have been stored in full, in part or not "
                    f"at all; read the book before writing them again, then connect() again."
                ) from exc
            refused = [outcome for outcome in outcomes if not outcome.ok]
            accepted = len(outcomes) - len(refused)
            stored += accepted
            self._unflushed += sum(
                len(part[outcome.index].levels) for outcome in outcomes if outcome.ok
            )
            if refused:
                first = refused[0]
                log("sde.write.failed", table=table, error="refused")
                raise EngineError(
                    f"the server refused {len(refused)} of {len(updates)} updates in this batch; "
                    f"the first was update {start + first.index} (rows "
                    f"{list(part[first.index].rows)}): {first.message}. {stored} updates were "
                    f"stored - a batch here is not a transaction - and any after this part of the "
                    f"batch were not sent."
                )

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

    def _query(self, query: str) -> list[Any]:
        try:
            rows: list[Any] = self._ob.query(query)
        except Exception as exc:
            if self._data_dir is None and _not_found(exc):
                return []
            if self._data_dir is not None:
                raise EngineError(
                    f"query failed: {query}: {exc}. In local mode the engine reports every "
                    f"failed read the same way, so a book nothing has been written to cannot be "
                    f"told apart from a failure, and this is not answered as an empty book. Over "
                    f"TCP the server says which it is."
                ) from exc
            raise EngineError(f"query failed: {query}: {exc}") from exc
        return rows

    @staticmethod
    def _row(symbol: str, exchange: str, row: Any) -> dict[str, Any]:
        return {
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

    def get(self, table: str, key: Mapping[str, Any]) -> dict[str, Any] | None:
        """One row by key, ``None`` if there is none, and a refusal if there are two.

        The refusal is the interesting half. This engine does not enforce the key - two writes with
        the same one both persist, measured - so "fetch the row with this key" is a question it can
        answer with more than one row. Returning either would be a read that lies about uniqueness,
        and the client cannot see that it happened. The condition arises only from a key violation
        the engine could not have prevented, so the honest response is to say so.
        """
        self._table(table)
        missing = sorted(set(ORDERBOOK_KEY) - set(key))
        if missing:
            raise EngineError(
                f"get from {table} is missing {missing} from the key. This engine addresses rows "
                f"by {list(ORDERBOOK_KEY)} and cannot scan for a partial one: the symbol and the "
                f"exchange are how a query reaches the data at all."
            )
        timestamp = int(key["timestamp_ns"])
        if not 0 <= timestamp <= MAX_TIMESTAMP_NS or key["side"] not in SIDES:
            return None
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
            raise EngineError(self._duplicate(table, key, len(rows)))
        return rows[0]

    @staticmethod
    def _duplicate(table: str, key: Mapping[str, Any], count: int) -> str:
        return (
            f"{count} rows in {table} share the key "
            f"{ {name: key[name] for name in ORDERBOOK_KEY} }. This engine is an append-only "
            f"log of depth updates and does not enforce a key, so this is a key violation it "
            f"could not have prevented. Refused rather than answered with one of them: picking "
            f"either would be a read that lies about uniqueness, and you would not see it."
        )

    def levels(
        self,
        *,
        symbol: str,
        exchange: str,
        start_ns: int | None = None,
        end_ns: int | None = None,
        limit: int | None = None,
    ) -> list[dict[str, Any]]:
        """Every stored level for one book, optionally within a timestamp range, in arrival order.

        The symbol and the exchange are not optional and cannot be. The engine's query language
        takes them in the FROM clause, so there is no such thing as a scan across books here - which
        is a property of an engine built for one workload, not a limitation to route around.

        ``end_ns`` is **inclusive**, because the engine's ``BETWEEN`` is, and translating a
        half-open range into it would need an off-by-one that only shows up at the boundary. The
        order is the engine's, which is the order the updates arrived in - not their event time;
        ``Session.scan`` answers in key order. A book no update has reached is empty, not an error.
        Without ``limit`` a read of more than :data:`SCAN_CHUNK_ROWS` rows is refused rather than
        brought into memory whole.
        """
        self.flush()
        where = ""
        if start_ns is not None or end_ns is not None:
            low = 0 if start_ns is None else start_ns
            high = _LAST_INSTANT if end_ns is None else end_ns
            where = f" WHERE timestamp BETWEEN {low} AND {high}"
        cap = SCAN_CHUNK_ROWS + 1 if limit is None else int(limit)
        query = (
            f"SELECT * FROM {_quote_literal(symbol)}.{_quote_literal(exchange)}{where} LIMIT {cap}"
        )
        rows = self._query(query)
        if limit is None and len(rows) > SCAN_CHUNK_ROWS:
            raise EngineError(
                f"this read of {symbol} {exchange} holds more than {SCAN_CHUNK_ROWS} rows; bound "
                f"it by time, give a limit, or page through it with Session.scan"
            )
        return [self._row(symbol, exchange, row) for row in rows]

    def _window(
        self, scan: _Scan, low: int, high: int
    ) -> tuple[list[dict[str, Any]], int] | None:
        """Every row of ``[low, high]`` the read keeps, in key order, and how many the engine sent;
        ``None`` if there are too many.

        Complete for its window whatever order the rows arrived in, which is what makes a window -
        and not the engine's ``LIMIT`` - the unit a page is assembled from.
        """
        price = ""
        if scan.price_low is not None or scan.price_high is not None:
            low_price = MIN_PRICE if scan.price_low is None else scan.price_low
            high_price = MAX_PRICE if scan.price_high is None else scan.price_high
            price = f" AND price BETWEEN {low_price} AND {high_price}"
        query = (
            f"SELECT * FROM {_quote_literal(scan.symbol)}.{_quote_literal(scan.exchange)} "
            f"WHERE timestamp BETWEEN {low} AND {high}{price} LIMIT {SCAN_CHUNK_ROWS + 1}"
        )
        raw = self._query(query)
        if len(raw) > SCAN_CHUNK_ROWS:
            return None
        rows = [
            row for row in (self._row(scan.symbol, scan.exchange, item) for item in raw)
            if scan.keeps(row)
        ]
        rows.sort(key=_key, reverse=scan.descending)
        return rows, len(raw)

    def select_rows(self, table: str, plan: ReadPlan) -> list[dict[str, Any]]:
        """One page of one book in key order, assembled from windows of time.

        The engine answers in arrival order, so the page cannot be its ``LIMIT``. Instead this
        reads a window of time completely, sorts it, and moves on to the next window in the scan's
        direction until it has ``limit + 1`` rows (``Session.scan`` keeps ``limit`` and derives the
        next position from the last). The window starts at :data:`SCAN_FIRST_WINDOW_NS`, doubles
        while windows are sparse and halves when one holds more than :data:`SCAN_CHUNK_ROWS`, so
        memory stays bounded and the number of rows read follows the page, not the book. One
        instant holding more than that many rows is refused.

        Two rows with one key in the page are refused, as in :meth:`get`: a position between them
        would skip or repeat one of them.
        """
        self._table(table)
        scan = _scan_of(plan)
        if scan is None:
            return []
        self.flush()
        need = plan.limit + 1
        page: list[dict[str, Any]] = []
        width = SCAN_FIRST_WINDOW_NS
        low, high = scan.low, scan.high
        while low <= high and len(page) < need:
            if scan.descending:
                start, end = max(low, high - width + 1), high
            else:
                start, end = low, min(high, low + width - 1)
            window = self._window(scan, start, end)
            if window is None:
                if start == end:
                    raise EngineError(
                        f"{scan.symbol} {scan.exchange} holds more than {SCAN_CHUNK_ROWS} rows at "
                        f"the instant {start}; that many updates at one nanosecond is a key "
                        f"collision on a scale this read cannot page through"
                    )
                width = max(1, (end - start + 1) // 2)
                continue
            rows, sent = window
            page.extend(rows[: need - len(page)])
            if scan.descending:
                high = start - 1
            else:
                low = end + 1
            # Grown on what the engine sent, not on what the read kept: a filter that keeps few
            # rows of a dense window must not double it into one that holds too many. And only
            # while a doubled window would still fit, so a sparse stretch followed by a dense one
            # does not alternate between an oversized read and a halving.
            if len(page) < need and sent * 4 < SCAN_CHUNK_ROWS:
                width *= 2
        for previous, current in pairwise(page):
            if _key(previous) == _key(current):
                raise EngineError(self._duplicate(table, current, 2))
        return page

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
