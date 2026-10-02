"""PostgreSQL, ClickHouse and the orderbook engine in one map, through a staging and a cutover.

The orderbook engine cannot fence writes, so under map contract 6 its group carries no write
generation, and the local operator moves the other group without a binding for it. Runs in the
``orderbook`` CI job, which has all three engines; ``_orderbook_live.py`` says how to run it
locally, with ``SDE_POSTGRES_DSN`` and ``SDE_CLICKHOUSE_DSN`` as for every live slice. The scenario
is in ``_unfenced_scenario.py``.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from _orderbook_live import ENABLED, MODES, REASON, fresh_book, open_engine
from _unfenced_scenario import run

pytestmark = pytest.mark.skipif(not ENABLED, reason=REASON)


@pytest.mark.parametrize("mode", MODES)
def test_an_operator_moves_a_fenced_group_beside_an_orderbook_it_never_binds(
    mode: str, tmp_path: Path
) -> None:
    engine = open_engine(mode, tmp_path)
    engine.connect()
    try:
        run(engine, tmp_path, symbol=fresh_book())
    finally:
        engine.close()
