"""Two ClickHouse behaviours the in-place build and its abandonment rest on (24.8.14.39).

1. ``SYSTEM STOP MERGES`` holds a ``MATERIALIZE INDEX`` mutation while inserts go on - how the live
   tests hold a build deterministically - and ``KILL MUTATION`` removes a pending one from
   ``system.mutations`` altogether.
2. ``ALTER TABLE ... DROP INDEX`` is itself a mutation. By default the ALTER waits for it on the
   merge pool, so while merges are stopped it waits until they start; with ``alter_sync = 0`` it
   returns at once and the index leaves the catalogue immediately.

    SDE_CLICKHOUSE_DSN=clickhouse://default:sde@127.0.0.1:58123/sde python probe_merge_pool.py
"""

import json
import os
import threading
import time
import uuid
from urllib.parse import urlsplit

import clickhouse_connect

parts = urlsplit(os.environ["SDE_CLICKHOUSE_DSN"])
database = "probe_" + uuid.uuid4().hex[:12]


def client(timeout: int = 60):  # type: ignore[no-untyped-def]
    return clickhouse_connect.get_client(
        host=parts.hostname,
        port=parts.port,
        username=parts.username,
        password=parts.password,
        database=database,
        send_receive_timeout=timeout,
        autogenerate_session_id=False,
    )


def rows(sql: str) -> list[list[str]]:
    return [[str(value) for value in row] for row in client().query(sql).result_rows]


def mutations(table: str) -> list[list[str]]:
    return rows(
        "SELECT mutation_id, command, is_done FROM system.mutations "
        f"WHERE database = currentDatabase() AND table = '{table}' ORDER BY create_time"
    )


out: dict[str, object] = {}
root = clickhouse_connect.get_client(
    host=parts.hostname, port=parts.port, username=parts.username, password=parts.password
)
root.command(f"CREATE DATABASE {database} ENGINE = Atomic")
try:
    c = client()
    c.command("CREATE TABLE t (id Int64, v Int32) ENGINE = MergeTree ORDER BY id")
    c.insert("t", [[i, i] for i in range(5000)], column_names=["id", "v"])
    c.command(f"SYSTEM STOP MERGES {database}.t")
    c.command("ALTER TABLE t ADD INDEX sde_i_probe v TYPE minmax GRANULARITY 4")
    c.command("ALTER TABLE t MATERIALIZE INDEX sde_i_probe")
    for i in range(10):
        c.insert("t", [[10000 + i, i]], column_names=["id", "v"])
    time.sleep(3)
    out["1_mutations_while_merges_stopped_3s"] = mutations("t")
    out["1_rows_after_inserts_while_stopped"] = rows("SELECT count() FROM t")
    c.command(f"SYSTEM START MERGES {database}.t")
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline and not all(m[2] == "1" for m in mutations("t")):
        time.sleep(0.2)
    out["1_mutations_after_merges_start"] = mutations("t")
    c.command(f"SYSTEM STOP MERGES {database}.t")
    c.command("ALTER TABLE t ADD INDEX sde_i_other id TYPE set(100) GRANULARITY 2")
    c.command("ALTER TABLE t MATERIALIZE INDEX sde_i_other")
    pending = rows(
        "SELECT mutation_id FROM system.mutations WHERE database = currentDatabase() "
        "AND table = 't' AND command = 'MATERIALIZE INDEX sde_i_other'"
    )
    c.command(
        "KILL MUTATION WHERE database = currentDatabase() AND table = 't' "
        f"AND mutation_id = '{pending[0][0]}'"
    )
    time.sleep(1)
    out["1_mutations_after_kill_of_the_pending_one"] = mutations("t")

    started = time.monotonic()
    result: dict[str, str] = {}

    def drop() -> None:
        try:
            client(timeout=8).command("ALTER TABLE t DROP INDEX sde_i_other")
            result["default"] = f"returned after {time.monotonic() - started:.1f}s"
        except Exception as exc:  # noqa: BLE001 - the timeout is the measurement
            result["default"] = f"no answer after {time.monotonic() - started:.1f}s ({type(exc).__name__})"

    worker = threading.Thread(target=drop)
    worker.start()
    worker.join()
    out["2_drop_index_default_while_merges_stopped"] = result["default"]
    c.command(f"SYSTEM START MERGES {database}.t")
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline and not all(m[2] == "1" for m in mutations("t")):
        time.sleep(0.2)
    c.command("ALTER TABLE t MATERIALIZE INDEX sde_i_probe SETTINGS mutations_sync = 2")
    c.command(f"SYSTEM STOP MERGES {database}.t")
    started = time.monotonic()
    client(timeout=8).command("ALTER TABLE t DROP INDEX sde_i_probe SETTINGS alter_sync = 0")
    out["2_drop_index_alter_sync_0_returned_after_s"] = round(time.monotonic() - started, 2)
    out["2_catalogue_right_after"] = rows(
        "SELECT name FROM system.data_skipping_indices WHERE database = currentDatabase() "
        "AND table = 't'"
    )
    out["2_mutations_right_after"] = mutations("t")
    c.command(f"SYSTEM START MERGES {database}.t")
finally:
    root.command(f"DROP DATABASE IF EXISTS {database} SYNC")
print(json.dumps(out, indent=1))
