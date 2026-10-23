"""The local operator's native operations on Elasticsearch, beside the SQL ones in ``_operator``.

Elasticsearch has no grant to revoke from one login. An API key's privileges are fixed when the key
is made, and a change to its owner's role does not reach it. What Elasticsearch has is the index
block, which refuses every principal at once - the runtime, a login the operator never heard of,
and the operator itself - and which only a principal allowed to change the index's settings can
lift: an administrator, whom this protocol trusts as it trusts a database superuser. So access
moves here by blocks, not grants (``docs/local-cutover.md``):

- the pause refuses writes by the fence's holds and leaves the frozen data readable;
- the side a decision retires gets a read block for good, and keeps the holds that refuse writes;
- the side in force is checked open: no read block, and every runtime key reads and writes it.

Credentials and documents stay in the customer's adapters; no value is ever in an error.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import Any

from ..errors import EngineError, MigrationRefused
from ..placement import PhysicalLayout
from ..schema import schema_statements
from ..watermark import WATERMARK_TABLE
from ._operator import TableIdentity
from ._staging import CREATION_MARKER
from ._write_fences import fence_pipeline

DIALECT = "elasticsearch"

FORBIDDEN_CLUSTER = ("all", "manage", "manage_security", "manage_pipeline",
                     "manage_ingest_pipelines")
"""Cluster privileges a runtime key may not hold: each can rewrite a fence or grant itself more.
Broadest first, so a refusal names the privilege a role was given rather than one it implies."""

FORBIDDEN_INDEX = ("all", "manage", "delete_index")
"""Index privileges a runtime key may not hold: each can lift an index's blocks or delete it.
Broadest first, for the same reason."""

REQUIRED_INDEX = ("index", "read", "view_index_metadata")
"""What a runtime key needs on each index it uses: write, read, and read the mapping it checks."""

FOREIGN_BLOCKS = ("metadata", "read_only", "read_only_allow_delete")
"""Blocks this protocol never sets: an index carrying one is somebody else's to release."""


def _error_type(body: Any) -> str | None:
    error = body.get("error") if isinstance(body, Mapping) else None
    kind = error.get("type") if isinstance(error, Mapping) else None
    return kind if isinstance(kind, str) else None


def _true(value: Any) -> bool:
    return str(value).lower() == "true"


