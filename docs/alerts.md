# What deserves an alert

*In English because it is for whoever is on call, not for us.*

This library runs in your application and reports through it: its events go to the `sde` logger in
Python and to the sink you pass in C++ and TypeScript, and its operations to the hook you give a
session ([observability](observability.md)). This page says which of them should wake somebody, which
should open a ticket, and which are for reading when something else went wrong. What each failure
does to your call is in [failure semantics](failure-semantics.md); this page is what to do about it.

Three rules shaped it:

- **The library never pages for itself.** No event is above INFO, and nothing here is sent anywhere
  by us. A page is your decision, made on your numbers.
- **A single failed operation is not an incident.** Engines restart, connections reset, and each of
  those fails the operation in flight with a named error and leaves the next one to succeed. Alert
  on rates, not on lines.
- **Every event name below is a closed vocabulary.** A test fails if this page and the library's
  list disagree in either direction, so an alert written against a name keeps working.

## Events

| Event | What it means | Page? | Check | Do |
|---|---|---|---|---|
| `sde.model.built` | the model this process uses, with its `model_version` | no | - | keep it: every map and refusal keys on that version |
| `sde.map.loaded` | a placement map was accepted; `signed` and `forward_only` say which kind | no | - | - |
| `sde.map.rejected` | a map was refused, with the error class and the structural reason | yes, when a process cannot start or keeps refusing | the map file the process reads, its model version against `sde.model.built` | deploy the map the control plane issued for this model, or roll the application back to the model the map was issued for |
| `sde.map.forward_only` | a signed map was checked against the newest one applied to your engines and is not older | no | - | - |
| `sde.map.rollback_unprotected` | no engine in the map can keep the record of applied maps, so an older map cannot be recognised | no; a ticket | whether the map places a group on an engine without bookkeeping (our orderbook engine has a fixed schema) | accept it knowingly, or keep a group with bookkeeping in the map |
| `sde.route.resolved` | a read went where the map's routing table sends it | no | - | - |
| `sde.route.fallback` | a read went to the source because the map routes that shape nowhere | no | - | the normal state of a hand-written map |
| `sde.schema.applied` | a session or the provisioning step created what was missing | no | - | - |
| `sde.schema.extra_columns` | a table has columns the map does not name | no; a ticket if unexpected | who added them | nothing, if they are yours; writes are unaffected |
| `sde.schema.text_collation` | a PostgreSQL index on a text column is not in the `C` collation the library's reads use, so no scan, count or summary can use it | no; a ticket | the index named in the fields | the remedy in the fields: a staging into a fresh copy, or an administrator's `ALTER COLUMN` |
| `sde.schema.physical_mismatch` | a table's sort key, key order, partition or index is not the design the map declares | no; a ticket | `Session.physical` | provision with the map, or ask for the design to be revised; rows are unaffected, only speed |
| `sde.write.failed` | an engine refused a write; the error class is in the fields | on its rate, below | the hook's `outcome` for the same engine | see [failure semantics](failure-semantics.md): the write did not happen or its outcome is uncertain, and nothing was retried |
| `sde.migration.divergence` | during a move, a copy did not take a write the source took | no; a ticket to whoever runs the move | the move's verification, which refuses to switch reads while any divergence remains | let verification and backfill close it; the source is authoritative |
| `sde.migration.backfill_progress` | one backfill chunk landed, with a row count | no | that it moves | - |
| `sde.orderbook.flushed` | the orderbook engine made writes visible, and how many rows | no | - | - |
| `sde.explain.no_query_tree` | a ClickHouse server did not answer `EXPLAIN QUERY TREE`; the plan came from `EXPLAIN ESTIMATE` | no | the server version | - |
| `sde.internal.error` | something internal to the library failed and was counted, never your operation: a telemetry aggregate, a log handler, the hook you gave a session | no; a ticket | `internal_failures()`, keyed by the same `operation` | a hook of yours that raises shows here as `session.hook`; anything else is a defect of ours, so report it |
| `sde.telemetry.window_closed` | a telemetry window closed and is buffered in your process for you to collect | no | - | - |
| `sde.telemetry.dropped` | the telemetry buffer was full and its oldest window was discarded | no; a ticket | whether your application collects windows (`Recorder.pending()` and `acknowledge()`) | collect them, or stop recording; operations are never lost, only telemetry |
| `sde.telemetry.storage_rejected` | a storage sample handed to the recorder was not two sane integers | no; a ticket | the code that calls `record_storage` | fix the caller |
| `sde.telemetry.storage_unavailable` | an engine could not say a group's size | no | the grant named in [runtime roles](runtime-roles.md) (ClickHouse's `system.parts`) | grant it, or leave the size unknown |

## Numbers from the hook

The hook gives every operation's engine, duration and outcome. Three alerts cover what the events
above cannot:

- **Errors per engine.** The share of records whose `outcome` is not `ok`, per `engine`, over five
  minutes. Page above 5 % for five minutes running. Under the soak's injected faults - restarts,
  crashes, resets, refusals, partitions - each fault failed one operation in flight per process and
  the next succeeded, within a second of the engine coming back; a sustained rate is an engine that
  is not coming back, not a blip.
- **`unexpected`.** Any record with `outcome` `unexpected` is an error that is not this library's: a
  defect of ours, or an exception from your own code inside a transaction. Ticket on the first one.
- **Slower than usual.** p99 of `duration_ns` per `shape` against the same hour a week before. A
  ticket at three times; not a page, because a slow engine is a capacity question first.

None of these thresholds is ours to set for your workload: they are where to start, and the soak's
numbers they start from are in its record, on named hardware.
