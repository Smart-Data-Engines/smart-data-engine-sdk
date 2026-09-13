# Canonical map identifiers

Map payload strings and member names must be Unicode scalar text in NFC. Both SDKs reject a
noncanonical payload during map loading, before returning an instruction. Valid NFC names,
including non-ASCII and astral characters, are retained exactly and work on PostgreSQL and
ClickHouse with restricted runtime credentials.

Canonical signing normalizes strings; SQL object lookup preserves their spelling. The two
operations must agree on the name being authorized. Refusing a noncanonical instruction prevents
the same signature or fingerprint from identifying different physical objects.

## Compatibility

This is a stricter loading boundary for all supported map contracts, including unsigned maps.
The canonical encoding, map versions, signature algorithm and fingerprint format have not changed.
Documents emitted as canonical bytes retain the same bytes and signatures. The private control
plane already emits canonical bytes.

If a hand-written map uses non-NFC physical names, inspect the actual database catalog and engine
bindings before updating it. Converting the map's spelling alone could select a different existing
object or a missing one. Any necessary physical rename or migration is a separate, deliberate
operator procedure; the loader does not perform it. Existing canonical maps need no data rewrite.

The top-level signature block is excluded from the payload. Its `key_id` annotation remains a
hint; the actual verifying key is still determined by trying the trusted key set. No key material,
account enrollment or network operation is introduced by the text check.

## Evidence

A controlled pre-fix probe used isolated tables and restricted logins in both native engines. Two
Unicode spellings produced the same canonical fingerprint and verified signature, while the SDKs
retained different physical names. The regression now refuses the noncanonical instruction.

Eight shared vectors cover canonical acceptance, noncanonical table/engine/member/nested text,
key-hint behavior and invalid scalar text. OpenSSL produced and checked the signatures; the
fixture generator derives the payload without importing either SDK. Per-language tests cover
all four map contracts and preserve the encoder's existing normalization behavior. Native tests
keep signed save/get working for canonical accented and astral table names.


## Acceptance on 13 September 2026

Full suites passed with both engines available: 906 Python tests, with only the 10 optional
orderbook skips, and 404 TypeScript tests without skips. The shared suite now has 181 vector
directories and 194 cases in each runner. The fourteen new per-language parser tests included
twelve failures before the repair; canonical acceptance and the unsigned key-hint control passed.

All twelve intentional mutations were detected by named failures: removing NFC/scalar checks,
omitting member names or arrays, checking only contract 4, and incorrectly treating the unsigned
key hint as instruction text. Exact sources were restored and both 208-case selected suites passed.
Wheel, sdist and npm archives passed inspection; fresh wheel/npm consumers passed all eight new
shared cases without importing the source checkout. No publication or deployment was performed.
