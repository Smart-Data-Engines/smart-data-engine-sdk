# Leaving: what this library leaves in your engines, and how to remove it

Your data stays in your own engines, in ordinary tables, in each engine's own format. Leaving needs
no export and no conversion. Two kinds of thing of ours can sit beside your rows, though, and one of
them **refuses your own code's writes until you remove it**. This page lists both, and
`sde.departure` and `sde-operator depart` find them in your engines and remove them.

## What is in your engines

| What | Where | Created by | Holds | Effect on a write without this library |
|---|---|---|---|---|
| column `__sde_write_epoch` | every table of a group that carries a write generation (map contract 4 and later) | `prepare_schema`, the local operator | the generation each row was written under | none by itself: PostgreSQL `DEFAULT 0`, ClickHouse `DEFAULT 0` |
| CHECK constraints `__sde_f_*` | the same tables (PostgreSQL, ClickHouse) | the same | `__sde_write_epoch >= E` and `<= E`, ownership and barrier markers ([write fences](write-fences.md)) | **refuses it**: an INSERT that does not name the column gets 0, and 0 is outside every generation |
| ingest pipeline `__sde_f_<index>` as `index.final_pipeline` | the same indexes (Elasticsearch) | the same | the same constraints, enforced per document | **refuses it** |
| `sde_map_state` | every engine a signed map was prepared on | the library | the newest map version each process loaded | none |
| `sde_backfill_state` | an engine a backfill wrote to | the library | rows copied per entity | none |
| `__sde_fence_drains` | ClickHouse | the local operator | the identity of each table a drain detached | none |
| field `__sde_tokens` | every Elasticsearch index with a text field | the library, at each write | the field's search tokens, computed by the library | none: a document written without it is accepted |

Nothing on this list holds a row of yours. Elasticsearch removes a mapped field only by a reindex, so
`__sde_write_epoch` and `__sde_tokens` stay in an index's mapping after you leave; the mapping is
`dynamic: strict`, as the library created it, and it is yours to change.

A table **outside the map in force** can also carry this project's fence: the source a completed move
retired, or the copy an abandoned one left. Those hold rows. Departure lists them and never releases
or drops them; whether to keep them is your decision.

## The procedure

1. **Finish or abandon any operation of the operator.** `sde-operator status` shows one; `resume`
   finishes it and `abandon` ends an unfinished staging or index build. Departure refuses while a
   table is held by a barrier, an interrupted setup or generation change, or, on ClickHouse, a drain
   that left it detached: releasing under an operation would open a table its recovery expects
   closed.
2. **List what is there.** With the operator configuration you already have:

   ```bash
   sde-operator --project-dir <dir> --config <config.json> depart
   ```

   It prints, per engine, every finding and the statements in two lists: `release`, after which an
   ordinary INSERT is accepted, and `cleanup`, which removes the generation column and the
   bookkeeping. Nothing runs. Only the operator's login is used; your application's runtime logins
   need not exist any more.
3. **Release.** `depart --execute` runs the release exactly as listed. It reads the engines again
   first and refuses if what it would run is no longer what was listed. Your application must have
   stopped using the library by then: a session of the same map refuses to open on a released table
   ("the write generation ... is not active"), because its writes would no longer be fenced.
4. **Point your code at the tables.** The table and column names are in the map, and the control
   plane's schema export lists them with the reasons for each placement.
5. **Clean up, if you like.** `depart --execute --cleanup` runs the release and the cleanup.

The listed statements are plain text in each engine's own language, and running them by hand is the
same as running `--execute`. On Elasticsearch they are console requests:

```text
PUT /readings/_settings {"index":{"final_pipeline":null}}
DELETE /_ingest/pipeline/__sde_f_readings
```

## What it never does

- It never renders a statement that removes a row: no `DROP TABLE` of a table with your data, no
  `TRUNCATE`, no `DELETE`.
- It never releases or drops a table outside the map in force, nor a table another project fenced.
- It keeps the bookkeeping when another project's fences are in the same namespace (a PostgreSQL
  schema, a ClickHouse database, an Elasticsearch cluster). The bookkeeping is shared there, and
  dropping it would reset that project's rollback protection.
- On Elasticsearch it cannot remove `__sde_write_epoch` or `__sde_tokens` from an index's mapping.
  Documents you write without them are accepted, and the finding says the fields stay.

## From Python

```python
listed = sde.departure(current_map, operator_engines, project_id=LOCAL_PROJECT_ID)
print(listed.as_record())            # findings, release, cleanup and refusals per engine
after = listed.execute(operator_engines, cleanup=False)
```

`operator_engines` are provisioning connections by the map's engine names, as `prepare_schema` takes
them. A generation-bearing map needs its locally configured project id, never one read off the map.

`sde.departure_script(placement, dialects)` renders the release and cleanup from a map alone, without
connecting, for an export written where the engines cannot be reached. It names the constraints the
map's own generations imply, each with `IF EXISTS`. Other names in the namespace are completed
barriers' markers, `CHECK (true)`, which accept every write; `departure` finds and removes them too.

Departure is in the Python library and its operator, like the operator's other commands. It reads
and changes the engines only, so the language your application was written in does not matter.

## What holds this page

`python/tests/test_departure_live.py` runs on PostgreSQL, ClickHouse and Elasticsearch. For each
engine it writes the way your code would: on the raw connection, without the generation. The write
is refused under the fence; that refusal is the control. The test then runs the listed text on the
same raw connection and requires the write to be accepted and every row kept. After the cleanup it
reads the engine's catalogue independently and requires nothing of ours to be left. It also holds
the refusals: a held table, a table detached by an interrupted ClickHouse drain, a listing the
engines no longer match, and tables outside the map or of another project, which stay closed with
their rows. `python/tests/test_operator_depart_live.py` runs the command itself.
