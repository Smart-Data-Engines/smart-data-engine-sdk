# Advance a group's write generation

A signed generation-advance authorization raises one group's write generation by one and changes
nothing else: the group stays in the engines, tables and copies it is in, every other group stays
as it is, and so does routing. The packet holds the signed map in force and the signed map that
follows it, and the second is the first with a newer version and that one number.

The three libraries load and validate the packet by the same rules, pinned by shared vectors.
The Python `LocalCutover.advance` executor runs it in the customer's environment: a barrier on the
group's tables, the next generation behind it, the next map published, the barrier lifted. No row
is read, copied or compared, and no grant changes hands.

## What a generation is a mark of

A [contract-4 map](generation-maps.md) gives every group a write generation, each write carries its
map's generation in `__sde_write_epoch`, and the engine's own constraint admits no other
([write fences](write-fences.md)). A library never changes a row without stamping it with the
generation of the map it holds. So once a group has moved from generation E to E+1:

- every write from then on carries E+1, and
- a row that still carries E or less has not been written through a library since.

That is a point in time the engine itself enforces, in every engine that fences writes, and it is
what an advance is for. A comparison of a source with its copy made after an advance has to settle
the rows of earlier generations once; whatever is written during or after it carries the new
generation and can be found by it. Today a [cutover](local-cutover.md) compares and repairs a whole
group under its pause, because nothing tells it which rows changed since an earlier pass. As with
every fence, the mark is of inserts through the libraries: SQL run beside them is outside it.

An advance moves no row, builds nothing and leaves the placement as it is, so there is nothing in
it to verify and no outcome to choose: the packet has one next map, not a `success` and an `abort`.

## Signed packet protocol 1

```json
{
  "kind": "sde-advance",
  "protocol": 1,
  "advance_id": "<32 lowercase hex>",
  "project_id": "<32 lowercase hex>",
  "group": "Event",
  "pause_budget_ms": 30000,
  "before": { "<the signed map in force>": "..." },
  "after": { "<the signed next map>": "..." },
  "signature": { "alg": "ed25519", "key_id": "...", "value": "..." }
}
```

The signature is Ed25519 over the canonical envelope without its top-level signature, with the
encoding and trusted-key selection of [cutover packets](cutover-packets.md); each nested map carries
its own valid signature. The packet fingerprint is SHA-256 of that canonical unsigned envelope, so
it covers both maps as they were signed. Integral JSON numbers normalize by the existing map rules
before validation.

A loader refuses the packet unless every rule holds:

1. exactly these fields; `kind` `sde-advance`, `protocol` 1; both ids 32 lowercase hexadecimal
   digits; `project_id` the locally configured project; `group` a nonempty string;
2. `pause_budget_ms` is an integer from 1 through 2^53−1;
3. a well-formed signature block on the envelope and on both maps, and valid signatures by the
   caller's keys - the envelope's is verified before either map is read;
4. both maps are contract 4 or later, of the locally configured project, with every layout written
   out: a layout that carries `auto`, whatever its value, or one without columns is refused, in any
   group, because the tables an operator fences are the ones the maps name and nobody signed a
   table a library derived;
