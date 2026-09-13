# Restricted runtime credentials and map bookkeeping

Generation-bearing sessions can use a database login with access to its application tables and
`sde_map_state`, without CREATE or ALTER privileges. Run `prepare_schema` / `prepareSchema` first
on separate local operator connections. For a signed map, provisioning prepares the bookkeeping
on every supplied engine that supports it, including one with no materialization in this map:
all supplied capable engines participate in the session's rollback protection.

Provisioning does not record the prepared map's version. An operator may be preparing a future
map while applications still use the current one; advancing the watermark at preparation time
would refuse those applications before the future generation was ready. Runtime admission still
checks model, project, physical columns and actual write generations before adopting a watermark.

```python
sde.prepare_schema(model, placement, operator_engines, project_id=LOCAL_PROJECT_ID)
# The local DBA grants table access to the separately configured application login.
session = sde.Session(model, placement, runtime_engines, project_id=LOCAL_PROJECT_ID)
```

```typescript
await prepareSchema(model, placement, operatorEngines, { projectId: LOCAL_PROJECT_ID })
// The local DBA grants table access to the separately configured application login.
const session = await Session.open(model, placement, runtimeEngines, { projectId: LOCAL_PROJECT_ID })
```

These connections and credentials remain inside the customer's environment. Neither provisioning
nor runtime contacts our control plane for a data operation.

## Table access

The local DBA creates the logins and namespace. For the concrete example of a single physical
table named `events` in namespace `customer_data`, the tested table grants are:

```sql
-- PostgreSQL: app_runtime is a separate login, not the table/schema owner.
GRANT USAGE ON SCHEMA customer_data TO app_runtime;
GRANT SELECT, INSERT ON customer_data.events, customer_data.sde_map_state TO app_runtime;
```

```sql
-- ClickHouse: app_runtime is a separate SQL-managed user.
GRANT SELECT, INSERT ON customer_data.events TO app_runtime;
GRANT SELECT, INSERT ON customer_data.sde_map_state TO app_runtime;
-- The Python driver's initialization reads settings.
GRANT SELECT ON system.settings TO app_runtime;
```

Use the physical tables in the loaded map, including copies; `events` is only an example.
Bookkeeping uses an append-only maximum, so runtime needs SELECT and INSERT on `sde_map_state`.
It does not need UPDATE, DELETE or ownership of that table. Continue using a separate PostgreSQL
schema or ClickHouse database per independent map stream; the watermark remains namespace-wide.

A PostgreSQL role's effective rights include inherited and PUBLIC grants, and owners/superusers
have additional powers. Removing one direct grant does not establish effective denial.
[PostgreSQL GRANT](https://www.postgresql.org/docs/15/sql-grant.html) and
[ClickHouse GRANT](https://clickhouse.com/docs/reference/statements/grant) describe those grant
models. This SDK change permits restricted credentials; it does not yet audit the complete role
graph or implement cutover's grant revocation. Qualifying that configuration remains part of the
local cutover executor's work.

## Reading existing metadata and compatibility

Both adapters check for an existing bookkeeping table through the native catalog before any
CREATE. PostgreSQL uses `to_regclass`; ClickHouse uses `EXISTS TABLE`. CREATE IF NOT EXISTS was
insufficient: both engines refused it without CREATE even when the table already existed.
Existing installations therefore need no schema rewrite or metadata reset for this change.

If the table is missing, the longstanding lazy initialization remains available to a caller with
provisioning rights. A restricted runtime login must fail in that situation; grant the intended
rights to the intended existing tables after provisioning, rather than adding DDL privileges to
the application. A failed lookup or SELECT is an error, never an empty watermark or permission
to accept an older signed map.

Unsigned no-account maps keep their existing behavior: provisioning and runtime do not create or
read map bookkeeping. This does not disable native generation checks when the unsigned map uses
contract 4.

## Native regression environment

The tests create disposable logins and isolated namespaces, then use the actual SDK adapters on
those logins. They prove signed save/get and rollback protection, absent CREATE/ALTER permissions,
metadata preparation without version adoption, refused unreadable metadata while INSERT remains
granted, an additional configured engine, and unsigned operation without bookkeeping.

The test administrator needs CREATE ROLE on PostgreSQL and access management on ClickHouse. The
SDK Makefile and CI start a disposable ClickHouse with
`CLICKHOUSE_DEFAULT_ACCESS_MANAGEMENT=1`. A test container created before that setting was added
retains its old configuration when merely restarted; recreate that disposable test container or
point the tests at a separately configured one. This is a test administrator setting, not a runtime
grant. No customer account or hosting service is configured by these tests.