class ElasticsearchOperator:
    """A dedicated stock adapter plus dedicated probes for declared runtime keys."""

    dialect = DIALECT

    def __init__(self, engine: Any, runtime: Sequence[Any]) -> None:
        from .elasticsearch import ElasticsearchEngine

        if not isinstance(engine, ElasticsearchEngine) or not runtime:
            raise MigrationRefused(
                "local cutover requires stock operator adapters and runtime probes"
            )
        if any(type(probe) is not type(engine) or probe is engine for probe in runtime):
            raise MigrationRefused(
                "runtime probes must be separate connections of the same dialect"
            )
        self.engine = engine
        self.runtime = tuple(runtime)
        self.principals: tuple[str, ...] = ()
        self.principal_ids: dict[str, str] = {}
        self._cluster: str | None = None

    # --- requests --------------------------------------------------------------------------

    @staticmethod
    def _call(adapter: Any, method: str, path: str, body: Any = None) -> tuple[int, Any]:
        try:
            status, answer = adapter._request(method, path, body)
        except EngineError as exc:
            raise EngineError(f"local operator request has an uncertain outcome: {exc}") from exc
        return int(status), answer

    def _settings(self, table: str, engine: Any = None) -> Mapping[str, Any]:
        adapter = self.engine if engine is None else engine
        status, body = self._call(adapter, "GET", adapter._path(table, "_settings"))
        if status == 404:
            raise MigrationRefused(f"cutover index {table} is missing or invisible")
        if status != 200 or not isinstance(body, Mapping):
            raise EngineError(f"the settings of {table} could not be read ({status})")
        if set(body) != {table}:
            # GET <alias>/_settings answers with the indexes behind it, under their own names.
            raise MigrationRefused(
                f"{table} names an alias or a pattern, not an index: a cutover moves an index"
            )
        settings = body[table].get("settings", {}).get("index")
        if not isinstance(settings, Mapping) or not isinstance(settings.get("uuid"), str):
            raise EngineError(f"the settings of {table} carry no index identity")
        return settings

    # --- identity ----------------------------------------------------------------------------

    def endpoint(self, engine: Any = None) -> tuple[str, str, str]:
        """The cluster, and the map stream on it: ``sde_map_state`` is one per cluster.

        The cluster's own identity needs the cluster privilege ``monitor``, which the operator's
        login has and a runtime key does not; a probe is placed on the same cluster by the identity
        of ``sde_map_state`` it reads, which is a uuid the cluster drew when it created the index.
        """
        if self._cluster is None:
            status, body = self._call(self.engine, "GET", "/")
            cluster = body.get("cluster_uuid") if isinstance(body, Mapping) else None
            if status != 200 or not isinstance(cluster, str) or cluster in ("", "_na_"):
                raise MigrationRefused(
                    "native endpoint identity is unavailable: the operator's login needs the "
                    "cluster privilege monitor"
                )
            self._cluster = cluster
        stream = str(self._settings(WATERMARK_TABLE, engine)["uuid"])
        return self._cluster, stream, WATERMARK_TABLE

    def identity(self, table: str, *, engine: Any = None) -> TableIdentity:
        """The index under ``table``, refused where something besides this protocol acts on it.

        A default pipeline changes a document before the fence sees it, as a trigger would; a
        lifecycle policy can roll the index over, shrink it or delete it; and a block this protocol
        never sets is somebody else's. The read and write blocks are this protocol's own.
        """
        cluster, stream, _ = self.endpoint(engine)
        settings = self._settings(table, engine)
        if settings.get("default_pipeline") not in (None, "_none"):
            raise MigrationRefused(
                f"index {table} runs a default ingest pipeline, which changes documents outside "
                f"the write fence; a cutover moves indexes without one"
            )
        lifecycle = settings.get("lifecycle")
        if isinstance(lifecycle, Mapping) and lifecycle.get("name"):
            raise MigrationRefused(
                f"index {table} has a lifecycle policy, which can roll it over or delete it "
                f"outside this protocol"
            )
        blocks = settings.get("blocks")
        blocks = blocks if isinstance(blocks, Mapping) else {}
        foreign = [name for name in FOREIGN_BLOCKS if _true(blocks.get(name))]
        if foreign:
            raise MigrationRefused(
                f"index {table} carries a {foreign[0]} block, which this protocol never sets"
            )
        return TableIdentity(DIALECT, cluster, stream, str(settings["uuid"]), "", table)

    def _blocked(self, table: str, kind: str) -> bool:
        blocks = self._settings(table).get("blocks")
        return isinstance(blocks, Mapping) and _true(blocks.get(kind))

    # --- runtime keys ------------------------------------------------------------------------

    def _principal(self, adapter: Any) -> tuple[str, str]:
        status, body = self._call(adapter, "GET", "/_security/_authenticate")
        if status != 200 or not isinstance(body, Mapping):
            raise EngineError(f"a login's identity could not be read ({status})")
        kind = body.get("authentication_type")
        if kind == "api_key":
            key = body.get("api_key")
            ident = key.get("id") if isinstance(key, Mapping) else None
            if not isinstance(ident, str) or not ident:
                raise EngineError("an API key's identity could not be read")
            return f"api_key:{ident}", ident
        realm = body.get("authentication_realm")
        realm_name = realm.get("name") if isinstance(realm, Mapping) else None
        user = body.get("username")
        if kind == "realm" and isinstance(realm_name, str) and isinstance(user, str) and user:
            name = f"user:{realm_name}:{user}"
            return name, name
        raise MigrationRefused(
            "cutover runtime must log in with an API key or a realm user's password"
        )

    def _privileges(
        self, probe: Any, names: Sequence[str]
    ) -> tuple[Mapping[str, Any], Mapping[str, Any]]:
        body = {
            "cluster": sorted({*FORBIDDEN_CLUSTER, "read_pipeline"}),
            "index": [{"names": sorted(names), "privileges": sorted({*FORBIDDEN_INDEX,
                                                                     *REQUIRED_INDEX})}],
        }
        status, answer = self._call(probe, "POST", "/_security/user/_has_privileges", body)
        cluster = answer.get("cluster") if isinstance(answer, Mapping) else None
        index = answer.get("index") if isinstance(answer, Mapping) else None
        if status != 200 or not isinstance(cluster, Mapping) or not isinstance(index, Mapping):
            raise EngineError(f"a runtime key's privileges could not be read ({status})")
        return cluster, index

    def qualify(
        self,
        tables: Sequence[str],
        allowed_tables: Sequence[str],
        *,
        required_access: bool = True,
        readers: bool = True,
    ) -> tuple[str, ...]:
        """Require runtime keys that cannot lift a block or rewrite a fence.

        Each probe asks Elasticsearch which privileges it holds, on ``tables`` and on every name
        in ``allowed_tables`` - fresh staging names included, which a key covers by a pattern
        before the operator creates them, because nothing here grants a privilege. ``readers`` is
        accepted and has nothing to do: no list of other principals is read, because a block
        refuses every principal whatever it holds, and one that can lift a block is an
        administrator.
        """
        del readers
        operator, _ = self._principal(self.engine)
        names = sorted({*tables, *allowed_tables})
        principals: list[str] = []
        identities: dict[str, str] = {}
        for probe in self.runtime:
            who, ident = self._principal(probe)
            if who == operator:
                raise MigrationRefused("a runtime probe logs in as the operator")
            cluster, index = self._privileges(probe, names)
            held = [name for name in FORBIDDEN_CLUSTER if cluster.get(name)]
            if held:
                raise MigrationRefused(
                    f"cutover runtime holds the cluster privilege {held[0]}, with which it could "
                    f"rewrite a write fence; a runtime key needs read_pipeline and no more"
                )
            if required_access and not cluster.get("read_pipeline"):
                raise MigrationRefused(
                    "cutover runtime needs the cluster privilege read_pipeline: a session reads "
                    "the write fence of each index it opens"
                )
            for name in names:
                rights = index.get(name)
                rights = rights if isinstance(rights, Mapping) else {}
                held = [privilege for privilege in FORBIDDEN_INDEX if rights.get(privilege)]
                if held:
                    raise MigrationRefused(
                        f"cutover runtime holds {held[0]} on {name}, with which it could lift "
                        f"the index's blocks or delete it"
                    )
                if required_access and not all(rights.get(p) for p in REQUIRED_INDEX):
                    raise MigrationRefused(
                        f"cutover runtime needs index, read and view_index_metadata on {name}; a "
                        f"staged copy's fresh name (sde_m_*) is covered by a pattern in advance"
                    )
            if required_access:
                for table in tables:
                    if self.identity(table, engine=probe).physical_key != \
                            self.identity(table).physical_key:
                        raise MigrationRefused("runtime resolves a different physical index")
            principals.append(who)
            identities[who] = ident
        if len(set(principals)) != len(principals):
            raise MigrationRefused("runtime probes repeat the same login")
        self.principals = tuple(sorted(principals))
        self.principal_ids = identities
        return self.principals

    # --- access ------------------------------------------------------------------------------

    def _same(self, tables: Sequence[TableIdentity], when: str) -> None:
        for table in tables:
            if self.identity(table.name).physical_key != table.physical_key:
                raise MigrationRefused(f"physical index identity changed before {when}")

    def _count(self, probe: Any, table: str) -> tuple[int, Any]:
        return self._call(probe, "POST", probe._path(table, "_count"),
                          {"query": {"match_none": {}}})

    def access(self, tables: Sequence[TableIdentity], *, enabled: bool) -> None:
        """Open ``tables`` to the runtime: no read block, and every key reads and writes them."""
        if not enabled:
            raise MigrationRefused(
                "Elasticsearch moves access by blocks: a pause leaves an index as it is and a "
                "retirement blocks it for good; no privilege of a login is revoked"
            )
        if not self.principals:
            raise MigrationRefused("runtime principals were not qualified")
        self._same(tables, "access transition")
        for table in tables:
            if self._blocked(table.name, "read"):
                raise MigrationRefused(
                    f"index {table.name} is blocked for reads: it was retired, or somebody else "
                    f"blocked it, and this protocol opens neither"
                )
        for probe in self.runtime:
            _, index = self._privileges(probe, [table.name for table in tables])
            for table in tables:
                rights = index.get(table.name)
                rights = rights if isinstance(rights, Mapping) else {}
                if not all(rights.get(privilege) for privilege in REQUIRED_INDEX):
                    raise MigrationRefused(
                        f"a runtime key does not cover {table.name}: it needs index, read and "
                        f"view_index_metadata there"
                    )
                status, _ = self._count(probe, table.name)
                if status != 200:
                    raise EngineError(
                        "runtime access probe did not establish the expected permissions"
                    )

    def pause(self, tables: Sequence[TableIdentity]) -> None:
        """Nothing to revoke: the holds refuse writes, and frozen data stays readable. The freeze
        on each side of this step reads each index through its fence, by its uuid."""

    def retire(self, tables: Sequence[TableIdentity]) -> None:
        """Block ``tables`` for reads, for good, and see every runtime key refused.

        The holds the retired side keeps refuse its writes, so with this block nothing reads or
        writes it again - no runtime key, and no principal the operator does not know. The effect
        is seen from each key's side: an acknowledgement is the engine's word, a refused read is
        the block.
        """
        self._same(tables, "retirement")
        for table in tables:
            status, body = self._call(self.engine, "PUT",
                                      self.engine._path(table.name, "_block", "read"))
            if status != 200 or not isinstance(body, Mapping) or body.get("acknowledged") \
                    is not True:
                raise EngineError(f"the read block on {table.name} was not confirmed ({status})")
        for probe in self.runtime:
            for table in tables:
                status, body = self._count(probe, table.name)
                if status == 200:
                    raise MigrationRefused("runtime still reads an index after it was retired")
                if _error_type(body) != "cluster_block_exception":
                    raise EngineError(
                        "runtime access probe did not establish the expected denial"
                    )

    # --- repair ------------------------------------------------------------------------------

    def truncate(self, tables: Sequence[TableIdentity]) -> None:
        """Delete every document of each index, and see none left.

        Not ``TRUNCATE``: Elasticsearch deletes a query's documents one by one, so this costs time
        in the number of documents, inside the pause.
        """
        self._same(tables, "repair")
        for table in tables:
            path = self.engine._path(table.name, "_refresh")
            status, _ = self._call(self.engine, "POST", path)
            if status != 200:
                raise EngineError(f"refreshing {table.name} before repair failed ({status})")
            path = self.engine._path(table.name, "_delete_by_query") + \
                "?refresh=true&conflicts=abort"
            status, body = self._call(self.engine, "POST", path, {"query": {"match_all": {}}})
            if (
                status != 200
                or not isinstance(body, Mapping)
                or body.get("timed_out") is not False
                or body.get("failures")
            ):
                raise EngineError(
                    f"emptying {table.name} has an uncertain outcome ({status}); resume its intent"
                )
            if self.engine.count(table.name):
                raise MigrationRefused(f"an emptied index {table.name} still holds documents")


