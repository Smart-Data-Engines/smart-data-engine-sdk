"""The write-fence protocol on a live Elasticsearch (write-fences.md, format contract 7m).

Set SDE_ELASTICSEARCH_DSN to run (``make es-up`` prints it), as the adapter's other live tests do.
Each test makes its own index and deletes it, with its fence pipeline and any key it created.
"""

from __future__ import annotations

import json
import threading
import time
import urllib.parse
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any
from uuid import uuid4

import pytest

import sde
from sde.engines._write_fences import fence_pipeline
from sde.engines.elasticsearch import ElasticsearchEngine
from sde.write_fence import EPOCH_COLUMN

DSN = __import__("os").environ.get("SDE_ELASTICSEARCH_DSN")
pytestmark = pytest.mark.skipif(DSN is None, reason="set SDE_ELASTICSEARCH_DSN to run")
PROJECT = "0" * 31 + "1"
LAYOUT_COLUMNS = {"id": "long", "body": "keyword"}


def _raw(engine: ElasticsearchEngine, method: str, path: str, body: Any = None,
         ndjson: bytes | None = None) -> tuple[int, Any]:
    if ndjson is not None:
        return engine._request(method, path, ndjson=ndjson)
    return engine._request(method, path, body)


@contextmanager
def fresh_index() -> Iterator[tuple[ElasticsearchEngine, str]]:
    assert DSN is not None
    engine = ElasticsearchEngine(DSN)
    engine.connect()
    index = "sde_fence_" + uuid4().hex[:12]
    layout = sde.PhysicalLayout(tables={"Note": index}, columns={"Note": LAYOUT_COLUMNS})
    try:
        engine.ensure_schema(layout, keys={"Note": ["id"]})
        yield engine, index
    finally:
        _raw(engine, "PUT", f"/{index}/_settings", {"index.blocks.write": False})
        _raw(engine, "DELETE", f"/{index}")
        _raw(engine, "DELETE", f"/_ingest/pipeline/{fence_pipeline(index)}")
        engine.close()


def _put(engine: ElasticsearchEngine, index: str, doc_id: str, document: dict[str, Any]) -> int:
    status, _ = _raw(engine, "PUT", f"/{index}/_doc/{doc_id}?refresh=wait_for", document)
    return status


def _count(engine: ElasticsearchEngine, index: str) -> int:
    _raw(engine, "POST", f"/{index}/_refresh")
    status, body = _raw(engine, "GET", f"/{index}/_count")
    assert status == 200
    return int(body["count"])


def test_the_engine_refuses_a_write_outside_the_generation_and_no_request_bypasses_it() -> None:
    with fresh_index() as (engine, index):
        fence = engine.write_fence(index, project_id=PROJECT)
        state = fence.prepare(3)
        assert (state.epoch, state.lower_epoch, state.upper_epoch, state.closed) == (3, 3, 3, False)
        assert _put(engine, index, "a", {"id": 1, "body": "x", EPOCH_COLUMN: 3}) in (200, 201)
        for doc_id, epoch in (("b", 2), ("c", 4), ("d", None), ("e", "3"), ("f", 3.0)):
            document: dict[str, Any] = {"id": 2, "body": "x"}
            if epoch is not None:
                document[EPOCH_COLUMN] = epoch
            assert _put(engine, index, doc_id, document) >= 400, epoch
        status, _ = _raw(engine, "PUT", f"/{index}/_doc/g?pipeline=_none&refresh=wait_for",
                         {"id": 3, "body": "x", EPOCH_COLUMN: 1})
        assert status >= 400
        assert _count(engine, index) == 1
        assert fence.prepare(3).epoch == 3  # repeated: returns the matching state
        with pytest.raises(sde.MigrationRefused, match="cannot change an existing write epoch"):
            fence.prepare(4)


