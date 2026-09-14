# Session and connection lifetime

A Session is a unit of work over a declared model and placement map. An active transaction belongs
to its session and execution context. A foreign session or independent context cannot join it by
sharing the same native PostgreSQL adapter. PostgreSQL and ClickHouse adapters also refuse a
concurrent native operation on the same adapter; use separate connections for concurrent work.
Sequential old/new sessions may still share a borrowed adapter and retain their own write epochs.

## Borrowed adapters

The existing Python constructor and TypeScript `Session.open` borrow already connected adapters.
Closing that Session ends its data API; it does not close the caller's adapters. Immutable model
and placement metadata remain available for diagnostics. Data operations, transactions and
migration helpers refuse a closed session with `ResourceClosed`.

```python
with sde.Session(model, placement, {"pg-main": pg}, project_id=project_id) as session:
    session.save("Event", {"id": 1, "value": 11})
# pg remains the caller's responsibility.
```

```typescript
const session = await Session.open(model, placement, { 'pg-main': pg }, { projectId })
try {
  await session.save('Event', { id: 1n, value: 11 })
} finally {
  await session.close()
}
```

`ResourceBusy` means the resource is already in use or belongs to another active transaction.
Both lifecycle errors are EngineError subclasses. A refusal does not silently wait for a callback
that may itself be waiting on the refused request, and it does not retry against another engine.
Do not use one Session or physical adapter as a concurrent connection pool.

## Owned connections

`Session.connect` takes factories for fresh adapters, connects them, validates the Session and
owns their cleanup. Factories must return distinct adapters dedicated to that unit of work.
The failed adapter and earlier opened adapters are all closed after partial startup failure.
The original startup exception is retained even if cleanup also fails.

```python
from sde.engines.postgres import PostgresEngine

with sde.Session.connect(
    model, placement,
    {"pg-main": lambda: PostgresEngine(local_pg_dsn)},
    project_id=project_id,
) as session:
    session.save("Event", {"id": 1, "value": 11})
```

```typescript
import { PostgresEngine } from '@smart-data-engines/sde/engines/postgres'

const session = await Session.connect(model, placement, {
  'pg-main': () => new PostgresEngine(localPgDsn),
}, { projectId })
try {
  await session.save('Event', { id: 1n, value: 11 })
} finally {
  await session.close()
}
```

Supply a factory for every binding required by the map and signed watermark handshake. Runtime
credentials remain local and restricted; creation of schemas/bookkeeping is still a separate
provisioning step. The factory does not choose placement, create a database, or invent credentials.
Close attempts every owned adapter. Failed closes remain available for explicit close retry;
successfully closed adapters are not closed twice. Concurrent cleanup is refused with ResourceBusy.

## Transaction ownership and nesting

A transaction covers one declared colocation group. Operations outside that active group are
refused before native work. PostgreSQL nesting uses savepoints: an inner success does not commit
the outer transaction, and an inner rollback does not discard successful outer work. Fan-out is
replayed only after the source's outermost successful commit.

Every operation in a transaction must finish before its callback/block exits. An unawaited operation
cannot enqueue another write after rollback or commit. The inherited context is invalidated before
the final native command, so late work receives ResourceClosed. Close cannot end a Session while
its operation or transaction is active. Python adapters/sessions inherited by fork are refused;
create fresh resources in the child, before acquiring any inherited lock.

Catching a statement error does not make PostgreSQL's failed transaction healthy. The adapter
refuses a successful outcome when the native transaction has aborted. An uncertain commit or failed
rollback requires explicit `engine.close()` followed by `engine.connect()` before reuse; no source
write is automatically retried. A lost commit response can mean the row exists, so the application
must reconcile its own intent instead of assuming rollback.

These rules apply through ordinary forwarding adapter wrappers because ownership is checked on
the actual native adapter. Private raw driver handles are outside the managed Session interface.
Use the dedicated operator connections described in the cutover runbook for provisioning/migration.
ClickHouse and the orderbook adapter continue to refuse multi-statement transactions.
