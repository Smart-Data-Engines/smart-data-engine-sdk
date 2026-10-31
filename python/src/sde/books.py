"""A book stream: one side of an order book at a time, sent whole or as its changes.

The orderbook engine reads an update by price (its #233): each level sets the quantity of its
price, a quantity of 0 removes the price, and a price the update does not name stays. So a writer
of books can send two things, and a :class:`BookStream` sends either:

- ``mode="full"``: every side the application gives is sent whole, as a side replacement - the
  engine removes every price the side holds and the update does not name (``MREPLACE``);
- ``mode="incremental"``: only what changed is sent, as an ordinary update. From whole sides,
  through :meth:`BookStream.update`, the stream computes the difference against the side it last
  sent; from changes the application already has - an exchange's diff stream - through
  :meth:`BookStream.apply`. The engine stores the changes and composes the book from them.

An incremental stream sends a side whole instead of its changes:

- the first time it sends the side, and after a send that failed, when it does not know what the
  engine holds;
- every ``resync`` updates or seconds, whichever comes first, which heals a difference nobody
  noticed - a change the application missed;
- when the whole side is smaller than its changes. This also keeps an update within the engine's
  ``max_levels``: two sides of 1000 prices can differ in 2000.

Two rules hold in both modes. A side holds at most ``max_levels`` prices, the engine's depth:
:meth:`BookStream.update` refuses a deeper side, and a side :meth:`BookStream.apply` grows deeper is
sent whole as its best ``max_levels``. And a side's times never go back: the engine's history
applies updates in time order and its live book in the order they arrive, so an update earlier
than the last one sent would make the two disagree. A refused call sends nothing and changes
nothing.

:func:`book_diff` is the difference itself, and every library computes it the same way: the shared
vectors in ``conformance/vectors/books/`` hold each one to it, and to what a stream sends.
"""

from __future__ import annotations

import math
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Literal, Protocol

Side = Literal["bid", "ask"]
#: One level: price, quantity, order count - the engine's integers.
Level = tuple[int, int, int]

SIDES: tuple[Side, ...] = ("bid", "ask")
MODES = ("full", "incremental")
#: The orderbook engine's depth: the prices a side holds, and the levels one update carries.
ENGINE_DEPTH = 1000
MAX_TIMESTAMP_NS = (1 << 63) - 1

__all__ = [
    "ENGINE_DEPTH",
    "BookSink",
    "BookStream",
    "Level",
    "Resync",
    "Side",
    "StreamStats",
    "book_diff",
    "ordered_changes",
]


def _best_first(side: Side, prices: list[int]) -> list[int]:
    """Prices in the side's own order: bids from the highest, asks from the lowest."""
    return sorted(prices, reverse=(side == "bid"))


def _side_of(side: str) -> Side:
    if side == "bid":
        return "bid"
    if side == "ask":
        return "ask"
    raise ValueError(f"side must be 'bid' or 'ask', not {side!r}")


def _level(raw: Sequence[int], where: str) -> Level:
    if len(raw) != 3:
        raise ValueError(f"{where}is {list(raw)!r}: a level is (price, quantity, order count)")
    price, quantity, count = raw
    for name, value in (("price", price), ("quantity", quantity), ("order count", count)):
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError(f"{where}{name} is {value!r}, which is not an integer")
    if quantity < 0 or count < 0:
        raise ValueError(f"{where}has a negative quantity or order count")
    return (price, quantity, count)


def _whole_side(side: Side, levels: Sequence[Sequence[int]]) -> dict[int, tuple[int, int]]:
    """A whole side, checked: each price once, none with a quantity of 0."""
    out: dict[int, tuple[int, int]] = {}
    for i, raw in enumerate(levels):
        price, quantity, count = _level(raw, f"{side} level {i} ")
        if quantity == 0:
            raise ValueError(
                f"{side} level {i} has a quantity of 0. A whole side lists the prices it holds; a "
                f"removal is a change, which apply() sends"
            )
        if price in out:
            raise ValueError(f"{side} price {price} is in the side twice")
        out[price] = (quantity, count)
    return out


def _levels_of(side: Side, book: Mapping[int, tuple[int, int]]) -> list[Level]:
    return [(p, book[p][0], book[p][1]) for p in _best_first(side, list(book))]


def book_diff(
    side: str, before: Sequence[Sequence[int]], after: Sequence[Sequence[int]]
) -> list[Level]:
    """What changes ``before`` into ``after``, one side of a book, as the engine applies an update.

    First a quantity of 0 for every price of ``before`` that ``after`` does not hold, best first;
    then every price of ``after`` that ``before`` does not hold, or holds with another quantity or
    count, best first. The removals go first because the engine applies an update in its order,
    and a full side - 1000 prices - has no room for a price it does not hold until they are
    applied.

    Empty when the two sides are the same: an update that changes nothing is not sent.
    """
    s = _side_of(side)
    old = _whole_side(s, before)
    new = _whole_side(s, after)
    removals = [(price, 0, 0) for price in _best_first(s, [p for p in old if p not in new])]
    changes = [
        (price, new[price][0], new[price][1])
        for price in _best_first(s, [p for p in new if old.get(p) != new[p]])
    ]
    return removals + changes


