"""What an API key can read of its own indexes' size, and the window a session builds from it.

``_stats`` needs the index privilege ``monitor``, which nothing else asks of a runtime key. Without
it the answer is a 403, and a session's size is unknown, with the reason ``refused``; with it the
key reads what the cluster's administrator reads. Numbers only - no document is read. Set
SDE_ELASTICSEARCH_DSN to run (measured on 9.5.5).
"""

from __future__ import annotations

import os
import urllib.parse
from collections.abc import Iterator, Sequence
from contextlib import contextmanager
from typing import Any
from uuid import uuid4

import pytest

import sde
from sde.engines.elasticsearch import ElasticsearchEngine, HttpStatus

DSN = os.environ.get("SDE_ELASTICSEARCH_DSN")
pytestmark = pytest.mark.skipif(DSN is None, reason="set SDE_ELASTICSEARCH_DSN to run")
RUNTIME = ("read", "index", "view_index_metadata")
"""What a runtime key holds on its indexes (runtime-roles.md), without the optional ``monitor``."""


def administrator() -> ElasticsearchEngine:
    assert DSN is not None
    engine = ElasticsearchEngine(DSN)
    engine.connect()
    return engine


def remove(method: str, path: str, body: Any = None) -> None:
    """Cleanup on a connection of its own: one that lost a response is dropped, and a test that
    failed on it must not leave its indexes and keys behind. A refused cleanup fails the test."""
    engine = administrator()
    try:
        status, answer = engine._request(method, path, body)
        assert status in (200, 404), (path, answer)
    finally:
        engine.close()


def remove_indexes(prefix: str) -> None:
    """Every index whose name starts with ``prefix``, each by name. The cluster refuses a wildcard
    in a destructive request (``action.destructive_requires_name``, true by default since 8.0), so
    ``DELETE /<prefix>*`` removed nothing, and until 24 October every run left its indexes."""
    engine = administrator()
    try:
        status, body = engine._request("GET", f"/_resolve/index/{prefix}*?expand_wildcards=all")
        assert status == 200, body
        names = [entry["name"] for entry in body["indices"]]
    finally:
        engine.close()
    for name in names:
        remove("DELETE", f"/{name}")


@contextmanager
def keyed(admin: ElasticsearchEngine, names: Sequence[str],
          privileges: Sequence[str]) -> Iterator[ElasticsearchEngine]:
    """An adapter on an API key with ``privileges`` on ``names``, removed afterwards."""
    assert DSN is not None
    status, key = admin._request("POST", "/_security/api_key", {
        "name": "sde-storage-" + uuid4().hex[:8],
        "role_descriptors": {"runtime": {
            "cluster": ["read_pipeline"],
            "indices": [{"names": list(names), "privileges": list(privileges)}],
        }},
    })
    assert status == 200, key
    parts = urllib.parse.urlsplit(DSN)
    query = urllib.parse.urlencode(
        [("api_key", key["encoded"]), *urllib.parse.parse_qsl(parts.query)]
    )
    host = parts.netloc.rpartition("@")[2]
    scoped = urllib.parse.urlunsplit((parts.scheme, host, parts.path, query, ""))
    engine = ElasticsearchEngine(scoped)
    engine.connect()
    try:
        yield engine
    finally:
        engine.close()
        remove("DELETE", "/_security/api_key", {"ids": [key["id"]]})


@contextmanager
def notes() -> Iterator[tuple[ElasticsearchEngine, sde.LogicalModel, sde.PlacementMap, str]]:
    """The administrator, and a group of one entity in an index of its own: its model and map."""
    assert DSN is not None
    sde.clear_registry()

    @sde.entity
    class Note:
        id: int
        title: str

    model = sde.build_model(Note)
    table = "sde_size_" + uuid4().hex[:12]
    columns = {"id": "long", "title": "keyword"}
    admin = administrator()
    try:
        admin.ensure_schema(sde.PhysicalLayout(tables={"Note": table}, columns={"Note": columns}),
                            keys={"Note": ["id"]})
        placement = sde.load_map(
            {"contract": 3, "model_version": model.version, "map_version": 1,
             "groups": {"Note": {"source": {"id": "es", "engine": "es", "layout": {
                 "tables": {"Note": table}, "columns": {"Note": columns}}}}}},
            model=model,
        )
        yield admin, model, placement, table
    finally:
        admin.close()
        remove_indexes(table)


def settle(admin: ElasticsearchEngine, *indexes: str) -> None:
    """One segment each, so a background merge cannot change a size between two reads of it."""
    names = ",".join(indexes)
    for path in (f"/{names}/_forcemerge?max_num_segments=1", f"/{names}/_refresh"):
        status, body = admin._request("POST", path)
        assert status == 200, body


def rows(start: int, count: int) -> list[dict[str, Any]]:
    return [{"id": start + index, "title": f"note {start + index}"} for index in range(count)]