5. `after.map_version` is greater than `before.map_version`;
6. `group` is a group of the map in force, and it carries a write generation with one above it
   available: a group whose engine cannot fence writes
   ([contract 6](format-contract.md#7k-a-group-without-a-write-generation-map-contract-6)) has none
   to advance;
7. every other group is byte-identical in the two maps;
8. the group's `write_epoch` in `after` is its `write_epoch` in `before` plus one, exactly;
9. the group is otherwise byte-identical: source, maintained copies and `also_write`;
10. every other top-level attribute is byte-identical: contract, model version, project, routing.

Both maps place exactly the model's groups, which the map loader holds for each map on its own; a
packet cannot add or remove one.

A refusal is `MigrationRefused` and names the rule. A refusal of a map inside the packet, or of an
encoding, keeps the map loader's own words after `advance authorization refused: `.

Load with `load_advance_plan(raw, model=..., project_id=..., public_key=...)` in Python,
`loadAdvancePlan(raw, { model, projectId, publicKey })` in TypeScript, or
`sde::load_advance_plan(raw, model, project_id, public_keys)` in C++. `AdvancePlan` keeps immutable
verified provenance - copying a parsed object does not transfer it - and exposes:

| Python | TypeScript | C++ | |
|---|---|---|---|
| `before`, `after` | `before`, `after` | `before()`, `after()` | the two loaded maps |
| `source_epoch`, `next_epoch` | `sourceEpoch`, `nextEpoch` | `source_epoch()`, `next_epoch()` | the group's generation before the advance and after it |
| `pause_budget_ms` | `pauseBudgetMs` | `pause_budget_ms()` | the budget the packet states |
| `as_record()` | `asRecord()` | `as_record()` | the packet as it was checked |
| `after_payload()` | `afterPayload()` | `after_payload()` | the canonical bytes of the signed next map |
| `check_current(map)` | `checkCurrent(map)` | `check_current(map)` | refuses any map in force but the signed `before` |
| `fingerprint`, `verified_with` | `fingerprint`, `verifiedWith` | `fingerprint()`, `verified_with()` | the packet's fingerprint and the name of the key that verified it |

`ADVANCE_PROTOCOL` is exported by the three libraries. Loading touches no engine, adopts no map and
advances no watermark.

## Shared vectors

`migration/201`-`230` are shared fixtures with `plan.json` and `advance.json`, encoded and signed
with OpenSSL rather than by any implementation (`conformance/tools/advance_vectors.py`). They hold
three acceptances - a group with a maintained copy, a group without one, a map that carries a
group without a generation - and a refusal for each rule, each with the fragment of the message
every library gives.

## Local execution

Use the [operator configuration](local-cutover.md#local-configuration-and-command-handoff) already
enrolled for the project. An advance needs no grant a cutover does not need.

```sh
sde-operator --config local-operator.json --project-dir ./client-state advance --plan advance.json
sde-operator --config local-operator.json --project-dir ./client-state status
sde-operator --config local-operator.json --project-dir ./client-state resume
```

**Before anything waits**, the operator checks, and records an intent that binds:

- the signed map in force;
- a binding for every engine a group with a generation uses, because the next map version is
  recorded in each;
- the native identity of every table of the group, the source's and each copy's - none of them
  one table under two names, and none a table another group also names, whose generation would
  be raised under a map that still says the earlier one;
- each table's write barrier complete, at the group's generation and **without any hold**: an
  advance does not start beside another operation's barrier;
- the runtime logins, and every other grantee of the tables, as a cutover qualifies them
  ([runtime roles](runtime-roles.md#the-operators-login-and-the-principals-beside-it)). An advance
  moves no authority, and the generation it leaves on the rows is a mark a later comparison
  stands on, which a principal nobody declared could change a row under;
- no engine holding a newer map version.

A refusal here has changed nothing.

**Then the group's writes wait**, and the packet's `pause_budget_ms` bounds the wait:

1. a barrier on the source's tables, then on each copy's. A write is the source's first and a
   copy's additionally, so with the source closed first nothing reaches a copy but what was
   already on its way;
2. the barrier read back: every table is the one that was qualified, closed by this advance's
   barrier and by no other, at the generation it started from;
3. the decision `advanced`, written durably;
4. the next generation on the source and on the copies, the next map version recorded in every
   engine, the next map published, and the barrier lifted - from the copies first, so that the
   source takes no write while a copy still refuses it.

It ends by reading that every table of the group is open at the next generation.

### What the application sees

From the first barrier to the last release an insert into the group is refused by the engine, at
either generation: nothing is written at the earlier one after the advance began, nor at the next
before its map is out. Afterwards a session that still holds the earlier map is refused until it
loads the next one, and a library does not retry ([generation maps](generation-maps.md)). Rows
written before the advance keep the generation they were written in. That is the mark.

### Interruption, the pause budget and recovery

An advance has two ends and reaches one of them from wherever it stops.

- **Before the decision** the map in force is still true of every table. An interruption followed
  by `resume`, and a pause budget spent before the decision, lift this advance's own barrier and
  leave that map in force: outcome `abandoned`, with the reason `recovered_before_decision` or
  `budget_exceeded`. Applications still hold that map and write again at once.
- **From the decision on** it is completed: a write fence cannot lower a generation, so there is
  no way back and `resume` goes forward, to the outcome `advanced`. A spent budget does not turn
  a decision into another one.

A deadline equal to the pause budget interrupts a statement on a server that stopped answering.
The command then exits with `recovery_required`, and `resume` with fresh connections ends the
advance one way or the other. After a kill, `resume` first restores what the last statement left:
a ClickHouse table detached by an unfinished barrier is attached again under its recorded intent.
There is no `abandon` for an advance - before the decision `resume` is the abandonment, and after
it nothing can be.

An abandoned advance spends its packet: the barrier's id is retired in the engines, as after an
aborted cutover, and the same packet answers with its receipt. Advancing again takes a new packet.
A barrier that is not this advance's, appearing while it runs, stops the decision, is not lifted
by the recovery, and keeps the advance from reporting an end until it is gone.

### Receipt and local state

The receipt holds `outcome`, `reason`, the version and fingerprint of the map in force afterwards,
the group's `write_epoch`, the tables acted on with their roles, `elapsed_ms`, `recovered` and
`within_budget`. The same packet again returns the same receipt and changes nothing; another
packet under a spent `advance_id` is refused.

`project.json` keeps the history of advances under `advances`, and uses storage contract 6 from an
advance's first intent: operators that know contracts 1 to 5 refuse the state whole, where they
would otherwise resume an advance as if it were a cutover.

## What this does not do

- It does not make a cutover shorter by itself. A cutover reads the whole group under its pause
  until [`sde-operator settle`](local-cutover.md#settling-before-the-pause) has compared the rows
  below the new generation, after the advance and before the cutover.
- It is not free for the application. Raising a generation is what a cutover's first step does: a
  session that still holds the earlier map has its writes refused by the engine from then on, and a
  library does not retry them - the application loads the next map
  ([generation maps](generation-maps.md)).
