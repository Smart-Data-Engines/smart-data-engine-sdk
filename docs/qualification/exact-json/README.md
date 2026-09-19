# Exact ClickHouse JSON numbers

The installed TypeScript SDK at `239f3e5` rounded an independently written and Python-verified
Decimal(38,18) before its decoder inspected the type. The native JSON token was already lossy
after `JSON.parse`; converting that JavaScript number to a string did not repair it. On
ClickHouse26.8, changed default integer formatting also rounded a key above2^53.

[Acceptance](acceptance.json) records the synthetic witnesses, exact corrected npm artifact and
both native server versions. On Amazon Linux ARM64/Node22.23.2, the installed artifact preserved
the full decimal, integer key and trailing zeros. Native regressions additionally cover negative
values, nullable fields, point/range reads, migration keysets and restricted read-only profiles.

The three output settings are pinned per JSON request; the decoder refuses unquoted exact
numeric values. A readonly1 profile with matching defaults works. A profile locking incompatible
values is refused; SELECT-only grants and compatible profile settings remain separate controls.
See the [connection guide](../../engine-connections.md).

[Five mutations](mutations.json) each changed a unique source span and were detected by an
assertion, with12-case positive controls before and after. Exact source bytes and original
mtime were restored after every mutation. Numeric transport and defensive decoding were tested
separately; no failing fixture or collection error was counted as a detected mutation.

This record establishes the tested correction, not a benchmark result. The application workload,
arrival model, engine durability and hardware qualification remain separate measurements.