def test_a_barrier_waits_for_a_bulk_in_flight_and_admits_nothing_until_released() -> None:
    with fresh_index() as (engine, index):
        fence = engine.write_fence(index, project_id=PROJECT)
        fence.prepare(3)
        lines = []
        for n in range(120_000):
            lines.append(json.dumps({"index": {"_id": str(n)}}))
            lines.append(json.dumps({"id": n, "body": "x" * 30, EPOCH_COLUMN: 3}))
        payload = ("\n".join(lines) + "\n").encode()
        writer = ElasticsearchEngine(DSN or "")
        writer.connect()
        done: dict[str, Any] = {}

        def bulk() -> None:
            done["status"], done["body"] = _raw(writer, "POST", f"/{index}/_bulk", ndjson=payload)

        thread = threading.Thread(target=bulk)
        thread.start()
        time.sleep(1.0)
        hold = uuid4().hex
        state = fence.freeze(hold)
        at_barrier = _count(engine, index)
        thread.join(timeout=120)
        writer.close()
        assert state.closed and hold in state.holds
        # Whatever the bulk wrote landed before the barrier returned: nothing lands after it.
        time.sleep(1.0)
        assert _count(engine, index) == at_barrier
        assert _put(engine, index, "late", {"id": -1, "body": "x", EPOCH_COLUMN: 3}) >= 400
        assert _count(engine, index) == at_barrier

        state = fence.advance(4)
        assert (state.lower_epoch, state.upper_epoch, state.closed) == (4, 4, True)
        state = fence.release(hold)
        assert (state.closed, state.holds, state.retired) == (False, (), (hold,))
        assert _put(engine, index, "old", {"id": -2, "body": "x", EPOCH_COLUMN: 3}) >= 400
        assert _put(engine, index, "new", {"id": -3, "body": "x", EPOCH_COLUMN: 4}) in (200, 201)
        with pytest.raises(sde.MigrationRefused, match="cannot be reused"):
            fence.freeze(hold)


def test_a_block_that_outlived_its_hold_reads_as_an_interrupted_setup_and_resumes() -> None:
    with fresh_index() as (engine, index):
        fence = engine.write_fence(index, project_id=PROJECT)
        fence.prepare(5)
        # As if a release stopped between dropping its hold and lifting the block.
        assert _raw(engine, "PUT", f"/{index}/_block/write")[0] == 200
        state = fence.state()
        assert state.closed and state.holds == ("setup",)
        state = fence.resume_prepare(5)
        assert (state.epoch, state.closed, state.holds) == (5, False, ())
        assert _put(engine, index, "a", {"id": 1, "body": "x", EPOCH_COLUMN: 5}) in (200, 201)


def test_a_block_or_a_pipeline_that_is_not_this_librarys_is_refused_not_taken_over() -> None:
    with fresh_index() as (engine, index):
        assert _raw(engine, "PUT", f"/{index}/_block/write")[0] == 200
        with pytest.raises(sde.MigrationRefused, match="somebody else blocked it"):
            engine.write_fence(index, project_id=PROJECT).state()
        _raw(engine, "PUT", f"/{index}/_settings", {"index.blocks.write": False})
        fence = engine.write_fence(index, project_id=PROJECT)
        fence.prepare(2)
        _, body = _raw(engine, "GET", f"/_ingest/pipeline/{fence_pipeline(index)}")
        pipeline = {key: value for key, value in body[fence_pipeline(index)].items()
                    if not key.endswith("_date_millis")}
        pipeline["processors"][0]["fail"]["if"] = "false"
        assert _raw(engine, "PUT", f"/_ingest/pipeline/{fence_pipeline(index)}", pipeline)[0] == 200
        with pytest.raises(sde.MigrationRefused, match="changed outside this library"):
            fence.state()