def ordered_changes(side: str, changes: Sequence[Sequence[int]]) -> list[Level]:
    """Changes as the application has them - a quantity of 0 a removal - in the order a stream
    sends them: the removals best first, then the rest best first. Each price once."""
    s = _side_of(side)
    seen: dict[int, Level] = {}
    for i, raw in enumerate(changes):
        level = _level(raw, f"{s} change {i} ")
        if level[0] in seen:
            raise ValueError(f"{s} price {level[0]} is changed twice in one update")
        seen[level[0]] = level
    removals = _best_first(s, [p for p, lv in seen.items() if lv[1] == 0])
    rest = _best_first(s, [p for p, lv in seen.items() if lv[1] != 0])
    return [(p, 0, 0) for p in removals] + [seen[p] for p in rest]


@dataclass(frozen=True)
class Resync:
    """When an incremental stream sends a side whole: after ``updates`` updates of the side since
    it last did, or ``seconds`` since then, whichever comes first. ``0`` turns that clause off."""

    updates: int = 1000
    seconds: float = 60.0

    def __post_init__(self) -> None:
        updates = self.updates
        if isinstance(updates, bool) or not isinstance(updates, int) or updates < 0:
            raise ValueError(f"resync updates must be a non-negative integer, not {updates!r}")
        seconds = self.seconds
        if not isinstance(seconds, (int, float)) or not math.isfinite(seconds) or seconds < 0:
            raise ValueError(f"resync seconds must be a non-negative number, not {seconds!r}")


class BookSink(Protocol):
    """Where a stream's updates go: the orderbook engine, through a session."""

    def send_whole(self, side: Side, levels: list[Level], timestamp_ns: int) -> None: ...

    def send_changes(self, side: Side, levels: list[Level], timestamp_ns: int) -> None: ...


@dataclass
class _SideState:
    book: dict[int, tuple[int, int]] = field(default_factory=dict)
    sent_whole: bool = False      # the side has been sent whole since the stream began
    unsure: bool = False          # a send failed: what the engine holds is not known
    since_whole: int = 0          # updates since the side was last sent whole
    whole_at: float = 0.0         # the policy clock when it was
    last_ns: int = -1             # the time of the last update accepted for the side


@dataclass
class StreamStats:
    """What a stream sent: updates and levels, whole and as changes - what a transfer is made of."""

    updates: int = 0              # update() and apply() calls accepted
    whole: int = 0                # sides sent whole
    changes: int = 0              # updates of changes sent
    unchanged: int = 0            # updates that changed nothing, and were not sent
    levels_whole: int = 0
    levels_changes: int = 0


