#!/usr/bin/env python3
"""Write conformance/vectors/books/: what every library's book stream sends (contract §8c).

    python3 conformance/tools/books_vectors.py [--check]

`--check` writes nothing and fails if a vector would change, or if a case directory is there that
this file no longer writes: CI's contract job runs it, so the vectors cannot drift from what is
written here.

One directory a case, `NNN-<name>/case.json`, of two kinds:
- `"kind": "diff"`: a side before and after, and the difference a stream sends - or the refusal;
- `"kind": "stream"`: a stream's calls on a clock, and what it sends - whole sides and changes, each
  with its time - or that a call is refused.

The expectations are written out here by hand, not computed by any library: a vector computed by the
implementation it checks would check nothing. A level is `[price, quantity, order_count]`. Rewriting
the tree is idempotent, and it removes a case directory this file no longer writes.
"""

from __future__ import annotations

import argparse
import json
import re
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1] / "vectors" / "books"


def diff_cases() -> list[dict[str, Any]]:
    cases = [
        {
            "name": "an empty side to one level",
            "side": "bid", "before": [], "after": [[100, 5, 1]],
            "diff": [[100, 5, 1]],
            "why": "the first update of a side is every level of it",
        },
        {
            "name": "the same side is no update",
            "side": "ask", "before": [[200, 5, 1], [201, 6, 2]],
            "after": [[201, 6, 2], [200, 5, 1]],
            "diff": [],
            "why": "an update that changes nothing is not sent, whatever order the levels came in",
        },
        {
            "name": "a removal goes before a new price",
            "side": "bid", "before": [[100, 5, 1], [99, 3, 1]], "after": [[101, 2, 1], [99, 3, 1]],
            "diff": [[100, 0, 0], [101, 2, 1]],
            "why": "a full side has no room for a new price until the removals are applied",
        },
        {
            "name": "a count alone is a change",
            "side": "ask", "before": [[200, 5, 1]], "after": [[200, 5, 2]],
            "diff": [[200, 5, 2]],
            "why": "the engine stores the order count: a book whose count moved is another book",
        },
        {
            "name": "asks from the lowest, the input in any order",
            "side": "ask",
            "before": [[203, 1, 1], [201, 1, 1], [202, 1, 1]],
            "after": [[204, 1, 1], [202, 1, 1]],
            "diff": [[201, 0, 0], [203, 0, 0], [204, 1, 1]],
            "why": "removals best first, then changes best first: the side's order, not the input",
        },
        {
            "name": "bids from the highest",
            "side": "bid", "before": [], "after": [[1, 1, 1], [3, 1, 1], [2, 1, 1]],
            "diff": [[3, 1, 1], [2, 1, 1], [1, 1, 1]],
            "why": "a bid's best is its highest",
        },
        {
            "name": "a side emptied",
            "side": "ask", "before": [[200, 1, 1], [201, 2, 2]], "after": [],
            "diff": [[200, 0, 0], [201, 0, 0]],
            "why": "every price removed, as quantity 0",
        },
        {
            "name": "negative prices",
            "side": "bid", "before": [[-5, 1, 1]], "after": [[-3, 1, 1], [-5, 2, 1]],
            "diff": [[-3, 1, 1], [-5, 2, 1]],
            "why": "a price is a signed integer in the smallest sub-unit, and orders as one",
        },
        {
            "name": "a price twice in a side is refused",
            "side": "bid", "before": [], "after": [[100, 1, 1], [100, 2, 1]],
            "error": "twice",
            "why": "a side holds one quantity a price; two would leave it to the order of a list",
        },
        {
            "name": "a quantity of 0 in a whole side is refused",
            "side": "ask", "before": [], "after": [[200, 0, 0]],
            "error": "quantity of 0",
            "why": "a whole side lists the prices it holds; a removal is a change",
        },
    ]
    # A full side of 1000 bids moved up by one tick: one removal, one new price.
    before = [[1000 - i, 1 + i % 7, 1] for i in range(1000)]
    after = [[1001 - i, 1 + (i + 6) % 7, 1] for i in range(1000)]
    cases.append({
        "name": "a full side of 1000 moved by one tick",
        "side": "bid", "before": before, "after": after,
        "diff": [[1, 0, 0], [1001, 7, 1]],
        "why": "the engine's 1000 levels a side: the removal first, or the new price has no room",
    })
    return cases