def _key_dsn(engine: ElasticsearchEngine, index: str, cluster: list[str],
             privileges: list[str]) -> tuple[str, str]:
    assert DSN is not None
    status, key = _raw(engine, "POST", "/_security/api_key", {
        "name": "sde-fence-test-" + uuid4().hex[:8],
        "role_descriptors": {"probe": {
            "cluster": cluster,
            "indices": [{"names": [index, "sde_map_state"], "privileges": privileges}],
        }},
    })
    assert status == 200, key
    parts = urllib.parse.urlsplit(DSN)
    query = urllib.parse.parse_qs(parts.query)
    netloc = parts.netloc.split("@", 1)[-1]
    options = {"ca_cert": query["ca_cert"][0], "api_key": key["encoded"]} if "ca_cert" in query \
        else {"api_key": key["encoded"]}
    return (f"{parts.scheme}://{netloc}/?{urllib.parse.urlencode(options)}", key["id"])


def test_a_runtime_key_reads_the_fence_and_cannot_change_it() -> None:
    with fresh_index() as (engine, index):
        runtime_dsn, runtime_id = _key_dsn(
            engine, index, ["read_pipeline"], ["write", "read", "view_index_metadata",
                                               "create_index"]
        )
        provisioning_dsn, provisioning_id = _key_dsn(
            engine, index, ["manage_pipeline"], ["manage", "write", "read"]
        )
        runtime = ElasticsearchEngine(runtime_dsn)
        provisioner = ElasticsearchEngine(provisioning_dsn)
        runtime.connect()
        provisioner.connect()
        try:
            with pytest.raises(sde.EngineError):
                runtime.write_fence(index, project_id=PROJECT).prepare(1)
            assert provisioner.write_fence(index, project_id=PROJECT).prepare(1).epoch == 1
            assert runtime.write_fence(index, project_id=PROJECT).state().epoch == 1
            with pytest.raises(sde.EngineError):
                runtime.write_fence(index, project_id=PROJECT).freeze(uuid4().hex)
        finally:
            runtime.close()
            provisioner.close()
            _raw(engine, "DELETE", "/_security/api_key", {"ids": [runtime_id, provisioning_id]})


def _map(model: sde.LogicalModel, index: str, **group: Any) -> dict[str, Any]:
    return {
        "contract": 6, "model_version": model.version, "map_version": 1, "project_id": PROJECT,
        "groups": {"Note": {**group, "source": {"id": "es", "engine": "es", "layout": {
            "tables": {"Note": index}, "columns": {"Note": LAYOUT_COLUMNS},
        }}}},
    }


def test_a_session_stamps_its_generation_and_reads_the_rows_without_it() -> None:
    sde.clear_registry()

    @sde.entity
    class Note:
        id: int
        body: str

    model = sde.build_model(Note)
    with fresh_index() as (engine, index):
        placement = sde.load_map(_map(model, index, write_epoch=7), model=model)
        sde.prepare_schema(model, placement, {"es": engine}, project_id=PROJECT)
        session = sde.Session(model, placement, {"es": engine}, project_id=PROJECT)
        session.save("Note", {"id": 1, "body": "hello"})
        assert session.get("Note", {"id": 1}) == {"id": 1, "body": "hello"}
        _, stored = _raw(engine, "GET", f"/{index}/_search", {"query": {"match_all": {}}})
        assert stored["hits"]["hits"][0]["_source"][EPOCH_COLUMN] == 7
        assert _put(engine, index, "raw", {"id": 2, "body": "x"}) >= 400


def test_a_group_on_elasticsearch_without_a_generation_is_refused_now_that_it_can_fence() -> None:
    """Requirement 2.2: a contract-6 map that left the generation out for Elasticsearch, as maps
    issued before this change did, is refused like one for any engine with write fences."""
    sde.clear_registry()

    @sde.entity
    class Note:
        id: int
        body: str

    model = sde.build_model(Note)
    with fresh_index() as (engine, index):
        placement = sde.load_map(_map(model, index), model=model)
        with pytest.raises(sde.MigrationRefused):
            sde.Session(model, placement, {"es": engine}, project_id=PROJECT)
