"""The local operator moves a group onto Elasticsearch and off it (local-cutover.md, staging.md).

Set SDE_ELASTICSEARCH_DSN and SDE_POSTGRES_DSN to run. PostgreSQL gets a schema of its own and a
restricted runtime login, as in every operator test; Elasticsearch gets the cluster's administrator
as the operator and an API key as the runtime. An index is cluster-wide, and so is `sde_map_state`:
map versions start from the clock, so a later test's maps are newer than an earlier one's.
"""

from __future__ import annotations

import base64
import os
import time
import urllib.parse
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from test_elasticsearch_fence_live import opens_the_index
from test_runtime_privileges_live import runtime_roles
from test_staging_operator_live import cutover

import sde
from sde.engines._write_fences import fence_pipeline
from sde.engines.elasticsearch import ElasticsearchEngine

PROJECT = "1" * 32
DSN = os.environ.get("SDE_ELASTICSEARCH_DSN")
pytestmark = pytest.mark.skipif(
    DSN is None or not os.environ.get("SDE_POSTGRES_DSN"),
    reason="set SDE_ELASTICSEARCH_DSN and SDE_POSTGRES_DSN to run",
)
RUNTIME = ("read", "index", "view_index_metadata")
"""What the runtime key holds on its indexes: what docs/engine-connections.md asks for."""


class Crash(BaseException):
    """A process that stops at a step: nothing of the operator's catches it, as nothing catches a
    SIGKILL."""


@dataclass
class Keys:
    operator: ElasticsearchEngine
    runtime: ElasticsearchEngine


def _key(admin: ElasticsearchEngine, names: Sequence[str], privileges: Sequence[str],
         cluster: Sequence[str]) -> tuple[str, str]:
    assert DSN is not None
    status, key = admin._request("POST", "/_security/api_key", {
        "name": "sde-operator-test-" + uuid4().hex[:8],
        "role_descriptors": {"runtime": {
            "cluster": list(cluster),
            "indices": [{"names": list(names), "privileges": list(privileges)}],
        }},
    })
    assert status == 200, key
    parts = urllib.parse.urlsplit(DSN)
    query = urllib.parse.parse_qs(parts.query)
    options = {"api_key": key["encoded"]}
    if "ca_cert" in query:
        options = {"ca_cert": query["ca_cert"][0], **options}
    netloc = parts.netloc.split("@", 1)[-1]
    return f"{parts.scheme}://{netloc}/?{urllib.parse.urlencode(options)}", key["id"]


@contextmanager
def elasticsearch_keys(names: Sequence[str], privileges: Sequence[str] = RUNTIME,
                       cluster: Sequence[str] = ("read_pipeline",)) -> Iterator[Keys]:
    """The cluster's administrator as the operator, and an API key on ``names`` as the runtime."""
    assert DSN is not None
    admin = ElasticsearchEngine(DSN)
    admin.connect()
    runtime_dsn, key_id = _key(admin, names, privileges, cluster)
    runtime = ElasticsearchEngine(runtime_dsn)
    runtime.connect()
    try:
        yield Keys(admin, runtime)
    finally:
        runtime.close()
        admin._request("DELETE", "/_security/api_key", {"ids": [key_id]})
        admin.close()


@contextmanager
def probe_key(names: Sequence[str], privileges: Sequence[str],
              cluster: Sequence[str]) -> Iterator[tuple[ElasticsearchEngine, str]]:
    """Another runtime key, for the operator to qualify: the application keeps its own."""
    assert DSN is not None
    admin = ElasticsearchEngine(DSN)
    admin.connect()
    dsn, key_id = _key(admin, names, privileges, cluster)
    probe = ElasticsearchEngine(dsn)
    probe.connect()
    try:
        yield probe, dsn
    finally:
        probe.close()
        admin._request("DELETE", "/_security/api_key", {"ids": [key_id]})
        admin.close()


def _remove(admin: ElasticsearchEngine, *indexes: str) -> None:
    for index in indexes:
        admin._request("DELETE", f"/{index}")
        admin._request("DELETE", f"/_ingest/pipeline/{fence_pipeline(index)}")


