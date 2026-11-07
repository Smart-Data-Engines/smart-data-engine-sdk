# Erasing a person's data: every copy, from the operator you already run

The library never deletes a row: there is no `Session.delete`, and a write fence guards an INSERT,
not a DELETE. When you have to honour a request to erase a person's data - Article 17 of the GDPR,
or any other - the rows are wherever the map in force put them: the group's source, every copy the
map keeps beside it, and the copy a migration is filling. `sde-operator erase` finds every one of
those tables, counts the person's rows in each, deletes them, and counts again. It runs on your
operator's login, on your machine, on the map you already hold. Nobody else can run it: we have no
connection to your engines to run it with.

## The procedure

1. **List.** Name the entity and its key, once or many times; nothing changes without `--execute`:

   ```bash
   sde-operator --project-dir <dir> --config <config.json> erase --entity Account --key '{"id": 4711}'
   ```

   Several keys: `--key` again, or `--keys <file>` with one JSON object a line. A key has exactly
   the entity's key fields. Text is accepted where the type has a text form: a UUID, a date
   (`2026-11-07`), a timestamp in ISO 8601, with an offset for a `timestamptz` field and without one
   for a `timestamp` field.

   The listing gives every table of the map in force that holds the entity - `source`, and `copy`
   for each copy the map keeps - with the rows that match, and the tables **outside** the map (see
   below). It names no key.

2. **Erase.** The same command with `--execute`. It deletes from the copies first, then from the
   source, then counts every table again, and prints the receipt:

   ```json
   {"protocol": 1, "id": "…", "project_id": "…", "entity": "Account", "group": "Account",
    "map_version": 12, "keys": 1, "key_digests": ["…"],
    "tables": [{"engine": "pg-main", "dialect": "postgres", "table": "account", "role": "source",
                "matching": 1, "deleted": 1, "remaining": 0}],
    "outside": [], "at": "2026-11-07T21:00:00+00:00", "outcome": "erased"}
   ```

   The exit code is 0 for `erased`, 1 for `incomplete`, 2 for a refusal and 3 for an engine error.
   The receipt is also kept in the project directory as `erasures/<id>.json`, mode 0600.

3. **If the outcome is `incomplete`,** a table still holds a row of the key after the deletion:
   something wrote the person again while the erasure ran. Stop that writer and run the same
   command again. Every step is idempotent, so running it again is also how an interrupted erasure
   finishes.

## What it does not do

- **It does not pause your application.** It takes no write barrier: a ClickHouse mutation can take
  minutes on a large table, and a barrier closes writes to the whole group. It runs under the
  operator's project lock, so never beside a staging, a cutover or an index build of the project; it
  is refused while one is unfinished, and `sde-operator status` names it.
- **It does not touch a table outside the map.** A table a completed move retired, or the copy an
  aborted cutover left, still holds the rows it had then, and it is no longer the entity's: another
  entity of the same shape could be in it. The listing names each table of this project, outside
  the map, that has the entity's columns, with the rows that match and the statement that would
  delete them. Whether to run it, or to drop the table, is your decision; the receipt records what
  they still hold. On Elasticsearch a retired index keeps its write block, which refuses a delete
  until it is lifted.
- **It does not reach your backups,** replicas outside the map, or the engines' own logs. Those
  follow your retention.
- **An orderbook engine deletes nothing.** An entity there is refused by name; its engine is never
  bound to the operator.

## How each engine deletes

| Engine | Statement | Physically |
|---|---|---|
| PostgreSQL | `DELETE FROM … WHERE (key…) IN (…)` | the row is dead at once and its space is reclaimed by VACUUM |
| ClickHouse | `ALTER TABLE … DELETE WHERE (key…) IN (…)` with `mutations_sync = 2` | a mutation rewrites the parts that held the row; every version a ReplacingMergeTree kept goes with it. Not a lightweight `DELETE FROM`, which only masks the row until a merge |
| Elasticsearch | `_delete_by_query?refresh=true&conflicts=abort` on the documents' `_id`s | gone from search at once, and from the segments when they merge |

The statements are in [`engine-statements.md`](engine-statements.md) with every other statement the
libraries and the operator send.

## The receipt names no key

Each key becomes `HMAC-SHA-256(secret, canonical bytes of {"entity": <entity>, "key": {<field>:
<value>}})`, in lowercase hexadecimal, where a value is written as text the one way the library
writes it: an integer as itself, a UUID in lowercase with hyphens, a date as `YYYY-MM-DD`, a
timestamp in ISO 8601 with microseconds. The canonical bytes are those of
[the format contract](format-contract.md), section 1. The secret is `erasure.key` in the project
directory: 32 random bytes, created at the first erasure, readable by its owner only, and the
operator refuses to use it otherwise.

A plain hash of a small key - a number, an e-mail address - is reversed by trying every candidate,
and a receipt is meant to be handed to whoever asks for proof that the request was honoured. With
the secret, you can show later that a given key was erased, by computing its digest again; without
it, the receipt says how many rows went from where, and nothing about whose.

## In Python

```python
from sde.erasure import ErasureRequest

request = ErasureRequest.build(model, "Account", [{"id": 4711}])
plan = operator.erase(request)                   # LocalCutover; lists, changes nothing
receipt = operator.erase(request, execute=True)  # deletes, counts again, keeps the receipt
```