class ElasticsearchStaging:
    """A staging's own index: created with its marker, recognized by it, and removed by it."""

    def __init__(self, native: ElasticsearchOperator) -> None:
        self.native = native
        self.engine = native.engine
        self.dialect = DIALECT

    def marker(self, table: str) -> tuple[str, str | None] | None:
        status, body = self.native._call(self.engine, "GET", self.engine._path(table, "_mapping"))
        if status == 404:
            return None
        if status != 200 or not isinstance(body, Mapping):
            raise EngineError(f"the mapping of {table} could not be read ({status})")
        if set(body) != {table}:
            raise MigrationRefused("staging index name has an ambiguous native identity")
        meta = body[table].get("mappings", {}).get("_meta", {})
        sde = meta.get("sde") if isinstance(meta, Mapping) else None
        stage = sde.get("stage") if isinstance(sde, Mapping) else None
        uuid = str(self.native._settings(table)["uuid"])
        return uuid, stage if isinstance(stage, str) else None

    def preflight(self, layout: PhysicalLayout, keys: Mapping[str, Sequence[str]]) -> None:
        schema_statements(layout, keys=keys, dialect=DIALECT)
        for table in layout.tables.values():
            if self.marker(table) is not None:
                raise MigrationRefused("a fresh staging name already exists in the target")

    def create_table(
        self, *, entity: str, layout: PhysicalLayout, keys: Mapping[str, Sequence[str]], marker: str
    ) -> TableIdentity:
        if CREATION_MARKER.fullmatch(marker) is None:
            raise MigrationRefused("staging creation marker is not valid protocol metadata")
        table = layout.tables[entity]
        present = self.marker(table)
        if present is not None:
            if present[1] != marker:
                raise MigrationRefused("staging refuses an index without its exact creation marker")
            return self.native.identity(table)
        single = PhysicalLayout(tables={entity: table}, columns={entity: layout.columns[entity]})
        statements = schema_statements(single, keys=keys, dialect=DIALECT)
        if len(statements) != 1:
            raise MigrationRefused("staging requires the SDK's ordinary index creation")
        document = json.loads(statements[0])
        mappings = document["mappings"]
        # In the request that creates the index, as a SQL staging comments its table in the
        # transaction that creates it: an index of this name without it is not this staging's.
        mappings["_meta"]["sde"]["stage"] = marker
        status, body = self.native._call(self.engine, "PUT", self.engine._path(table),
                                         {"mappings": mappings})
        if status != 200 and _error_type(body) != "resource_already_exists_exception":
            raise EngineError(
                f"staging index creation has an uncertain outcome ({status}); resume its intent"
            )
        found = self.marker(table)
        if found is None or found[1] != marker:
            raise MigrationRefused("staging creation marker was not established with the index")
        return self.native.identity(table)

    def owned(
        self, table: str, marker: str, identity: TableIdentity | None
    ) -> TableIdentity | None:
        """This staging's own index under ``table``, or ``None`` when it is absent or not ours."""
        present = self.marker(table)
        if present is None or present[1] != marker:
            return None
        if identity is not None:
            return identity if present[0] == identity.object else None
        return self.native.identity(table)

    def drop_owned(self, table: TableIdentity, marker: str) -> None:
        """Delete this staging's own index, then its write fence's pipeline."""
        present = self.marker(table.name)
        if present is not None:
            if present[1] != marker or present[0] != table.object:
                raise MigrationRefused("the index to abandon is no longer this staging's own")
            status, _ = self.native._call(self.engine, "DELETE", self.engine._path(table.name))
            if status != 200:
                raise EngineError(f"deleting {table.name} has an uncertain outcome ({status})")
            if self.marker(table.name) is not None:
                raise MigrationRefused("an abandoned staging index is still in the cluster")
        # The pipeline outlives its index; it is this staging's when it names the index removed.
        path = f"/_ingest/pipeline/{fence_pipeline(table.name)}"
        status, body = self.native._call(self.engine, "GET", path)
        if status == 404:
            return
        document = body.get(fence_pipeline(table.name)) if isinstance(body, Mapping) else None
        meta = document.get("_meta", {}).get("sde", {}) if isinstance(document, Mapping) else {}
        if status != 200 or not isinstance(meta, Mapping):
            raise EngineError(f"the write fence of {table.name} could not be read ({status})")
        if meta.get("index") == table.name and meta.get("uuid") == table.object:
            status, _ = self.native._call(self.engine, "DELETE", path)
            if status not in (200, 404):
                raise EngineError(f"deleting the write fence of {table.name} failed ({status})")

    def revoke_runtime(self, tables: Sequence[TableIdentity], principals: Sequence[str]) -> None:
        """Nothing to take back: a key's privileges are its own, and the index is gone."""

    def create_indexes(self, layout: PhysicalLayout) -> None:
        """Nothing to build: every field of an Elasticsearch layout is indexed already."""