def _layout(engine: str, table: str) -> dict[str, Any]:
    columns = (
        {"id": "bigint", "value": "integer", "note": "text"}
        if engine == "postgres"
        else {"id": "long", "value": "integer", "note": "keyword"}
    )
    return {"tables": {"Event": table}, "columns": {"Event": columns}}


@dataclass
class Move:
    operator: sde.LocalCutover
    stage: Any
    old: sde.Session
    pg: Any
    es: Keys
    model: sde.LogicalModel
    public: bytes
    signed: Any
    source: str
    target: str
    source_table: str
    probe: ElasticsearchEngine
    """The runtime key the operator qualifies: the application's own may hold more or less."""
    probe_dsn: str

    def runtime(self) -> dict[str, Any]:
        return {"postgres": self.pg.runtime, "elasticsearch": self.es.runtime}

    def session(self) -> sde.Session:
        return sde.Session(self.model, self.operator.active_map(), self.runtime(),
                           project_id=PROJECT)

    @property
    def copy(self) -> str:
        return str(self.stage.prepared.groups["Event"].derived[0].layout.tables["Event"])

    def fresh_operator(self) -> sde.LocalCutover:
        """The same project directory with new connections, as a restarted process has it."""
        return sde.LocalCutover(
            self.operator.store.root, model=self.model, project_id=PROJECT,
            public_key=self.public,
            operators={"postgres": self.pg.operator, "elasticsearch": self.es.operator},
            runtime={"postgres": [self.pg.runtime], "elasticsearch": [self.probe]},
        )


@contextmanager
def moving(source: str, root: Path, *, target: str | None = None,
           privileges: Sequence[str] = RUNTIME, cluster: Sequence[str] = ("read_pipeline",),
           covers_stage: bool = True) -> Iterator[Move]:
    """A source-only map with one row, an old session on it, an operator, and a staging packet.

    ``target`` defaults to the other engine. ``privileges``, ``cluster`` and ``covers_stage`` are
    the operator's runtime probe's: the key the operator qualifies. ``covers_stage`` gives it the
    pattern `sde_m_*`, which a staged copy's fresh name needs, because nothing grants it later.
    The application's key, which the sessions use, holds what the documentation asks for.
    """
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    target = target or ("elasticsearch" if source == "postgres" else "postgres")
    protocol = 2 if target == source else 1
    base = time.time_ns() // 1_000_000
    index = "sde_op_" + uuid4().hex[:12]
    names = [index, "sde_map_state", "sde_m_*"]
    probed = [index, "sde_map_state", *(["sde_m_*"] if covers_stage else [])]
    stage_id = uuid4().hex
    with (
        runtime_roles("postgres") as pg,
        elasticsearch_keys(names) as es,
        probe_key(probed, privileges, cluster) as (probe, probe_dsn),
    ):
        try:
            sde.clear_registry()

            @sde.entity
            class Event:
                id: int
                value: sde.Int32
                note: str

            model = sde.build_model(Event)
            key = Ed25519PrivateKey.generate()
            public = key.public_key().public_bytes_raw()

            def signed(raw: dict[str, Any]) -> dict[str, Any]:
                document = deepcopy(raw)
                document.pop("signature", None)
                document["signature"] = {
                    "alg": "ed25519",
                    "value": base64.b64encode(key.sign(sde.canonical_bytes(document))).decode(),
                }
                return document

            source_table = index if source == "elasticsearch" else "initial_events"
            current = signed({
                "contract": 4, "project_id": PROJECT, "model_version": model.version,
                "map_version": base,
                "groups": {"Event": {"write_epoch": 1, "source": {
                    "id": "source", "engine": source, "layout": _layout(source, source_table)}}},
            })
            parsed = sde.load_map(current, model=model, public_key=public)
            operators = {"postgres": pg.operator, "elasticsearch": es.operator}
            runtime = {"postgres": pg.runtime, "elasticsearch": es.runtime}
            sde.prepare_schema(model, parsed, operators, project_id=PROJECT)
            if source == "postgres":
                pg.grant("initial_events")
            pg.grant("sde_map_state")
            old = sde.Session(model, parsed, runtime, project_id=PROJECT)
            old.save("Event", {"id": 1, "value": 11, "note": "one"})
            operator = sde.LocalCutover(
                root, model=model, project_id=PROJECT, public_key=public, operators=operators,
                runtime={"postgres": [pg.runtime], "elasticsearch": [probe]},
            )
            operator.enroll(current)
            prepared = deepcopy(current)
            prepared["map_version"] = base + 1
            copy = {"id": "copy", "engine": target,
                    "layout": _layout(target, sde.staging_table_name(stage_id, 1)),
                    "lag_budget_ms": 30000}
            prepared["groups"]["Event"].update(derived=[copy], also_write=["copy"])
            stage = sde.load_staging_plan(
                signed({"kind": "sde-stage", "protocol": protocol, "stage_id": stage_id,
                        "project_id": PROJECT, "group": "Event", "current": current,
                        "prepared": signed(prepared)}),
                model=model, project_id=PROJECT, public_key=public,
            )
            yield Move(operator, stage, old, pg, es, model, public, signed, source, target,
                       source_table, probe, probe_dsn)
        finally:
            _remove(es.operator, index, sde.staging_table_name(stage_id, 1))


