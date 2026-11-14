# Upgrading

One section for each release that can make a working application fail or answer differently, with
who is affected and what to do. The [changelog](../CHANGELOG.md) has the whole account of each
change and the evidence behind it; this page is the part you act on. Each item names the changelog
entry it comes from, and `python/tests/test_upgrading.py` holds the two together: an entry the
changelog marks as breaking has to be named here, and a title named here has to be in the changelog.

What a release of which number may change is in
[`compatibility.md`](compatibility.md#what-a-release-may-change). The page starts at 0.2.0: what
changed before it is in the changelog alone.

## From 0.2.0 to the next release

`main` after `smart-data-engine-sdk` 0.2.0 and `@smart-data-engines/sde` 0.2.0. The next release of
both takes the number 0.3.0, since it refuses input that 0.2.0 accepted.

**What does not change.** Both read placement maps of contracts 1 to 6 and the signed packets of the
same protocols as 0.2.0 does ([`compatibility.md`](compatibility.md#maps-and-signed-packets)). No `schema/`
vector that 0.2.0 passed has changed, so the tables the library creates on PostgreSQL and ClickHouse
are the ones it created before. Nothing is rewritten in your engines by installing the new version.

**During a rollout** services on 0.2.0 and on the new version open the same map and the same
tables. What differs between them is listed below: which connection strings, values and keys each
one refuses, and how a `float32` field reads.

### Connection strings

**Plain transport to a host other than the loopback is refused unless the connection string says
so.** Affected: every deployment whose engine is on another host and whose DSN does not mention TLS.
The adapter refuses before it connects, and the message names the setting.

| Engine | Use TLS | Or state plaintext, on a network you trust |
|---|---|---|
| PostgreSQL | `sslmode=verify-full` | `sslmode=disable`, or `PGSSLMODE=disable` in the environment |
| ClickHouse | `https://`, `clickhouses://` or `secure=true` | `secure=false` |
| the orderbook engine | `tls=on`, with `ca=` for a private CA | `tls=off` |

A PostgreSQL DSN with any `sslmode` is accepted, since the decision is written down. With several
hosts in one DSN, every one outside the loopback needs it. The loopback is `localhost`, 127.0.0.0/8,
`::1`, and 127.0.0.0/8 mapped into IPv6; an address with a zone (`::1%lo`) is not one. A PostgreSQL
Unix socket also counts. Given field by field rather than as a DSN, an orderbook engine whose `tls`
is left out is refused the same way: pass `tls=False` (`tls: false`) or turn it on.
The table for every engine is in
[`engine-connections.md`](engine-connections.md#plaintext-only-when-the-dsn-says-so).

Changelog: *Changed, and it may break a configuration: plaintext to a host other than the loopback
only when the DSN says so* and *Changed, and it may break a configuration: the orderbook engine's
connection states its TLS too*.

**An `orderbook://` DSN is read by one grammar.** Affected: a DSN written by hand with characters a
URL parser used to tolerate. Each is refused with a message naming the part:
- a space, a control character or a character beyond ASCII, raw: percent-encode it. A trailing
  newline is refused, not trimmed;
- a `#` anywhere: a `#` in a secret is `%23`;
- an identity outside `[A-Za-z0-9_.:-]{1,64}`, a `%` without two hexadecimal digits, or credentials
  that are not UTF-8;
- a host that is not a name of letters, digits, `.`, `-` and `_` or a bracketed IPv6 address without
  a zone, and port 0;
- a timeout that is not decimal digits from 0.001 to 2147483.647 seconds: `inf`, `1e3`, `.5`, `1_0`;
- an empty `ca`, and a `ca` together with `verify=off`.

Changelog: *Changed: the orderbook DSN is read by one grammar in every library*.

**A ClickHouse account under a `readonly=1` profile, used from TypeScript,** has to allow one more
setting. The adapter pins `output_format_json_quote_denormals` on every JSON exchange, beside the
three it already pinned, and a profile that fixes them has to fix the fourth to the same value
([`engine-connections.md`](engine-connections.md)).

Changelog: *Fixed: NaN is NaN when a copy is verified, and TypeScript reads it back from ClickHouse*.

**TypeScript sends a ClickHouse statement as the request's body, not in the URL.** Nothing to do in
the application. A proxy or a firewall rule that read the `query` parameter of the URL now sees none
for a read: the statement, with the values it filters on, is in the body.

Changelog: *Fixed: TypeScript sent each ClickHouse statement in the URL, values included*.

### Values you write

**A value its field's type does not hold is refused before any engine is called.** Affected: code
that relied on a driver's or an engine's conversion. Until now each engine answered such a value in
its own way: rounded, truncated, wrapped or stored as another day. The refusal is a
`ModelPlanningError` naming the entity, the field, the type and what does not fit, never the value.
- A Python `float`, or a TypeScript `number` with a fraction, for a `decimal` field: pass a
  `Decimal`, or the decimal as text.
- A decimal with more fractional digits than its column, or past its precision: round it yourself,
  to the scale the model declares.
- A boolean for an integer field, and text for a number: pass the number.
- A `datetime` (Python) or a `Date` (TypeScript) for a `date` field: pass the day.
- An integer outside its type, a date that does not exist (`2026-02-30`), and a number a `float32`
  field would round to an infinity or to zero.

A `json` field is not checked. From TypeScript, `NaN` and the infinities are now written to
ClickHouse as those values; the `null` JSON has for them had been stored as NULL, or as 0 in a
required column.

Changelog: *Fixed: a value its field's type does not hold is refused before any engine*.

**Text holds no U+0000.** Affected: an application that stored a zero character in a `string` field
on ClickHouse. PostgreSQL's `text` cannot hold one, so such a group could not be moved there. A row
with one is refused before any engine, and so is a filter. Binary data with a zero byte belongs in a
`bytes` field.

Changelog: *Changed: text holds no U+0000*.

**A day or an instant outside the server's range is refused.** Affected: rows dated before 1900 or
after 2299 written to ClickHouse 24.8, which stored them moved to the nearer end of its range
without an error. A session now refuses such a row with `EngineError` before it writes to any
engine, the copies included, and a migration refuses a chunk its target cannot hold. ClickHouse 26.9
holds the contract's whole range. Rows stored before the upgrade keep the day they were moved to:
look for them at the two ends of the range, 1900-01-01 and 2299-12-31.

Changelog: *Fixed: a day or an instant outside the server's range is refused before it is sent*.

### Values you read

**A point read's key is held to the rules of a written row.** Affected: `Session.get` with a key
value in another form than its field's type, which the engines used to convert and each differently:
text for an integer key, `1.234` for a `decimal(12,2)` key, a boolean for an integer. The call is
refused with `ModelPlanningError` before any engine. Pass the key as the type the model declares.

Changelog: *Changed: a point read's key is held to a written row's rules*.

**A `float32` field reads as the float32 it holds.** Affected: code that compares a value read from
a `float32` field with a literal, or prints it. PostgreSQL in Python, and PostgreSQL and ClickHouse
in TypeScript, returned the shortest decimal that rounds back to the stored float32: `0.1`. They now
return the stored value exactly, as Python's ClickHouse adapter already did: `0.10000000149011612`.
Stored values are unchanged. A `float64` field reads as before.

Changelog: *Fixed: a float32 reads as the float32 it is, from every engine and in both languages.*

**A decimal or a timestamp given as text in a filter is read by one rule.** Affected: text with
unusual whitespace or an unusual offset. Around a decimal, Unicode's `White_Space` is trimmed and
nothing else: Python no longer trims U+001C to U+001F, TypeScript no longer trims U+FEFF and now
trims U+0085. A timestamp's offset part past 59 (`+10:60`) and `24:00` are refused.

Changelog: *Tightened: one rule for a read value written as text*.

**An entity the model does not declare is refused in one way.** Affected: code that catches the old
error. `save`, `save_many` and `get` naming such an entity raise `ModelPlanningError`
(`this model has no entity '<name>'`); in Python, `get` used to raise a bare `KeyError`.

Changelog: *Fixed: an entity the model does not declare is refused in one way*.

### Maps and declarations written by hand

A map issued by the control plane is not affected by either item here.

**A model and a map have the shapes the contract writes.** Affected: a hand-written declaration or
map that relied on a coercion. Each is refused at load with the rule it breaks, alike in every
library. In a declaration: `nullable` that is not a boolean, `residency` that is not a string, a
relation that is not three strings, atomic groups that overlap or have one member, a `cost_ceiling`
that is not `{amount, currency}` strings. In a map: a materialisation that is not an object or whose
`id` or `engine` is not a string, a lag budget that is not a non-negative integer, layout tables and
columns that are not names and types, `derived` that is not a list, empty `groups`, an uppercase
`project_id`, a write shape routed at a copy, and a signature value that is not base64.

Changelog: *Tightened: a model and a map have the shapes the contract writes*.

**A temporal column keeps its field's zone.** Affected: a hand-written layout that gives a
`timestamptz` field a column type without a zone, or a `timestamp` field one with a zone. A session
refuses such a layout when it opens, with `MapError`, since one row read as two types. Write the
column type the library renders: for ClickHouse `DateTime64(6, 'UTC')` for a `timestamptz` field.

Changelog: *Fixed: a temporal column keeps its field's zone*.

### Messages, events and alerts

Affected: an alert or a script that matches on a message's text or counts an event.
- `sde.schema.extra_columns` no longer names `__sde_write_epoch`, the library's own column, so an
  alert on that event stops firing at every start of a deployment with write generations. A column
  you add is still named.
- A ClickHouse transport failure names the classes of its causes:
  `ClickHouse transport failed (ProtocolError <- RemoteDisconnected); …`.
- A staging, a cutover or an index build that finds a newer map on an engine names the engine
  binding and both versions.
- In Python, a refused staging packet says "staging" where it said "cutover".
- In Python, the orderbook adapter refuses a write with no connection with
  `not connected; call connect() first`, where it reported a write whose outcome was unknown.

Changelog: *Fixed: `sde.schema.extra_columns` named the library's own generation column*, *Fixed: a
ClickHouse request is not sent on a connection the server is closing, and a transport failure names
its causes*, *Changed: an operation a newer map overtook says where and which versions.*, *Fixed: a
staging packet's refusals name a staging* and *Fixed: the orderbook adapter's connection, as the C++
port found it*.

### Marked breaking in the changelog, and nothing to do from 0.2.0

*New: the local operator moves a group onto Elasticsearch and off it* is breaking only for maps that
place a group on Elasticsearch without a write generation. Elasticsearch is not in 0.2.0, and no
release wrote such a map.

### After the upgrade

Open a session on each service's map in a staging environment first: every refusal above that a map,
a layout or a connection string can cause happens when the session opens or when the engine is
constructed, before a row is read or written. The refusals of a value and of a key happen at the
call that passes it, so they need the application's own tests. The local operator's commands work as they
did: `depart` and `erase` are new, and its configuration gains one optional key, `chunk_rows`
([`local-cutover.md`](local-cutover.md)).
