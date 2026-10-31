"""``examples/book-stream`` against a live orderbook server, as a client runs it, from Python and
from TypeScript: one feed sent whole and as its changes, and the book the engine composes read back
at every tenth step and compared with the feed's own.

The two halves walk the same feed from the same seed, so they must send the same updates: the test
compares what each stream counted, across the two libraries. Runs in the SDK's ``orderbook`` CI job;
locally, with ``SDE_ORDERBOOK_TCP`` and the TypeScript library built (``npm run build``).
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
from _orderbook_live import ENABLED, REASON, TCP

ROOT = Path(__file__).resolve().parents[2]
EXAMPLE = ROOT / "examples" / "book-stream"

pytestmark = pytest.mark.skipif(
    not (ENABLED and TCP), reason=f"{REASON}; this one also needs SDE_ORDERBOOK_TCP"
)


def _run(command: list[str], cwd: Path, *, expect: int = 0) -> dict[str, Any]:
    env = {**os.environ, "BOOK_STREAM_OB_DSN": f"orderbook://{TCP}"}
    done = subprocess.run(command, cwd=cwd, env=env, capture_output=True, text=True, timeout=600)
    assert done.returncode == expect, done.stdout + done.stderr
    result: dict[str, Any] = json.loads(done.stdout)
    return result


def _node_half(tmp_path: Path) -> Path:
    """The TypeScript half, from the library built in this tree, laid out as a package would be."""
    package = ROOT / "typescript"
    if not (package / "dist" / "engines" / "orderbook.js").is_file():
        pytest.fail("build the TypeScript library first: (cd typescript && npm run build)")
    node = tmp_path / "node"
    (node / "node_modules" / "@smart-data-engines").mkdir(parents=True)
    (node / "node_modules" / "@smart-data-engines" / "sde").symlink_to(package)
    for name in ("book-stream.mjs", "model.json"):
        shutil.copyfile(EXAMPLE / name, node / name)
    return node


def test_both_halves_store_the_book_the_feed_gave_and_send_the_same(tmp_path: Path) -> None:
    steps = ["--steps", "300"]
    python = _run([sys.executable, str(EXAMPLE / "book_stream.py"), *steps], tmp_path)
    node = _run(["node", "book-stream.mjs", *steps], _node_half(tmp_path))
    for half in (python, node):
        assert half["mismatches"] == []
        assert half["books_read"] == 60   # every tenth of 300 steps, two books each
    assert python["streams"] == node["streams"]
    full, incremental = python["streams"]["full"], python["streams"]["incremental"]
    assert full["sides_whole"] == full["updates"] == 300
    # The incremental stream sends the feed's changes as they came and its snapshots' differences,
    # with a whole side where that is smaller: fewer levels than the full stream's every side.
    assert incremental["updates_of_changes"] > 0
    assert incremental["levels_sent"] < full["levels_sent"]


def test_without_a_server_it_says_what_to_set(tmp_path: Path) -> None:
    done = subprocess.run(
        [sys.executable, str(EXAMPLE / "book_stream.py")],
        cwd=tmp_path, env={k: v for k, v in os.environ.items() if k != "BOOK_STREAM_OB_DSN"},
        capture_output=True, text=True, timeout=60,
    )
    assert done.returncode == 2
    assert "BOOK_STREAM_OB_DSN" in done.stderr