def _blocks(es: Keys, index: str) -> dict[str, str]:
    """The blocks in force on ``index``: a lifted one stays in the settings as ``false``."""
    status, body = es.operator._request("GET", f"/{index}/_settings")
    assert status == 200, body
    blocks = body[index]["settings"]["index"].get("blocks", {})
    return {name: value for name, value in blocks.items() if value == "true"}


def _counted(engine: ElasticsearchEngine, index: str) -> tuple[int, str | None]:
    status, body = engine._request("POST", f"/{index}/_count", {"query": {"match_all": {}}})
    error = body.get("error", {}).get("type") if isinstance(body, dict) else None
    return status, error


ROWS = {1: {"id": 1, "value": 11, "note": "one"}, 2: {"id": 2, "value": 22, "note": "two"},
        3: {"id": 3, "value": 33, "note": "three"}}


def test_a_group_moves_onto_elasticsearch_and_the_copy_is_repaired_from_the_frozen_source(
    tmp_path: Path,
) -> None:
    """A row the old map wrote to PostgreSQL alone reaches Elasticsearch by the repair; the
    staged index carries its creation marker and the generation, and the old map is refused."""
    with moving("postgres", tmp_path) as move:
        result = move.operator.stage(move.stage).as_record()
        assert result["outcome"] == "prepared"
        status, body = move.es.operator._request("GET", f"/{move.copy}/_mapping")
        assert status == 200
        marker = body[move.copy]["mappings"]["_meta"]["sde"]["stage"]
        assert marker.startswith(f"sde.stage1.{PROJECT}.{move.stage.stage_id}.")
        assert move.es.operator.count(move.copy) == 0
        move.old.save("Event", ROWS[2])  # the old map: PostgreSQL only, a fan-out never made
        move.session().save("Event", ROWS[3])  # the prepared map: fanned out to Elasticsearch
        assert move.es.operator.get(move.copy, {"id": 3}) == ROWS[3]
        assert move.es.operator.get(move.copy, {"id": 2}) is None
        # A document only the copy holds - a fan-out whose source write never committed - is
        # removed by the repair, which empties the copy before it copies the frozen source.
        identity, stray = move.es.operator._index(move.copy).document(
            {"id": 9, "value": 99, "note": "stray", sde.WRITE_EPOCH_COLUMN: 1})
        assert move.es.operator._request(
            "PUT", f"/{move.copy}/_doc/{identity}?refresh=true", stray)[0] == 201

        final = move.operator.execute(
            cutover(move.stage, move.signed, move.model, move.public)).as_record()
        assert (final["outcome"], final["reason"]) == ("success", "matched")
        assert final["verification"]["matched"] is True
        moved = move.session()
        for key, row in ROWS.items():
            assert moved.get("Event", {"id": key}) == row
        assert moved.get("Event", {"id": 9}) is None
        moved.save("Event", {"id": 4, "value": 44, "note": "four"})
        assert move.es.operator.get(move.copy, {"id": 4}) == {"id": 4, "value": 44,
                                                             "note": "four"}
        state = move.es.operator.write_fence(move.copy, project_id=PROJECT).state()
        assert (state.epoch, state.closed) == (3, False)
        assert _blocks(move.es, move.copy) == {}
        with pytest.raises(sde.EngineError):
            move.old.get("Event", {"id": 1})


