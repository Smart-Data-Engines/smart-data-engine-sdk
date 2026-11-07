"""Erasure on Elasticsearch: by ``_id``, beside PostgreSQL, from a source and from a copy.

The fixture is the Elasticsearch move's (``test_elasticsearch_operator_live.moving``): a group in
force on one engine with one row, and a staging that adds a copy in the other. The control in each
test is a row of another key, which has to survive in both.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from test_elasticsearch_operator_live import moving

from sde.erasure import ErasureRequest

ERASED = 734512981


@pytest.mark.parametrize("source", ["postgres", "elasticsearch"])
def test_an_erasure_reaches_a_document_and_a_row_of_one_group(source: str, tmp_path: Path) -> None:
    with moving(source, tmp_path) as move:
        move.operator.stage(move.stage)
        session = move.session()
        for identity in (2, ERASED):
            session.save("Event", {"id": identity, "value": 5, "note": "both"})
        request = ErasureRequest.build(move.model, "Event", [{"id": ERASED}])

        plan = move.operator.erase(request)
        assert {(t.role, t.dialect, t.matching) for t in plan.tables} == {
            ("source", source, 1),
            ("copy", move.target, 1),
        }
        record = move.operator.erase(request, execute=True).as_record()
        assert record["outcome"] == "erased"
        assert {(t["dialect"], t["deleted"], t["remaining"]) for t in record["tables"]} == {
            (source, 1, 0),
            (move.target, 1, 0),
        }
        engines = {"postgres": move.pg.operator, "elasticsearch": move.es.operator}
        tables = {source: move.source_table, move.target: move.copy}
        for dialect, table in tables.items():
            assert engines[dialect].get(table, {"id": ERASED}) is None
            assert engines[dialect].get(table, {"id": 2}) is not None
        assert session.get("Event", {"id": 2}) == {"id": 2, "value": 5, "note": "both"}