def _step(at: int, op: str, side: str | None = None, levels: list[list[int]] | None = None,
          ts: int | None = None, error: str | None = None) -> dict[str, Any]:
    out: dict[str, Any] = {"at": at, "op": op}
    if side is not None:
        out["side"] = side
    if levels is not None:
        out["levels"] = levels
    if ts is not None:
        out["ts"] = ts
    if error is not None:
        out["error"] = error
    return out


def _sent(kind: str, side: str, ts: int, levels: list[list[int]]) -> dict[str, Any]:
    return {"kind": kind, "side": side, "ts": ts, "levels": levels}


def stream_cases() -> list[dict[str, Any]]:
    """A case: `mode`, `resync`, optionally `max_levels` (else 1000) and `wall_ns` (the machine's
    clock for a step without a time). A step: `at`, the policy clock in seconds when it is made;
    `op` - update, apply, resync, or fail_next (the sink refuses its next send); `side`, `levels`,
    `ts`, and `error`, a word of the refusal it must meet. `sent`: every update the sink received,
    with the time it carried. `book`: the stream's own book after the last step."""
    a = [[100, 5, 1], [99, 3, 1]]
    b = [[101, 2, 1], [99, 3, 1]]
    c = [[101, 2, 1], [98, 4, 2]]
    d = [[102, 1, 1]]
    every = {"updates": 1000, "seconds": 60}
    x = [[200, 1, 1], [201, 1, 1], [202, 1, 1]]
    y = [[200, 1, 1], [201, 2, 1], [202, 1, 1]]
    z = [[200, 1, 1], [201, 2, 1], [202, 3, 1]]
    e = [[100, 1, 1], [99, 1, 1], [98, 1, 1], [97, 1, 1], [96, 1, 1]]
    f = [[100, 5, 1], [99, 3, 1], [97, 1, 1], [96, 1, 1]]
    return [
        {
            "name": "a full stream sends every side whole",
            "mode": "full", "resync": every,
            "steps": [_step(0, "update", "bid", a, 10), _step(1, "update", "bid", b, 20),
                      _step(2, "update", "ask", [], 30)],
            "sent": [_sent("whole", "bid", 10, a), _sent("whole", "bid", 20, b),
                     _sent("whole", "ask", 30, [])],
        },
        {
            "name": "an incremental stream sends a side whole first, then its difference",
            "mode": "incremental", "resync": every,
            "steps": [_step(0, "update", "bid", a, 10), _step(1, "update", "bid", b, 20),
                      _step(2, "update", "bid", b, 30)],
            "sent": [_sent("whole", "bid", 10, a),
                     _sent("changes", "bid", 20, [[100, 0, 0], [101, 2, 1]])],
        },
        {
            "name": "a resync after so many updates",
            "mode": "incremental", "resync": {"updates": 2, "seconds": 0},
            "steps": [_step(0, "update", "bid", a, 10), _step(1, "update", "bid", b, 20),
                      _step(2, "update", "bid", c, 30), _step(3, "update", "bid", d, 40)],
            "sent": [_sent("whole", "bid", 10, a),
                     _sent("changes", "bid", 20, [[100, 0, 0], [101, 2, 1]]),
                     _sent("changes", "bid", 30, [[99, 0, 0], [98, 4, 2]]),
                     _sent("whole", "bid", 40, d)],
        },
        {
            "name": "a resync after so long",
            "mode": "incremental", "resync": {"updates": 0, "seconds": 10},
            "steps": [_step(0, "update", "ask", x, 10), _step(5, "update", "ask", y, 20),
                      _step(10, "update", "ask", z, 30)],
            "sent": [_sent("whole", "ask", 10, x), _sent("changes", "ask", 20, [[201, 2, 1]]),
                     _sent("whole", "ask", 30, z)],
        },
        {
            "name": "a resync sends even an unchanged side",
            "mode": "incremental", "resync": {"updates": 1, "seconds": 0},
            "steps": [_step(0, "update", "bid", a, 10), _step(1, "update", "bid", a, 20),
                      _step(2, "update", "bid", a, 30)],
            "sent": [_sent("whole", "bid", 10, a), _sent("whole", "bid", 30, a)],
        },
        {
            "name": "a side whole when it is smaller than its difference",
            "mode": "incremental", "resync": every,
            "steps": [_step(0, "update", "bid", e, 10),
                      _step(1, "update", "bid", [[110, 1, 1]], 20)],
            "sent": [_sent("whole", "bid", 10, e), _sent("whole", "bid", 20, [[110, 1, 1]])],
        },
        {
            "name": "a side emptied is sent whole, as no levels",
            "mode": "incremental", "resync": every,
            "steps": [_step(0, "update", "bid", a, 10), _step(1, "update", "bid", [], 20)],
            "sent": [_sent("whole", "bid", 10, a), _sent("whole", "bid", 20, [])],
        },
        {
            "name": "changes are sent as given, removals first",
            "mode": "incremental", "resync": every,
            "steps": [_step(0, "update", "bid", f, 10),
                      _step(1, "apply", "bid", [[99, 0, 0], [101, 5, 1], [98, 0, 7]], 20)],
            "sent": [_sent("whole", "bid", 10, f),
                     _sent("changes", "bid", 20, [[99, 0, 0], [98, 0, 0], [101, 5, 1]])],
            "book": {"bid": [[101, 5, 1], [100, 5, 1], [97, 1, 1], [96, 1, 1]]},
        },
        {
            "name": "after a send fails the side is sent whole",
            "mode": "incremental", "resync": every,
            "steps": [_step(0, "update", "bid", a, 10), _step(1, "fail_next"),
                      _step(2, "update", "bid", b, 20, error="sink"),
                      _step(3, "update", "bid", c, 30)],
            "sent": [_sent("whole", "bid", 10, a), _sent("whole", "bid", 30, c)],
        },
        {
            "name": "a full stream refuses changes",
            "mode": "full", "resync": every,
            "steps": [_step(0, "apply", "bid", [[100, 1, 1]], 10, error="full")],
            "sent": [],
        },
        {
            "name": "an explicit resync sends both sides whole, as of each side's last update",
            "mode": "incremental", "resync": every,
            "steps": [_step(0, "update", "bid", a, 10),
                      _step(1, "update", "ask", [[200, 1, 1]], 20), _step(2, "resync")],
            "sent": [_sent("whole", "bid", 10, a), _sent("whole", "ask", 20, [[200, 1, 1]]),
                     _sent("whole", "bid", 10, a), _sent("whole", "ask", 20, [[200, 1, 1]])],
        },
        {
            "name": "a time earlier than the side's last is refused",
            "mode": "incremental", "resync": every,
            "steps": [_step(0, "update", "bid", a, 20),
                      _step(1, "update", "bid", b, 10, error="earlier"),
                      _step(2, "update", "ask", [[200, 1, 1]], 10),
                      _step(3, "update", "bid", b, 20)],
            "sent": [_sent("whole", "bid", 20, a), _sent("whole", "ask", 10, [[200, 1, 1]]),
                     _sent("changes", "bid", 20, [[100, 0, 0], [101, 2, 1]])],
        },
        {
            "name": "without a time, the machine's clock, never before the side's last",
            "mode": "incremental", "resync": every, "wall_ns": 15,
            "steps": [_step(0, "update", "bid", a, 10), _step(1, "update", "bid", b),
                      _step(2, "update", "bid", c, 30), _step(3, "update", "bid", a)],
            "sent": [_sent("whole", "bid", 10, a),
                     _sent("changes", "bid", 15, [[100, 0, 0], [101, 2, 1]]),
                     _sent("changes", "bid", 30, [[99, 0, 0], [98, 4, 2]]),
                     _sent("whole", "bid", 30, a)],
        },
        {
            "name": "a side deeper than the engine keeps is refused",
            "mode": "incremental", "resync": every, "max_levels": 2,
            "steps": [_step(0, "update", "bid", [[100, 1, 1], [99, 1, 1], [98, 1, 1]], 10,
                            error="keeps 2"),
                      _step(1, "update", "bid", a, 20)],
            "sent": [_sent("whole", "bid", 20, a)],
        },
        {
            "name": "changes may make a side deeper, and a whole side is its best",
            "mode": "incremental", "resync": {"updates": 2, "seconds": 0}, "max_levels": 2,
            "steps": [_step(0, "update", "bid", a, 10),
                      _step(1, "apply", "bid", [[98, 1, 1]], 20),
                      _step(2, "apply", "bid", [[97, 1, 1]], 30),
                      _step(3, "apply", "bid", [[100, 0, 0]], 40)],
            "sent": [_sent("whole", "bid", 10, a), _sent("changes", "bid", 20, [[98, 1, 1]]),
                     _sent("changes", "bid", 30, [[97, 1, 1]]),
                     _sent("whole", "bid", 40, [[99, 3, 1], [98, 1, 1]])],
            "book": {"bid": [[99, 3, 1], [98, 1, 1], [97, 1, 1]]},
        },
        {
            # One update carries at most `max_levels` levels, which a deeper side's changes can
            # pass: three changes when two fit go as the side whole, its best two.
            "name": "more changes than one update carries send the side whole",
            "mode": "incremental", "resync": {"updates": 0, "seconds": 0}, "max_levels": 2,
            "steps": [_step(0, "update", "bid", a, 10),
                      _step(1, "apply", "bid", [[98, 1, 1]], 20),
                      _step(2, "apply", "bid", [[97, 1, 1], [96, 1, 1], [95, 1, 1]], 30)],
            "sent": [_sent("whole", "bid", 10, a), _sent("changes", "bid", 20, [[98, 1, 1]]),
                     _sent("whole", "bid", 30, a)],
            "book": {"bid": [[100, 5, 1], [99, 3, 1], [98, 1, 1], [97, 1, 1], [96, 1, 1],
                             [95, 1, 1]]},
        },
    ]