def test_a_group_moves_off_elasticsearch_and_the_index_it_left_is_closed_to_everyone(
    tmp_path: Path,
) -> None:
    """Requirement 3.2: the index a group left refuses reads and writes for good - the runtime's,
    an unknown principal's, and the administrator's own - and keeps the holds that fence it."""
    with moving("elasticsearch", tmp_path) as move:
        assert move.operator.stage(move.stage).as_record()["outcome"] == "prepared"
        move.old.save("Event", ROWS[2])
        move.session().save("Event", ROWS[3])
        final = move.operator.execute(
            cutover(move.stage, move.signed, move.model, move.public)).as_record()
        assert (final["outcome"], final["reason"]) == ("success", "matched")
        moved = move.session()
        for key, row in ROWS.items():
            assert moved.get("Event", {"id": key}) == row

        left = move.source_table
        assert _blocks(move.es, left) == {"read": "true", "write": "true"}
        for engine in (move.es.runtime, move.es.operator):
            assert _counted(engine, left) == (403, "cluster_block_exception")
        status, _ = move.es.operator._request("PUT", f"/{left}/_doc/x?refresh=true",
                                              {"id": 9, "value": 9, "note": "x",
                                               sde.WRITE_EPOCH_COLUMN: 3})
        assert status >= 400
        state = move.es.operator.write_fence(left, project_id=PROJECT).state()
        assert state.closed and len(state.holds) == 2
        with pytest.raises(sde.EngineError):
            move.old.get("Event", {"id": 1})
        with pytest.raises(sde.EngineError):
            move.old.save("Event", {"id": 5, "value": 55, "note": "five"})


def test_a_read_block_the_engine_acknowledged_and_did_not_apply_is_noticed(tmp_path: Path) -> None:
    """The retirement is seen from each runtime key's side. An engine that acknowledges a block it
    did not apply leaves the decision durable and the receipt unwritten; a resume blocks it."""
    with moving("elasticsearch", tmp_path) as move:
        move.operator.stage(move.stage)
        es = move.es.operator
        actual = es._request

        def acknowledged(method: str, path: str, body: Any = None, **options: Any) -> Any:
            if method == "PUT" and path.endswith("/_block/read"):
                return 200, {"acknowledged": True, "shards_acknowledged": True}
            return actual(method, path, body, **options)

        es._request = acknowledged  # type: ignore[method-assign]
        try:
            with pytest.raises(sde.CutoverRecoveryRequired):
                move.operator.execute(cutover(move.stage, move.signed, move.model, move.public))
        finally:
            es._request = actual  # type: ignore[method-assign]
        assert move.operator.status()["decision"] == "success"
        assert _blocks(move.es, move.source_table) == {"write": "true"}
        receipt = move.fresh_operator().resume().as_record()
        assert (receipt["outcome"], receipt["recovered"]) == ("success", True)
        assert _blocks(move.es, move.source_table) == {"read": "true", "write": "true"}


@pytest.mark.parametrize("checkpoint", ["repair:done", "verify_frozen:done"])
def test_a_crash_before_the_decision_aborts_and_retires_the_elasticsearch_copy(
    checkpoint: str, tmp_path: Path,
) -> None:
    """Recovery before a decision aborts: PostgreSQL stays authoritative at the next generation,
    and the copy on Elasticsearch is closed for good, as a retired source is."""
    with moving("postgres", tmp_path) as move:
        move.operator.stage(move.stage)
        move.old.save("Event", ROWS[2])

        def crash(step: str) -> None:
            if step == checkpoint:
                raise Crash(step)

        move.operator._after_step = crash
        with pytest.raises(Crash):
            move.operator.execute(cutover(move.stage, move.signed, move.model, move.public))
        receipt = move.fresh_operator().resume().as_record()
        assert (receipt["outcome"], receipt["reason"]) == ("abort", "recovered_before_decision")
        active = move.session()
        assert active.get("Event", {"id": 2}) == ROWS[2]
        active.save("Event", ROWS[3])
        assert active.get("Event", {"id": 3}) == ROWS[3]
        assert _blocks(move.es, move.copy) == {"read": "true", "write": "true"}
        assert _counted(move.es.runtime, move.copy) == (403, "cluster_block_exception")