def test_a_key_reads_its_indexes_sizes_with_monitor_and_is_refused_without_it() -> None:
    with notes() as (admin, model, placement, filled):
        empty, alias, missing = filled + "_empty", filled + "_alias", filled + "_missing"
        admin.ensure_schema(
            sde.PhysicalLayout(tables={"Note": empty},
                               columns={"Note": {"id": "long", "title": "keyword"}}),
            keys={"Note": ["id"]},
        )
        sde.Session(model, placement, {"es": admin}).save_many("Note", rows(0, 500))
        settle(admin, filled, empty)
        status, body = admin._request(
            "POST", "/_aliases", {"actions": [{"add": {"index": filled, "alias": alias}}]}
        )
        assert status == 200, body
        names = [filled, empty, alias, missing]
        with keyed(admin, [filled + "*"], RUNTIME) as plain:
            with pytest.raises(sde.EngineError, match="storage sizes could not be read") as caught:
                plain.storage_sizes(names)
            assert isinstance(caught.value.__cause__, HttpStatus)
            assert caught.value.__cause__.status == 403
        with keyed(admin, [filled + "*"], (*RUNTIME, "monitor")) as monitored:
            measured = monitored.storage_sizes(names)
            assert measured == admin.storage_sizes(names), "the key reads what the cluster's does"
            # Absent, not zero: an index that does not exist, and an alias - this measures indexes,
            # never what a name resolves to.
            assert set(measured) == {filled, empty}
            assert measured[filled][0] > measured[empty][0] >= 0
            assert measured[filled][1] == measured[empty][1] == 0
            assert monitored.storage_sizes([]) == {}


def test_a_session_on_elasticsearch_measures_its_group_and_the_window_carries_it() -> None:
    with notes() as (admin, model, placement, table):
        with keyed(admin, [table], RUNTIME) as plain:
            session = sde.Session(model, placement, {"es": plain},
                                  recorder=sde.Recorder(model.version))
            refused = session.measure_storage()
            assert refused.sizes == ()
            assert refused.unavailable == {"Note": "refused"}
        with keyed(admin, [table], (*RUNTIME, "monitor")) as monitored:
            recorder = sde.Recorder(model.version)
            session = sde.Session(model, placement, {"es": monitored}, recorder=recorder)
            for start in (0, 1_000):
                session.save_many("Note", rows(start, 1_000))
            settle(admin, table)
            measured = session.measure_storage()
            assert measured.unavailable == {}
            (size,) = measured.sizes
            assert (size.group, size.engine, size.materialization) == ("Note", "es", "es")
            assert (size.total_bytes, size.secondary_index_bytes) == (
                admin.storage_sizes([table])[table]
            )
            assert size.total_bytes > 0
            window = recorder.roll()
            assert window is not None
            body = window.as_record(model)["groups"]["Note"]
            assert body["total_bytes"] == size.total_bytes
            # No secondary index beside the store, measured: a ratio of 0, not an unknown.
            assert body["index_to_table_ratio"] == 0.0
            assert "total_bytes" not in body["missing"]
            assert "index_to_table_ratio" not in body["missing"]


def test_a_size_from_fewer_primaries_than_the_index_has_is_refused_not_reported() -> None:
    """An unassigned primary counts as neither a success nor a failure in ``_shards``: summed
    without it, the size read smaller than it is, and an index whose only primary is unassigned
    read as missing. It is refused, and a session's reason for the unknown size is ``failed``, not
    ``missing_table`` (second review)."""
    with notes() as (admin, model, _placement, table):
        stranded = table + "_stranded"
        status, body = admin._request("GET", f"/{table}/_mapping")
        assert status == 200, body
        # The library's own mapping, on a primary no node may take. Closing an index and opening it
        # under the same filter does not strand it: an existing primary is allocated anyway.
        status, body = admin._request("PUT", f"/{stranded}?wait_for_active_shards=0", {
            "settings": {"index.routing.allocation.require._name": "no-such-node",
                         "index.number_of_replicas": 0},
            "mappings": body[table]["mappings"],
        })
        assert status == 200, body
        with pytest.raises(sde.EngineError, match="from 0 of its 1 primary shards"):
            admin.storage_sizes([table, stranded])
        assert set(admin.storage_sizes([table])) == {table}, "a healthy index still measures"
        on_stranded = sde.load_map(
            {"contract": 3, "model_version": model.version, "map_version": 1,
             "groups": {"Note": {"source": {"id": "es", "engine": "es", "layout": {
                 "tables": {"Note": stranded},
                 "columns": {"Note": {"id": "long", "title": "keyword"}}}}}}},
            model=model,
        )
        session = sde.Session(model, on_stranded, {"es": admin})
        assert session.measure_storage().unavailable == {"Note": "failed"}