def _dump(case: dict[str, Any]) -> str:
    """The case as JSON, each level - three integers - on a line of its own."""
    text = json.dumps(case, indent=1)
    return re.sub(r"\[\s+(-?\d+),\s+(-?\d+),\s+(-?\d+)\s+\]", r"[\1, \2, \3]", text)


def _slug(name: str) -> str:
    words = "".join(c if c.isalnum() else " " for c in name.lower()).split()
    out = ""
    for word in words:
        if len(out) + len(word) + 1 > 48:
            break
        out = f"{out}-{word}" if out else word
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument("--check", action="store_true", help="fail if a vector would change")
    args = parser.parse_args()
    cases = [{"kind": "diff", **case} for case in diff_cases()]
    cases += [{"kind": "stream", **case} for case in stream_cases()]
    planned = {
        f"{number:03d}-{_slug(case['name'])}": _dump(case) + "\n"
        for number, case in enumerate(cases, start=1)
    }
    changed = []
    for name, text in planned.items():
        path = ROOT / name / "case.json"
        if path.exists() and path.read_text(encoding="utf-8") == text:
            continue
        changed.append(f"{name}/case.json")
        if not args.check:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
    stale = sorted(p for p in ROOT.iterdir() if p.name not in planned) if ROOT.exists() else []
    for folder in stale:
        changed.append(f"{folder.name}/ (no longer written)")
        if not args.check:
            for item in folder.iterdir():
                item.unlink()
            folder.rmdir()
    if args.check and changed:
        print("books vectors would change:", *changed, sep="\n  ")
        return 1
    written = "unchanged" if not changed else f"{len(changed)} change(s) written"
    print(f"{ROOT.relative_to(ROOT.parents[1])}: {len(cases)} case(s), {written}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