@pytest.mark.parametrize("checkpoint", ["decision:success", "confirm_source_denied:done",
                                        "publish_map:done"])
def test_a_crash_after_the_decision_completes_the_move_off_elasticsearch(
    checkpoint: str, tmp_path: Path,
) -> None:
    with moving("elasticsearch", tmp_path) as move:
        move.operator.stage(move.stage)

        def crash(step: str) -> None:
            if step == checkpoint:
                raise Crash(step)

        move.operator._after_step = crash
        with pytest.raises(Crash):
            move.operator.execute(cutover(move.stage, move.signed, move.model, move.public))
        receipt = move.fresh_operator().resume().as_record()
        assert (receipt["outcome"], receipt["recovered"]) == ("success", True)
        assert move.session().get("Event", {"id": 1}) == ROWS[1]
        assert _blocks(move.es, move.source_table) == {"read": "true", "write": "true"}


def test_a_move_lost_while_it_opens_the_index_in_force_is_finished_by_a_resume(
    tmp_path: Path,
) -> None:
    """The move's last step releases the final hold on the index in force: the block lifted, then
    the pipeline written without the hold. The first request is made, its response lost, and the
    process is gone. What is left is the hold, still refusing every write a runtime key can make,
    and a resume finishes the release. In the old order the pipeline went first, and what was left
    was a block with no hold to say whose it was: the resume refused, and the published map's index
    stayed closed to its writes (second review)."""
    with moving("postgres", tmp_path) as move:
        move.operator.stage(move.stage)
        es = move.es.operator
        actual = es._request
        releasing: list[tuple[dict[str, str], tuple[str, ...]]] = []
        lost: list[str] = []

        def made_then_lost(method: str, path: str, body: Any = None, **options: Any) -> Any:
            answer = actual(method, path, body, **options)
            if releasing and not lost and opens_the_index(method, path, body):
                lost.append(path)
                raise Crash("the response was lost")
            return answer

        def step(name: str) -> None:
            if name == "release_runtime:intent":
                # The premise: a block to lift, or the release is one request and nothing between;
                # and the final hold, the one a resume must find.
                fence = es.write_fence(move.copy, project_id=PROJECT)
                releasing.append((_blocks(move.es, move.copy), fence.state().holds))

        move.operator._after_step = step
        es._request = made_then_lost  # type: ignore[method-assign]
        try:
            with pytest.raises(Crash):
                move.operator.execute(cutover(move.stage, move.signed, move.model, move.public))
        finally:
            es._request = actual  # type: ignore[method-assign]
        [(blocks, final)] = releasing
        assert blocks == {"write": "true"} and len(final) == 1
        assert lost, "the release never made the request that opens the index"
        state = es.write_fence(move.copy, project_id=PROJECT).state()
        assert (state.closed, state.holds) == (True, final), "the hold is there to be found"
        receipt = move.fresh_operator().resume().as_record()
        assert (receipt["outcome"], receipt["recovered"]) == ("success", True)
        state = es.write_fence(move.copy, project_id=PROJECT).state()
        assert (state.epoch, state.closed, state.holds) == (3, False, ())
        assert _blocks(move.es, move.copy) == {}
        moved = move.session()
        moved.save("Event", ROWS[3])
        assert moved.get("Event", {"id": 3}) == ROWS[3]
        assert moved.get("Event", {"id": 1}) == ROWS[1]


def test_a_mismatch_aborts_and_the_source_on_elasticsearch_writes_again(tmp_path: Path) -> None:
    with moving("elasticsearch", tmp_path) as move:
        move.operator.stage(move.stage)
        move.old.save("Event", ROWS[2])
        actual = move.pg.operator.copy_in

        def omit_one(table: str, rows: Any) -> None:
            actual(table, [row for row in rows if row["id"] != 2])

        move.pg.operator.copy_in = omit_one
        try:
            receipt = move.operator.execute(
                cutover(move.stage, move.signed, move.model, move.public)).as_record()
        finally:
            move.pg.operator.copy_in = actual
        assert (receipt["outcome"], receipt["reason"]) == ("abort", "verification_mismatch")
        active = move.session()
        assert active.get("Event", {"id": 2}) == ROWS[2]
        active.save("Event", ROWS[3])
        assert active.get("Event", {"id": 3}) == ROWS[3]
        assert _blocks(move.es, move.source_table) == {}
        with pytest.raises(sde.EngineError):
            move.old.save("Event", {"id": 6, "value": 66, "note": "six"})


