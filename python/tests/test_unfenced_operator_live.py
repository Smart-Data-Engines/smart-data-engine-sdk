"""A group without a write generation beside a staging and a cutover, with the memory engine.

The engine without fences here is the SDK's memory engine, so this runs wherever PostgreSQL and
ClickHouse do; the orderbook engine itself plays the part in ``test_orderbook_three_engines.py``.
The scenario is in ``_unfenced_scenario.py``.
"""

from __future__ import annotations

from pathlib import Path

from _unfenced_scenario import run

from sde.testing.memory import MemoryEngine


def test_an_operator_moves_a_fenced_group_and_never_binds_the_engine_without_fences(
    tmp_path: Path,
) -> None:
    run(
        MemoryEngine("orderbook", name="book", can_keep_bookkeeping=False),
        tmp_path,
        symbol="BTCUSDT",
    )
