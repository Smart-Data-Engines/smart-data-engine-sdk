# Abandoning a staging: evidence

[Abandonment](../../staging.md#abandoning-a-staging-that-cannot-finish) removes a staging's own
copy before its decision. One engine behaviour decides what it has to do beyond `DROP TABLE`, and it
was measured before the code was written - ClickHouse 24.8.14.39 in the SDK's test container,
24 September 2026:

`probe_grants_after_drop.py` -> `probe_grants_after_drop.out.json`: a runtime login's `SELECT` and
`INSERT` grants on a table are still listed in `system.grants` after `DROP TABLE ... SYNC`, and
`REVOKE` on the dropped table is accepted and removes them. So an abandonment on ClickHouse revokes
the runtime logins' grants on the copy after dropping it, and reads `system.grants` back; PostgreSQL
removes a table's privileges with the table.

The behaviour of the abandonment itself is pinned by `python/tests/test_staging_abandon_live.py` on
both engines.