@pytest.mark.parametrize("checkpoint", ["stage_create_Event:done", "stage_generation_Event:done",
                                        "stage_qualify:done"])
def test_an_unfinished_staging_onto_elasticsearch_is_abandoned_with_its_index(
    checkpoint: str, tmp_path: Path,
) -> None:
    with moving("postgres", tmp_path) as move:

        def crash(step: str) -> None:
            if step == checkpoint:
                raise Crash(step)

        move.operator._after_step = crash
        with pytest.raises(Crash):
            move.operator.stage(move.stage)
        move.operator._after_step = lambda _step: None
        receipt = move.operator.abandon().as_record()
        assert receipt["outcome"] == "abandoned"
        (row,) = receipt["tables"]
        assert row["identity"]["name"] == move.copy
        assert move.es.operator._request("GET", f"/{move.copy}/_mapping")[0] == 404
        assert move.es.operator._request(
            "GET", f"/_ingest/pipeline/{fence_pipeline(move.copy)}")[0] == 404
        assert move.operator.active_map().fingerprint == move.stage.current.fingerprint
        move.old.save("Event", ROWS[2])
        assert move.old.get("Event", {"id": 2}) == ROWS[2]


def test_an_index_of_the_staging_name_without_its_marker_is_left_alone(tmp_path: Path) -> None:
    with moving("postgres", tmp_path) as move:
        move.es.operator.ensure_schema(
            sde.PhysicalLayout(tables={"Event": move.copy},
                               columns={"Event": _layout("elasticsearch", move.copy)["columns"]
                                        ["Event"]}),
            keys={"Event": ["id"]})
        with pytest.raises(sde.MigrationRefused, match="already exists"):
            move.operator.stage(move.stage)
        assert move.es.operator._request("GET", f"/{move.copy}/_mapping")[0] == 200


@pytest.mark.parametrize(("privileges", "cluster", "refusal"), [
    ([*RUNTIME, "manage"], ["read_pipeline"], "holds manage on"),
    ([*RUNTIME, "delete_index"], ["read_pipeline"], "holds delete_index on"),
    # A delete runs no ingest pipeline: a key that may delete documents goes past the fence.
    ([*RUNTIME, "delete"], ["read_pipeline"], "holds delete on"),
    # What runtime-roles.md said before the second review: named by the privilege given.
    (["read", "write", "view_index_metadata"], ["read_pipeline"], "holds write on"),
    (RUNTIME, ["read_pipeline", "manage_pipeline"], "cluster privilege manage_pipeline"),
    (RUNTIME, [], "needs the cluster privilege read_pipeline"),
])
def test_a_runtime_key_that_could_lift_a_block_or_a_fence_is_refused(
    privileges: Sequence[str], cluster: Sequence[str], refusal: str, tmp_path: Path,
) -> None:
    with moving("elasticsearch", tmp_path, privileges=privileges, cluster=cluster) as move:
        with pytest.raises(sde.MigrationRefused, match=refusal):
            move.operator.stage(move.stage)
        assert move.es.operator._request("GET", f"/{move.copy}/_mapping")[0] == 404


def test_a_runtime_key_that_may_also_measure_its_indexes_is_admitted(tmp_path: Path) -> None:
    """``monitor`` reads statistics and nothing of a document, and lifts no block: the optional
    privilege a size takes (runtime-roles.md) does not stop the key's group from moving."""
    with moving("postgres", tmp_path, privileges=(*RUNTIME, "monitor")) as move:
        assert move.operator.stage(move.stage).as_record()["outcome"] == "prepared"


