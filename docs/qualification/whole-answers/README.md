# An answer is the whole answer, or an error

The libraries and the operator take an engine's answer without an error for the whole answer. A
count, a page of a scan, a row read back, the comparison a cutover decides by: all of them stand on
it. This record measures where it was not true - where an engine handed back part of an answer and
said nothing - and what the libraries do about each since.

Measured on 14 November 2026 against throwaway servers on one laptop: ClickHouse 24.8.14.39 and
26.9.27.3 (the two releases CI runs) and PostgreSQL 15.19, over plain HTTP and `psql`, so nothing a
driver decides is in the numbers.

## A ClickHouse login's settings

[`probe_login_settings.py`](probe_login_settings.py) gives a login a settings profile and reads a
table of 200 000 rows in ten parts as that login: once as the statement is, once with the settings
the libraries now send with every statement
([`login-settings-24.8.json`](login-settings-24.8.json),
[`login-settings-26.9.json`](login-settings-26.9.json)). Where the two releases answered
differently, both are given.

| the login's profile | rows, as it is | `count()`, as it is | rows, saying so | |
|---|---|---|---|---|
| nothing | 200 000 | 200 000 | 200 000 | |
| `max_result_rows = 1000`, `result_overflow_mode = 'break'` | **48 193 on 24.8, 20 000 on 26.9, no error** | 200 000 | error 396 | |
| the same, the mode `CONST` | **the same, no error** | 200 000 | error 452, naming the setting | |
| the same, `readonly = 1` | **the same, no error** | 200 000 | error 164, naming the setting | |
| `readonly = 1`, the server's own modes | 200 000 | 200 000 | 200 000 | a value equal to the login's own is accepted |
| `max_rows_to_read = 50000`, `read_overflow_mode = 'break'` | 200 000 | 200 000 on 24.8; **120 000 on 26.9, no error** | error 158 | what this cap cuts depends on the table's parts and on the release |
| `max_execution_time = 0.001`, `timeout_overflow_mode = 'break'` | **0, no error** | **no row at all** | error 159 | |
| `limit = 10` | **10, no error** | 200 000 | 200 000 | |
| `limit = 10`, `readonly = 1` | **10, no error** | 200 000 | error 164 on 24.8; 200 000 on 26.9 | 26.9 lets a read-only login change `limit` and `offset` |
| `offset = 199995` | **5, no error** | **no row at all** | 200 000 | |
| `additional_result_filter = 'id < 10'` | **10, no error** | error 47 | 200 000 | |

"Saying so" is eleven overflow modes set to `throw` - every one either release has - and `limit`,
`offset`, `additional_table_filters` and `additional_result_filter` set to nothing. A limit is then
the server's error, and a login that may not say so and whose own value is another is refused by
the setting's name. Three things follow from the table and are in the libraries now:

- **The settings travel with every statement**, an application's too, in all three libraries
  (`WHOLE_ANSWERS` in each ClickHouse adapter). Nothing is asked of the server first: under the
  time cap above, the question itself - a read of `system.settings` - came back empty.
- **A `count()` answered no row at all is an error.** It was read as 0.
- **A login that cannot say so is refused when it connects**, with the setting's name and what to
  change.

## An error after the answer has begun

A statement can fail after its first rows are on their way: a limit under the server's own mode,
`throw`, a timeout, a memory limit, a cancelled query. The status has been sent by then. The same
probe caps a result of 20 000 rows at 5000, in blocks of 100:

| | 24.8 | 26.9 |
|---|---|---|
| `FORMAT JSON`, as the statement is sent | **HTTP 200**, a valid document with 5000 rows in `data` and an `exception` member beside them | HTTP 200, then the body cut before its last chunk |
| the same with `wait_end_of_query=1` | HTTP 500 and the error | HTTP 500 and the error |

The TypeScript library reads `FORMAT JSON` without `wait_end_of_query`, and until this change read
such an answer for its `data` alone: on 24.8 the first rows of a table came back as the table, and
on 26.9 the read never ended, since nothing was listening for an answer that stops. It refuses
both now: an answer that carries an `exception`, or that ends in the server's error as text, is that
error, and an answer the server stops sending fails the read. The Python library's driver and the
C++ library ask for `wait_end_of_query=1` and always did, so the error reached them as a status.

## Row policies

A policy that applies to a login answers it the rows it lets through and no error:
[`probe_row_policies.py`](probe_row_policies.py) on ClickHouse (10 rows of 100 for a policy on the
login, on a role it holds, or on all but another login;
[`row-policies-24.8.json`](row-policies-24.8.json), [`row-policies-26.9.json`](row-policies-26.9.json))
and [`probe_postgres_row_security.sql`](probe_postgres_row_security.sql) on PostgreSQL (10 rows of
100 for the owner of a table with `FORCE ROW LEVEL SECURITY`;
[`postgres-row-security-15.txt`](postgres-row-security-15.txt)).

The local operator already refused a table with a policy, where it takes the table's identity: a
copy would not carry the policy. Nothing tested that; `test_operator_whole_tables_live.py` does
now, and a backfill takes its tables' identity as a cutover does. One thing is new. With
`row_security = off`, PostgreSQL refuses a statement a policy would narrow (`query would be
affected by row-level security policy`) instead of answering part of it, and the operator's
connections are opened that way: what holds if a policy appears after the identity was taken.
ClickHouse has no such setting. The probe also records that the plan of a login's own read names
the policy's filter exactly where one narrows it, under a different name in each release; the
operator does not use that, since the catalogue it already reads says more.

## What this does not show

- Every setting of a ClickHouse profile that can change an answer. These are the ones measured to
  shorten one without an error; a session's time zone, sampling over replicas and the like are not
  here.
- Elasticsearch. A search there is sent with `allow_partial_search_results=false` and a timed-out
  one is refused, since the adapter was written; document-level security of an operator's key was
  not measured.
- A profile or a policy changed while an operation runs. The settings are sent with each
  statement, so a profile cannot take them back; a policy added to a table after its identity was
  taken is seen at the next one.

## Reproducing

```bash
docker run -d --name probe -p 127.0.0.1:58124:8123 -e CLICKHOUSE_USER=default \
    -e CLICKHOUSE_PASSWORD=probe -e CLICKHOUSE_DEFAULT_ACCESS_MANAGEMENT=1 \
    -e CLICKHOUSE_DB=probe clickhouse/clickhouse-server:24.8-alpine
python3 probe_login_settings.py http://127.0.0.1:58124 default probe > login-settings-24.8.json
python3 probe_row_policies.py http://127.0.0.1:58124 default probe > row-policies-24.8.json

docker run -d --name probe-pg -e POSTGRES_PASSWORD=probe postgres:15-alpine
docker exec -i probe-pg psql -U postgres -At < probe_postgres_row_security.sql \
    > postgres-row-security-15.txt 2>&1
```

`SHA256SUMS` lists every file here but this one.