class BookStream:
    """One book - one symbol on one exchange - sent whole or as its changes. See the module."""

    def __init__(
        self,
        sink: BookSink,
        *,
        mode: str = "incremental",
        resync: Resync | None = None,
        max_levels: int = ENGINE_DEPTH,
        clock: Callable[[], float] = time.monotonic,
        wall_clock_ns: Callable[[], int] = time.time_ns,
    ) -> None:
        if mode not in MODES:
            raise ValueError(f"mode must be one of {list(MODES)}, not {mode!r}")
        if isinstance(max_levels, bool) or not isinstance(max_levels, int) or max_levels < 1:
            raise ValueError(f"max_levels must be a positive integer, not {max_levels!r}")
        self._sink = sink
        self._mode = mode
        self._resync = resync if resync is not None else Resync()
        self._max_levels = max_levels
        self._clock = clock
        self._wall_clock_ns = wall_clock_ns
        self._sides: dict[Side, _SideState] = {s: _SideState() for s in SIDES}
        self.stats = StreamStats()

    @property
    def mode(self) -> str:
        return self._mode

    def book(self, side: str) -> list[Level]:
        """The side as the stream holds it, best first: what it last sent, with every change
        :meth:`apply` was given - deeper than ``max_levels`` if they made it so."""
        s = _side_of(side)
        return _levels_of(s, self._sides[s].book)

    def update(
        self, side: str, levels: Sequence[Sequence[int]], *, timestamp_ns: int | None = None
    ) -> None:
        """The whole side as it is now: sent whole in ``full`` mode, its difference in
        ``incremental``. Without a time, this machine's clock when it is called."""
        s = _side_of(side)
        new = _whole_side(s, levels)
        if len(new) > self._max_levels:
            raise ValueError(
                f"the {s} side has {len(new)} prices and the engine keeps {self._max_levels} a "
                f"side: give its best {self._max_levels}. Nothing was sent"
            )
        state = self._sides[s]
        stamp = self._stamp(s, state, timestamp_ns)
        self.stats.updates += 1
        if self._mode == "full" or self._due(state):
            self._send_whole(s, state, new, stamp)
            return
        diff = book_diff(s, _levels_of(s, state.book), _levels_of(s, new))
        if len(diff) > len(new):
            self._send_whole(s, state, new, stamp)
            return
        self._send_changes(s, state, new, diff, stamp)

    def apply(
        self, side: str, changes: Sequence[Sequence[int]], *, timestamp_ns: int | None = None
    ) -> None:
        """Changes to the side - a quantity of 0 removes a price - applied to the stream's book and
        sent as given, removals first; or the side whole, when the module says so. ``incremental``
        only. Without a time, this machine's clock when it is called."""
        s = _side_of(side)
        if self._mode != "incremental":
            raise ValueError(
                "apply() sends changes, which a full stream does not: it sends every side whole. "
                "Give it the whole side with update(), or open the stream incremental"
            )
        ordered = ordered_changes(s, changes)
        state = self._sides[s]
        stamp = self._stamp(s, state, timestamp_ns)
        self.stats.updates += 1
        new = dict(state.book)
        for price, quantity, count in ordered:
            if quantity == 0:
                new.pop(price, None)
            else:
                new[price] = (quantity, count)
        if self._due(state) or len(ordered) > min(len(new), self._max_levels):
            self._send_whole(s, state, new, stamp)
            return
        # Sent as given, a removal of a price the stream's book does not hold included: the
        # engine's book may hold it, and removing a price nothing holds changes nothing.
        self._send_changes(s, state, new, ordered, stamp)

    def resync(self, side: str | None = None, *, timestamp_ns: int | None = None) -> None:
        """Send the side - both, without one - whole now, whatever the policy says. Without a
        time, the time of the side's last update, so the side is the book as of it; this
        machine's clock for a side the stream has not sent."""
        sides = SIDES if side is None else (_side_of(side),)
        stamps: list[tuple[Side, int]] = []
        for s in sides:
            state = self._sides[s]
            if timestamp_ns is None and state.last_ns >= 0:
                stamps.append((s, state.last_ns))
            else:
                stamps.append((s, self._stamp(s, state, timestamp_ns, record=False)))
        for s, stamp in stamps:
            state = self._sides[s]
            state.last_ns = max(state.last_ns, stamp)
            self._send_whole(s, state, dict(state.book), stamp)

    # --- internals -----------------------------------------------------------------------------

    def _stamp(
        self, side: Side, state: _SideState, timestamp_ns: int | None, *, record: bool = True
    ) -> int:
        if timestamp_ns is None:
            stamp = max(self._wall_clock_ns(), state.last_ns)
        else:
            if (
                isinstance(timestamp_ns, bool)
                or not isinstance(timestamp_ns, int)
                or not 0 <= timestamp_ns <= MAX_TIMESTAMP_NS
            ):
                raise ValueError(
                    f"timestamp_ns must be an integer of nanoseconds in [0, 2^63), "
                    f"not {timestamp_ns!r}"
                )
            if timestamp_ns < state.last_ns:
                raise ValueError(
                    f"timestamp_ns {timestamp_ns} is earlier than {state.last_ns}, the time of the "
                    f"last update of the {side} side: the engine's history applies updates in "
                    f"time order and its live book in the order they arrive, so the two would "
                    f"disagree. Nothing was sent"
                )
            stamp = timestamp_ns
        if record:
            state.last_ns = stamp
        return stamp

    def _due(self, state: _SideState) -> bool:
        if not state.sent_whole or state.unsure:
            return True
        r = self._resync
        if r.updates and state.since_whole >= r.updates:
            return True
        return bool(r.seconds) and self._clock() - state.whole_at >= r.seconds

    def _send_whole(
        self,
        side: Side,
        state: _SideState,
        new: dict[int, tuple[int, int]],
        timestamp_ns: int,
    ) -> None:
        levels = _levels_of(side, new)[: self._max_levels]
        state.book = new
        try:
            self._sink.send_whole(side, levels, timestamp_ns)
        except BaseException:
            state.unsure = True
            raise
        state.unsure = False
        state.sent_whole = True
        state.since_whole = 0
        state.whole_at = self._clock()
        self.stats.whole += 1
        self.stats.levels_whole += len(levels)

    def _send_changes(
        self,
        side: Side,
        state: _SideState,
        new: dict[int, tuple[int, int]],
        levels: list[Level],
        timestamp_ns: int,
    ) -> None:
        state.book = new
        state.since_whole += 1
        if not levels:
            self.stats.unchanged += 1
            return
        try:
            self._sink.send_changes(side, levels, timestamp_ns)
        except BaseException:
            state.unsure = True
            raise
        self.stats.changes += 1
        self.stats.levels_changes += len(levels)