def test_a_runtime_key_that_does_not_cover_the_fresh_name_is_refused_before_anything(
    tmp_path: Path,
) -> None:
    with moving("postgres", tmp_path, covers_stage=False) as move:
        with pytest.raises(sde.MigrationRefused, match=f"on {move.copy}"):
            move.operator.stage(move.stage)
        assert move.es.operator._request("GET", f"/{move.copy}/_mapping")[0] == 404


def test_a_relayout_on_elasticsearch_is_refused_by_name(tmp_path: Path) -> None:
    with moving("elasticsearch", tmp_path, target="elasticsearch") as move:
        with pytest.raises(sde.MigrationRefused, match="no physical design to relayout"):
            move.operator.stage(move.stage)
        # Its cutover too, whichever map is in force: refused before anything else is read.
        relayout = cutover(move.stage, move.signed, move.model, move.public)
        assert relayout.as_record()["protocol"] == 2
        with pytest.raises(sde.MigrationRefused, match="no physical design to relayout"):
            move.operator.execute(relayout)


def test_an_index_with_a_default_pipeline_is_not_moved(tmp_path: Path) -> None:
    """A default pipeline changes a document before the fence sees it, as a trigger would."""
    with moving("elasticsearch", tmp_path) as move:
        pipeline = "sde_test_default_" + uuid4().hex[:8]
        es = move.es.operator
        assert es._request("PUT", f"/_ingest/pipeline/{pipeline}",
                           {"processors": [{"set": {"field": "note", "value": "x"}}]})[0] == 200
        try:
            assert es._request("PUT", f"/{move.source_table}/_settings",
                               {"index.default_pipeline": pipeline})[0] == 200
            with pytest.raises(sde.MigrationRefused, match="default ingest pipeline"):
                move.operator.stage(move.stage)
        finally:
            es._request("PUT", f"/{move.source_table}/_settings",
                        {"index.default_pipeline": None})
            es._request("DELETE", f"/_ingest/pipeline/{pipeline}")


def test_an_index_build_on_elasticsearch_is_refused_by_name(tmp_path: Path) -> None:
    """Every field of an Elasticsearch layout is indexed already: a map gives it no index."""
    with moving("elasticsearch", tmp_path) as move:
        current = move.stage.as_record()["current"]
        identity = uuid4().hex
        prepared = deepcopy(current)
        prepared["contract"], prepared["map_version"] = 5, current["map_version"] + 1
        prepared["groups"]["Event"]["source"]["layout"]["indexes"] = [
            {"entity": "Event", "name": sde.index_build_name(identity, 1), "columns": ["note"]}]
        plan = sde.load_index_plan(
            move.signed({"kind": "sde-index", "protocol": 1, "index_id": identity,
                         "project_id": PROJECT, "group": "Event", "current": current,
                         "prepared": move.signed(prepared), "build_budget_ms": 30000}),
            model=move.model, project_id=PROJECT, public_key=move.public,
        )
        with pytest.raises(sde.MigrationRefused, match="builds no index in place"):
            move.operator.index(plan)


