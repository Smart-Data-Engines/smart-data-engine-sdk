"""The book stream (sde.books, the engine's #233) against the shared vectors, and its own checks.

`conformance/vectors/books/` holds what every library's stream sends (contract §8c): the
difference of two sides, and a stream's calls on a clock with the whole sides and changes they
send. The vectors were written by hand, not computed by this library, which is what lets them
check it.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from sde.books import BookStream, Resync, book_diff, ordered_changes

VECTORS = Path(__file__).resolve().parents[2] / "conformance" / "vectors" / "books"


def _cases(kind: str) -> list[dict]:
    cases = [json.loads((d / "case.json").read_text()) | {"id": d.name}
             for d in sorted(VECTORS.iterdir()) if d.is_dir()]
    kinds = {case["kind"] for case in cases}
    assert kinds == {"diff", "stream"}, f"a kind this runner does not know: {kinds}"
    return [case for case in cases if case["kind"] == kind]


@pytest.mark.parametrize("case", _cases("diff"), ids=lambda c: c["id"])
def test_the_difference_is_the_vectors(case: dict) -> None:
    if "error" in case:
        with pytest.raises(ValueError, match=case["error"]):
            book_diff(case["side"], case["before"], case["after"])
        return
    got = book_diff(case["side"], case["before"], case["after"])
    assert [list(lv) for lv in got] == case["diff"], case["why"]


class _Sink:
    def __init__(self) -> None:
        self.sent: list[dict] = []
        self.fail_next = False

    def _send(self, kind: str, side: str, levels: list, timestamp_ns: int) -> None:
        if self.fail_next:
            self.fail_next = False
            raise RuntimeError("sink refused")
        self.sent.append(
            {"kind": kind, "side": side, "ts": timestamp_ns, "levels": [list(lv) for lv in levels]}
        )

    def send_whole(self, side: str, levels: list, timestamp_ns: int) -> None:
        self._send("whole", side, levels, timestamp_ns)

    def send_changes(self, side: str, levels: list, timestamp_ns: int) -> None:
        self._send("changes", side, levels, timestamp_ns)


def _step(stream: BookStream, step: dict) -> None:
    ts = step.get("ts")
    if step["op"] == "resync":
        stream.resync(step.get("side"), timestamp_ns=ts)
    elif step["op"] == "update":
        stream.update(step["side"], step["levels"], timestamp_ns=ts)
    else:
        stream.apply(step["side"], step["levels"], timestamp_ns=ts)


@pytest.mark.parametrize("case", _cases("stream"), ids=lambda c: c["id"])
def test_a_stream_sends_what_the_vectors_say(case: dict) -> None:
    sink = _Sink()
    now = [0.0]

    def wall() -> int:
        assert "wall_ns" in case, "a step without a time in a case without the machine's clock"
        return int(case["wall_ns"])

    stream = BookStream(
        sink,
        mode=case["mode"],
        resync=Resync(case["resync"]["updates"], case["resync"]["seconds"]),
        max_levels=case.get("max_levels", 1000),
        clock=lambda: now[0],
        wall_clock_ns=wall,
    )
    for step in case["steps"]:
        now[0] = float(step["at"])
        if step["op"] == "fail_next":
            sink.fail_next = True
            continue
        if "error" in step:
            before = len(sink.sent)
            with pytest.raises((ValueError, RuntimeError), match=step["error"]):
                _step(stream, step)
            assert len(sink.sent) == before
        else:
            _step(stream, step)
    assert sink.sent == case["sent"]
    for side, levels in case.get("book", {}).items():
        assert [list(lv) for lv in stream.book(side)] == levels


def test_ordered_changes_refuses_a_price_twice() -> None:
    with pytest.raises(ValueError, match="twice"):
        ordered_changes("bid", [[100, 1, 1], [100, 0, 0]])


def test_a_stream_counts_what_it_sent() -> None:
    sink = _Sink()
    stream = BookStream(sink, mode="incremental", resync=Resync(1000, 60), clock=lambda: 0.0)
    stream.update("bid", [[100, 1, 1], [99, 1, 1]])
    stream.update("bid", [[100, 1, 1], [99, 2, 1]])
    stream.update("bid", [[100, 1, 1], [99, 2, 1]])
    stats = stream.stats
    assert (stats.updates, stats.whole, stats.changes, stats.unchanged) == (3, 1, 1, 1)
    assert (stats.levels_whole, stats.levels_changes) == (2, 1)


def test_the_settings_refuse_what_they_cannot_mean() -> None:
    with pytest.raises(ValueError):
        Resync(-1, 60)
    with pytest.raises(ValueError):
        Resync(10, float("nan"))
    with pytest.raises(ValueError, match="mode"):
        BookStream(_Sink(), mode="some")
    with pytest.raises(ValueError, match="max_levels"):
        BookStream(_Sink(), max_levels=0)
    with pytest.raises(ValueError, match="side"):
        BookStream(_Sink()).update("buy", [])
    with pytest.raises(ValueError, match="timestamp_ns"):
        BookStream(_Sink()).update("bid", [], timestamp_ns=-1)


def test_a_refused_time_changes_nothing() -> None:
    """The refusal is checked before anything moves: the next update is a difference against the
    side last sent, not against the refused one."""
    sink = _Sink()
    stream = BookStream(sink, clock=lambda: 0.0)
    stream.update("bid", [[100, 1, 1], [99, 1, 1]], timestamp_ns=20)
    with pytest.raises(ValueError, match="earlier"):
        stream.update("bid", [[101, 1, 1], [99, 1, 1]], timestamp_ns=19)
    assert stream.book("bid") == [(100, 1, 1), (99, 1, 1)]
    assert stream.stats.updates == 1
