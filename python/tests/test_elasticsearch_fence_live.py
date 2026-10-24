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


def test_a_barrier_makes_every_write_before_it_visible_to_a_search() -> None:
    """The block waits for the writes on the shards, but a write may be indexed and not yet
    searchable when it returns, and a copy and a comparison read by searching. With the index's own
    refresh off, only the barrier's refresh can show this document."""
    with fresh_index() as (engine, index):
        assert _raw(engine, "PUT", f"/{index}/_settings", {"index.refresh_interval": "-1"})[0] \
            == 200
        fence = engine.write_fence(index, project_id=PROJECT)
        fence.prepare(3)
        assert _put_unrefreshed(engine, index, "a", {"id": 1, "body": "x", EPOCH_COLUMN: 3}) \
            in (200, 201)
        assert engine.count(index) == 0
        fence.freeze(uuid4().hex)
        assert engine.count(index) == 1


def _put_unrefreshed(engine: ElasticsearchEngine, index: str, doc_id: str,
                     document: dict[str, Any]) -> int:
    status, _ = _raw(engine, "PUT", f"/{index}/_doc/{doc_id}", document)
    return status


def test_a_block_that_outlived_its_hold_reads_as_an_interrupted_setup_and_resumes() -> None:
    with fresh_index() as (engine, index):
        fence = engine.write_fence(index, project_id=PROJECT)
        fence.prepare(5)
        # As if a setup stopped between blocking the index and recording its hold. A release no
        # longer leaves this: it lifts the block before it drops the last hold.
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
            engine, index, ["read_pipeline"], ["index", "read", "view_index_metadata",
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


def opens_the_index(method: str, path: str, body: Any) -> bool:
    """A request of the last hold's release: the pipeline written without a hold, or the block
    lifted."""
    if method != "PUT" or not isinstance(body, dict):
        return False
    if path.startswith("/_ingest/pipeline/"):
        constraints = body.get("_meta", {}).get("sde", {}).get("constraints", {})
        return not any(name.startswith("__sde_f_hold_") for name in constraints)
    return body.get("index.blocks.write") is False


def test_a_release_interrupted_between_its_two_requests_is_finished_by_a_retry() -> None:
    """The last hold's release makes two requests. The first is made and its response lost: the
    process is gone before the second. The block goes first now, so what is left is the hold - still
    refusing every write a runtime key can make - and a retry removes it. When the pipeline went
    first, what was left was a block with no hold to say whose it was, which no retry lifted: the
    index in force stayed closed to writes after a published cutover (second review)."""
    with fresh_index() as (engine, index):
        fence = engine.write_fence(index, project_id=PROJECT)
        fence.prepare(3)
        hold = uuid4().hex
        fence.freeze(hold)
        backend = fence._backend  # type: ignore[attr-defined]
        call = backend._call
        lost: list[str] = []

        def made_then_lost(method: str, path: str, body: Any = None) -> tuple[int, Any]:
            answer = call(method, path, body)
            if not lost and opens_the_index(method, path, body):
                lost.append(path)
                raise sde.EngineError("the response was lost")
            return answer  # type: ignore[no-any-return]

        backend._call = made_then_lost
        with pytest.raises(sde.EngineError, match="lost"):
            fence.release(hold)
        assert lost, "the release never made the request that opens the index"
        backend._call = call
        state = fence.state()
        assert state.closed and state.holds == (hold,), "the hold is there to be found"
        assert _put(engine, index, "late", {"id": 1, "body": "x", EPOCH_COLUMN: 3}) >= 400
        state = fence.release(hold)
        assert (state.closed, state.holds, state.retired) == (False, (), (hold,))
        assert _put(engine, index, "open", {"id": 2, "body": "x", EPOCH_COLUMN: 3}) in (200, 201)


def _write_block(engine: ElasticsearchEngine, index: str) -> str:
    status, body = _raw(engine, "GET", f"/{index}/_settings")
    assert status == 200, body
    return str(body[index]["settings"]["index"].get("blocks", {}).get("write", "false"))


def test_the_block_stays_while_another_hold_does() -> None:
    """Two holds, released one at a time: the block goes with the last of them and never earlier,
    so a write that passed an older pipeline on its way is refused at the shard while any hold is
    there."""
    with fresh_index() as (engine, index):
        fence = engine.write_fence(index, project_id=PROJECT)
        fence.prepare(3)
        first, second = uuid4().hex, uuid4().hex
        fence.freeze(first)
        fence.freeze(second)
        state = fence.release(first)
        assert (state.closed, state.holds) == (True, (second,))
        assert _write_block(engine, index) == "true"
        state = fence.release(second)
        assert (state.closed, state.holds) == (False, ())
        assert _write_block(engine, index) == "false"
        assert _put(engine, index, "open", {"id": 1, "body": "x", EPOCH_COLUMN: 3}) in (200, 201)


def test_a_hold_refuses_an_index_and_an_update_and_no_delete_without_its_block() -> None:
    """Why a runtime key holds `index` and not `write` (runtime-roles.md), and what is left between
    the two requests of the last hold's release: with the hold in the pipeline and no block, an
    index and an update are refused, because both run the final pipeline, and a delete is not,
    because it runs none (measured on 9.5.5)."""
    with fresh_index() as (engine, index):
        fence = engine.write_fence(index, project_id=PROJECT)
        fence.prepare(3)
        assert _put(engine, index, "a", {"id": 1, "body": "x", EPOCH_COLUMN: 3}) in (200, 201)
        fence.freeze(uuid4().hex)
        assert _raw(engine, "PUT", f"/{index}/_settings", {"index.blocks.write": False})[0] == 200
        assert _write_block(engine, index) == "false" and fence.state().closed
        assert _put(engine, index, "b", {"id": 2, "body": "x", EPOCH_COLUMN: 3}) >= 400
        updated, _ = _raw(engine, "POST", f"/{index}/_update/a?refresh=wait_for",
                          {"doc": {"body": "y"}})
        assert updated >= 400
        deleted, _ = _raw(engine, "DELETE", f"/{index}/_doc/a?refresh=wait_for")
        assert deleted == 200
        assert _count(engine, index) == 0


def test_a_pipeline_that_is_not_the_indexs_final_one_fences_nothing_and_reads_so() -> None:
    """Unset ``index.final_pipeline`` and nothing the pipeline holds is enforced. The fence reads
    as an interrupted setup - closed to this library - and resume_prepare installs it again."""
    with fresh_index() as (engine, index):
        fence = engine.write_fence(index, project_id=PROJECT)
        fence.prepare(3)
        unset = {"index.final_pipeline": "_none"}
        assert _raw(engine, "PUT", f"/{index}/_settings", unset)[0] == 200
        # The engine admits a stale generation now, and the fence does not read as holding.
        assert _put(engine, index, "stale", {"id": 1, "body": "x", EPOCH_COLUMN: 2}) in (200, 201)
        state = fence.state()
        assert state.closed and state.holds == ("setup",)
        state = fence.resume_prepare(3)
        assert (state.complete, state.epoch, state.closed) == (True, 3, False)
        assert _put(engine, index, "stale2", {"id": 2, "body": "x", EPOCH_COLUMN: 2}) >= 400


def test_a_pipeline_level_on_failure_is_refused() -> None:
    """``on_failure`` on the pipeline catches the fence's own refusal and indexes the document."""
    with fresh_index() as (engine, index):
        fence = engine.write_fence(index, project_id=PROJECT)
        fence.prepare(3)
        name = fence_pipeline(index)
        status, current = _raw(engine, "GET", f"/_ingest/pipeline/{name}")
        assert status == 200
        document = {key: value for key, value in current[name].items()
                    if key in ("description", "version", "_meta", "processors")}
        document["on_failure"] = [{"set": {"field": "body", "value": "admitted"}}]
        assert _raw(engine, "PUT", f"/_ingest/pipeline/{name}", document)[0] == 200
        with pytest.raises(sde.MigrationRefused, match="on_failure"):
            fence.state()


def test_a_pipeline_another_executor_moved_is_refused_by_name() -> None:
    """Elasticsearch answers a conditional pipeline write that lost the race with a 400 reading
    "version conflict" (measured on 9.5.5): refused as another executor's, not a failed request."""
    with fresh_index() as (engine, index):
        fence = engine.write_fence(index, project_id=PROJECT)
        fence.prepare(3)
        backend = fence._backend  # type: ignore[attr-defined]
        pipeline = backend._pipeline

        def stale(table: str) -> Any:
            document, version = pipeline(table)
            return document, (version or 0) - 1

        backend._pipeline = stale
        with pytest.raises(sde.MigrationRefused, match="another executor"):
            fence.freeze(uuid4().hex)