def test_the_operator_cli_moves_a_group_onto_elasticsearch(tmp_path: Path) -> None:
    """`sde-operator` binds Elasticsearch by its dialect, from environment variables alone, and
    prints no connection string."""
    import json
    import subprocess
    import sys

    from psycopg.conninfo import make_conninfo

    with moving("postgres", tmp_path / "state") as move:
        assert DSN is not None
        environment = {
            **os.environ,
            "SDE_CLI_PG": make_conninfo(move.pg.operator._dsn,
                                        options="-csearch_path=" + move.pg.namespace),
            "SDE_CLI_APP_PG": move.pg.runtime._dsn,
            "SDE_CLI_ES": DSN,
            "SDE_CLI_APP_ES": move.probe_dsn,
        }
        config = {
            "protocol": 1, "project_id": PROJECT, "model": sde.neutral_declaration(move.model),
            "public_keys": {"primary": base64.b64encode(move.public).decode()},
            "engines": {
                "postgres": {"dialect": "postgres", "operator_dsn_env": "SDE_CLI_PG",
                             "runtime_dsn_envs": ["SDE_CLI_APP_PG"]},
                "elasticsearch": {"dialect": "elasticsearch", "operator_dsn_env": "SDE_CLI_ES",
                                  "runtime_dsn_envs": ["SDE_CLI_APP_ES"]},
            },
        }
        config_path = tmp_path / "config.json"
        config_path.write_text(json.dumps(config))
        stage_path, plan_path = tmp_path / "stage.json", tmp_path / "cutover.json"
        stage_path.write_text(json.dumps(move.stage.as_record()))
        base = [sys.executable, "-m", "sde_operator", "--project-dir",
                str(move.operator.store.root), "--config", str(config_path)]
        outputs = []
        for command, path in (("stage", stage_path), ("execute", plan_path)):
            if command == "execute":
                plan = cutover(move.stage, move.signed, move.model, move.public)
                path.write_text(json.dumps(plan.as_record()))
            run = subprocess.run([*base, command, "--plan", str(path)], env=environment,
                                 capture_output=True, text=True, timeout=120)
            assert run.returncode == 0, run.stderr
            outputs.append(run.stdout + run.stderr)
        receipt = json.loads(outputs[-1])
        assert receipt["outcome"] == "success" and receipt["verification"]["matched"] is True
        stored = move.operator.store.path.read_text() + "".join(outputs)
        for secret in (move.probe_dsn, DSN, move.pg.runtime._dsn):
            assert secret not in stored
        assert "api_key=" not in stored
        assert move.session().get("Event", {"id": 1}) == ROWS[1]


def test_an_alias_a_lifecycle_or_a_foreign_block_is_not_moved(tmp_path: Path) -> None:
    """Something besides this protocol acts on such an index: an alias names another index, a
    lifecycle policy can roll it over or delete it, and a block this protocol never sets is
    somebody else's to release."""
    with moving("elasticsearch", tmp_path) as move:
        es = move.es.operator
        native = move.operator.native["elasticsearch"]
        index = move.source_table
        assert native.identity(index).name == index
        alias = index + "_alias"
        policy = "sde_test_policy_" + uuid4().hex[:8]
        assert es._request("POST", "/_aliases", {"actions": [
            {"add": {"index": index, "alias": alias}}]})[0] == 200
        try:
            with pytest.raises(sde.MigrationRefused, match="alias or a pattern"):
                native.identity(alias)
        finally:
            es._request("POST", "/_aliases", {"actions": [
                {"remove": {"index": index, "alias": alias}}]})
        assert es._request("PUT", f"/_ilm/policy/{policy}", {"policy": {"phases": {
            "hot": {"actions": {}}}}})[0] == 200
        try:
            assert es._request("PUT", f"/{index}/_settings",
                               {"index.lifecycle.name": policy})[0] == 200
            with pytest.raises(sde.MigrationRefused, match="lifecycle policy"):
                native.identity(index)
        finally:
            es._request("POST", f"/{index}/_ilm/remove")
            es._request("DELETE", f"/_ilm/policy/{policy}")
        assert native.identity(index).name == index
        assert es._request("PUT", f"/{index}/_settings",
                           {"index.blocks.read_only_allow_delete": True})[0] == 200
        try:
            with pytest.raises(sde.MigrationRefused, match="read_only_allow_delete block"):
                move.operator.stage(move.stage)
        finally:
            es._request("PUT", f"/{index}/_settings",
                        {"index.blocks.read_only_allow_delete": None})



def test_a_newer_map_on_an_elasticsearch_cluster_is_named_as_the_cluster_s(tmp_path: Path) -> None:
    """The watermark is one index for the whole cluster, so the newer map can be another project's
    or a test's on the same cluster, and the refusal says so. The version stays within the clock's
    next milliseconds, so a later test's maps, which start from the clock, are still newer."""
    with moving("postgres", tmp_path) as move:
        newer = move.stage.prepared.map_version + 3
        move.es.operator.record_map_version(newer, model_version=move.model.version)
        with pytest.raises(sde.MigrationRefused) as refused:
            move.operator.stage(move.stage)
        assert str(refused.value) == (
            f"a newer map was adopted before local staging: elasticsearch records map version "
            f"{newer}, and the map this operation was prepared from is version "
            f"{move.stage.current.map_version}. An Elasticsearch cluster keeps one stream of maps, "
            f"whatever project wrote them (format contract 7m)."
        )
