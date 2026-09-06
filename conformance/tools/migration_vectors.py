"""Generate the ``migration/`` vectors: the last set section 10 of the contract recorded as missing.

Tier 2's second half - participation in a migration - and the reason it needed vectors at all is
that everything in it is behaviour against an engine rather than a document transformation. So these
cases pin two things:

- **the record**, which is what a gate on our side reads: a backfill's progress and a verify's seven
  counts;
- **the calls**, which is how the record was obtained. A library that reached the same counts by
  scanning the whole table and filtering in memory would satisfy every count and be unusable on a
  real one. The call sequence is also what makes the shared fixture self-checking: two in-memory
  tables that disagreed would show up here rather than as a plausible wrong answer.

The fixture is :class:`sde.testing.MemoryEngine`, in the library rather than in a runner, because a
runner that writes its own is a runner whose fixture can be the thing that differs.

Two properties this family exists to hold, and both are silent when they break:

**The progress marker is a row count and never a key.** A key resumes exactly and needs a codec in
every language that ever writes an adapter, and a lossy codec puts the resume point *after* rows
nobody copied. ``004`` is the case where the marker is non-zero, and the call it pins is the one that
turns a count back into a position.

**The verify counters split at the marker.** Below it, a mismatch means the backfill did not copy;
above it, the fan-out did not reach. Two mechanisms, two pairs of numbers - and a library that used
one pair would produce a plausible report that sends an operator to the wrong half of the system.

    python conformance/tools/migration_vectors.py --i-am-changing-the-contract
"""

from __future__ import annotations

import argparse
import base64
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "python" / "src"))

import sde  # noqa: E402
from sde.errors import MapRolledBack, MigrationRefused  # noqa: E402
from sde.testing.loader import model_from_neutral  # noqa: E402
from sde.testing.memory import MemoryEngine, engines_from as _engines  # noqa: E402
from sde.watermark import enforce_forward_only  # noqa: E402

VECTORS = ROOT / "conformance" / "vectors" / "migration"

# The 12-byte SubjectPublicKeyInfo prefix an Ed25519 public key carries in DER. Restated rather than
# parsed, exactly as in the signature generator: the point of asking openssl is that this script
# shares no code with the libraries.
DER_PREFIX = bytes.fromhex("302a300506032b6570032100")


def openssl(*args: str, stdin: bytes | None = None) -> bytes:
    result = subprocess.run(["openssl", *args], input=stdin, capture_output=True, check=False)
    if result.returncode != 0:
        raise SystemExit(f"openssl {' '.join(args)} failed: {result.stderr.decode()}")
    return result.stdout


def sign(document: dict[str, Any], home: Path) -> tuple[dict[str, Any], str]:
    """Sign a map with a throwaway key. No private key is ever written into the repository."""
    from sde.canonical import canonical_bytes

    private = home / "key.pem"
    openssl("genpkey", "-algorithm", "ed25519", "-out", str(private))
    der = openssl("pkey", "-in", str(private), "-pubout", "-outform", "DER")
    if not der.startswith(DER_PREFIX) or len(der) != len(DER_PREFIX) + 32:
        raise SystemExit(f"unexpected DER shape: {der.hex()}")
    public = der[len(DER_PREFIX) :]

    payload = canonical_bytes({k: v for k, v in document.items() if k != "signature"})
    message = home / "payload.bin"
    signature = home / "payload.sig"
    message.write_bytes(payload)
    openssl(
        "pkeyutl", "-sign", "-rawin", "-inkey", str(private),
        "-in", str(message), "-out", str(signature),
    )
    signed = {
        **document,
        "signature": {
            "alg": "ed25519",
            "key_id": "issuing",
            "value": base64.b64encode(signature.read_bytes()).decode(),
        },
    }
    return signed, base64.b64encode(public).decode()


def _model() -> sde.LogicalModel:
    """One entity with a single-column integer key, and one with a composite key.

    Two keys because the library hands an engine the **whole** key tuple and the composite case is
    where a hand-rolled pagination goes wrong - by skipping rows. A single column cannot show it.
    """
    sde.clear_registry()
    return model_from_neutral(
        {
            "entities": [
                {
                    "name": "Reading",
                    "fields": [
                        {"name": "id", "type": "int64"},
                        {"name": "celsius", "type": "int32"},
                    ],
                    "key": ["id"],
                },
                {
                    "name": "Sample",
                    "fields": [
                        {"name": "station", "type": "string"},
                        {"name": "seq", "type": "int64"},
                        {"name": "note", "type": "string"},
                    ],
                    "key": ["station", "seq"],
                    "atomic_with": ["Reading"],
                },
            ],
            "relations": [],
            "atomic": [["Reading", "Sample"]],
        }
    )


READING_ROWS = [{"id": index, "celsius": index * 2} for index in range(1, 8)]
SAMPLE_ROWS = [
    {"station": station, "seq": seq, "note": f"{station}-{seq}"}
    for station in ("alpha", "beta")
    for seq in (1, 2)
]


def _layout() -> dict[str, Any]:
    return {
        "tables": {"Reading": "reading", "Sample": "sample"},
        "columns": {
            "Reading": {"id": "bigint", "celsius": "integer"},
            "Sample": {"station": "text", "seq": "bigint", "note": "text"},
        },
    }


def _map(*, also_write: bool, map_version: int = 1) -> dict[str, Any]:
    model = _model()
    group = sde.colocation_groups(model)[0].name
    body: dict[str, Any] = {
        "source": {"id": f"{group}@pg", "engine": "pg-main", "layout": _layout()},
    }
    if also_write:
        body["derived"] = [
            {
                "id": f"{group}@pg2",
                "engine": "pg-copy",
                "layout": _layout(),
                "lag_budget_ms": 30000,
            }
        ]
        body["also_write"] = [f"{group}@pg2"]
    return {
        "contract": sde.MAP_CONTRACT if also_write else sde.CONTRACT,
        "model_version": model.version,
        "map_version": map_version,
        "groups": {group: body},
    }


def _write(name: str, files: dict[str, Any]) -> None:
    out = VECTORS / name
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)
    for filename, body in files.items():
        (out / filename).write_text(json.dumps(body, indent=2) + "\n", encoding="utf-8")
    print(f"  wrote migration/{name}")


def _calls(engines: dict[str, MemoryEngine]) -> list[dict[str, Any]]:
    """One sequence for the whole engine set, each entry naming its engine.

    Per-engine lists could not express the guarantee the dual-write cases are about - a row reaches
    the source before anything is attempted against the copy - and reversing those two lines passed
    every vector while the note in one of them claimed that ordering was pinned.
    """
    journals = {id(engine.recorded) for engine in engines.values()}
    if len(journals) > 1:
        raise SystemExit("the engines do not share one journal, so cross-engine order is not pinned")
    any_engine = next(iter(engines.values()))
    return any_engine.recorded.as_list()


# --- the watermark cases -------------------------------------------------------------------------


def _watermark_case(
    name: str,
    *,
    signed: bool,
    map_version: int,
    spec: dict[str, dict[str, Any]],
    why: str,
    expect_error: str | None = None,
    why_match: tuple[str, ...] = (),
) -> None:
    model = _model()
    document = _map(also_write=False, map_version=map_version)
    files: dict[str, Any] = {"model.json": sde.neutral_declaration(model)}
    public: str | None = None
    if signed:
        with tempfile.TemporaryDirectory() as home:
            document, public = sign(document, Path(home))
    files["map.json"] = document
    if public is not None:
        files["keys.json"] = {"issuing": public}
        files["load.json"] = {"require_signature": True, "public_key": "issuing"}
    placement = sde.load_map(
        document,
        model=model,
        public_key=base64.b64decode(public) if public else None,
        require_signature=bool(public),
    )
    engines = _engines(spec)
    files["engines.json"] = spec
    if expect_error is not None:
        try:
            enforce_forward_only(placement, engines)
        except MapRolledBack as exc:
            if expect_error not in str(exc):
                raise SystemExit(f"the reference said {exc}, not containing {expect_error!r}")
            files["watermark.json"] = {
                "error": "MapRolledBack",
                "match": expect_error,
                "why": why,
            }
        else:
            raise SystemExit(f"{name}: expected a refusal and got none")
    else:
        check = enforce_forward_only(placement, engines)
        for fragment in why_match:
            if fragment not in check.why:
                raise SystemExit(f"{name}: {check.why!r} does not contain {fragment!r}")
        # `why` is prose, so it is pinned by substring like every other diagnostic in this suite -
        # a library may say more, in any language, and may not say less. Everything else in the
        # record is compared exactly, because a protection level and a list of engines are answers
        # rather than explanations.
        record = {key: value for key, value in check.as_record().items() if key != "why"}
        files["watermark.json"] = {
            "expect": record,
            "why_match": list(why_match),
            "why": why,
        }
    files["calls.json"] = _calls(engines)
    _write(name, files)


# --- the backfill and verify cases ---------------------------------------------------------------


def _run_case(
    name: str,
    *,
    spec: dict[str, dict[str, Any]],
    why: str,
    backfill: dict[str, Any] | None = None,
    verify: dict[str, Any] | None = None,
    also_write: bool = True,
) -> None:
    model = _model()
    document = _map(also_write=also_write)
    group = sde.colocation_groups(model)[0].name
    placement = sde.load_map(document, model=model)
    engines = _engines(spec)
    session = sde.Session(model, placement, engines)  # type: ignore[arg-type]

    files: dict[str, Any] = {
        "model.json": sde.neutral_declaration(model),
        "map.json": document,
        "engines.json": spec,
    }

    if backfill is not None:
        options = dict(backfill.get("options") or {})
        expect_error = backfill.get("error")
        if expect_error is not None:
            try:
                sde.backfill(session, group, **options)
            except MigrationRefused as exc:
                if expect_error not in str(exc):
                    raise SystemExit(f"the reference said {exc}, not containing {expect_error!r}")
                files["backfill.json"] = {
                    "group": group,
                    "options": options,
                    "error": "MigrationRefused",
                    "match": expect_error,
                    "why": why,
                }
            else:
                raise SystemExit(f"{name}: expected a refusal and got none")
        else:
            progress = sde.backfill(session, group, **options)
            files["backfill.json"] = {
                "group": group,
                "options": options,
                "progress": progress.as_record(),
                "why": why,
            }

    if verify is not None:
        options = dict(verify.get("options") or {})
        report = sde.verify(session, group, **options)
        files["verify.json"] = {
            "group": group,
            "options": options,
            # A fixed instant, because `at` is the one field of this record that is a clock reading
            # and a vector with a timestamp in it would be a vector that expires.
            "report": {**report.as_record(), "at": "<any>"},
            "matched": report.matched,
            "differences": [
                {
                    "entity": difference.entity,
                    "table": difference.table,
                    "key": dict(difference.key),
                    "columns": list(difference.columns),
                }
                for difference in report.differences
            ],
            "why": why,
        }

    files["calls.json"] = _calls(engines)
    _write(name, files)


# --- the dual-write cases ------------------------------------------------------------------------


def _session_case(
    name: str,
    *,
    spec: dict[str, dict[str, Any]],
    operations: list[dict[str, Any]],
    why: str,
) -> None:
    """Drive a session through a sequence of writes and pin what each engine ended up holding.

    The heart of Tier 2, and the part no document transformation can reach: a fan-out is a second
    write in the client's own process, and every rule about it is about *when* it happens. Deferred
    to commit inside a transaction, dropped when that transaction rolls back, and swallowed when the
    copy refuses - each of those is a place where two languages could differ silently and a client
    would lose rows in one of them.
    """
    model = _model()
    document = _map(also_write=True)
    placement = sde.load_map(document, model=model)
    engines = _engines(spec)
    recorder = sde.Recorder(model.version)
    session = sde.Session(model, placement, engines, recorder=recorder)  # type: ignore[arg-type]

    def run(steps: list[dict[str, Any]]) -> None:
        for step in steps:
            if step["op"] == "save":
                session.save(step["entity"], step["values"])
            elif step["op"] == "transaction":
                try:
                    with session.transaction(*step.get("entities", ())):
                        run(step["body"])
                        if step.get("rollback"):
                            raise _Rollback
                except _Rollback:
                    pass
            else:
                raise SystemExit(f"unknown operation {step['op']!r}")

    run(operations)
    window = recorder.roll()
    group = sde.colocation_groups(model)[0].name
    # The two lag percentiles are **measured elapsed time**, so they are replaced by a placeholder:
    # a vector that pinned them would pin this machine, and it would pass on the runtime that
    # produced it and nowhere else. The runner asserts they are present and are a number or null,
    # which is the part that is a property of the library rather than of the clock.
    copies = [
        {**copy.as_record(), "lag_p50_ms": "<any>", "lag_p99_ms": "<any>"}
        for copy in (() if window is None else window.copies(group))
    ]

    _write(
        name,
        {
            "model.json": sde.neutral_declaration(model),
            "map.json": document,
            "engines.json": spec,
            "operations.json": operations,
            "tables.json": {
                engine: {
                    table: sorted(
                        (dict(row) for row in rows),
                        key=lambda row: json.dumps(row, sort_keys=True),
                    )
                    for table, rows in sorted(built.tables.items())
                }
                for engine, built in sorted(engines.items())
            },
            "copies.json": copies,
            "calls.json": _calls(engines),
            "why.json": {"why": why},
        },
    )


class _Rollback(Exception):
    """Raised by a case that asks its transaction to roll back, and caught by the runner."""


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--i-am-changing-the-contract", action="store_true")
    args = parser.parse_args()
    if not args.i_am_changing_the_contract:
        print(__doc__)
        print("Refusing to run without --i-am-changing-the-contract.")
        return 1
    VECTORS.mkdir(parents=True, exist_ok=True)
    print("migration vectors:")

    _watermark_case(
        "001-an-unsigned-map-is-not-checked-at-all",
        why_match=("this map is unsigned", "no-account mode",),
        signed=False,
        map_version=1,
        spec={"pg-main": {"watermark": 9}},
        why=(
            "An unsigned map is the client's own document, so there is no newest version for us to "
            "be the authority on. The `calls.json` of this case is the load-bearing part and it is "
            "**empty**: the no-account mode promises no table, no query and no cost, so an "
            "implementation that gathered the watermarks and then noticed the map was unsigned "
            "would return the right answer and break that promise - which is the one shape of "
            "defect the decision record cannot show. The TypeScript port did exactly that until "
            "this case existed."
        ),
    )
    _watermark_case(
        "002-a-signed-map-records-its-version-and-equal-is-allowed",
        why_match=("the highest map version applied against these engines is", "sde_map_state", "A map older than that is refused",),
        signed=True,
        map_version=4,
        spec={"pg-main": {"watermark": 4}, "pg-copy": {"watermark": 4}},
        why=(
            "Equal is the ordinary case - a process restarting against the same map - so only "
            "strictly lower is refused. And nothing is written when the version does not move: "
            "recording every start would grow the table by one row per restart and the watermark "
            "would say nothing more than it does now. Both engines are read, because the watermark "
            "is the maximum over all of them: losing an engine cannot lose the protection."
        ),
    )
    _watermark_case(
        "003-a-map-that-goes-backwards-is-refused",
        signed=True,
        map_version=3,
        spec={"pg-main": {"watermark": 5}, "pg-copy": {"watermark": 4}},
        why=(
            "The whole reason this module exists: an older signed map verifies perfectly, so "
            "nothing else in the library would notice that the file was replaced, and the writes "
            "would go to the previous placement. Cryptographic verification is not replay "
            "protection. The maximum is taken across engines, so the engine at 4 does not lower "
            "the bar the engine at 5 set."
        ),
        expect_error="Refusing to go backwards",
    )
    _watermark_case(
        "004-an-engine-that-cannot-keep-bookkeeping-is-reported",
        why_match=("can keep bookkeeping", "nowhere to put it", "does not exist for it",),
        signed=True,
        map_version=2,
        spec={"pg-main": {"bookkeeping": False}},
        why=(
            "An engine whose schema is fixed in its own source has nowhere to put this, so a client "
            "whose only engine is that one has no rollback protection and cannot have any. The "
            "honest maximum is to say so: `unavailable` is a third value, not a `false`, and it is "
            "readable from the session - a protection whose state cannot be read is a protection "
            "taken on trust."
        ),
    )
    _watermark_case(
        "005-the-first-map-a-fresh-engine-sees-is-recorded",
        why_match=("the highest map version applied against these engines is", "sde_map_state",),
        signed=True,
        map_version=7,
        spec={"pg-main": {"watermark": None}},
        why=(
            "Nothing has been applied yet, so there is no bar to be under and the version is "
            "written. `highest_seen` is null rather than zero: zero would be a map version, and a "
            "map version nobody issued is worse than an absence."
        ),
    )

    _run_case(
        "006-a-map-with-no-fan-out-is-not-a-migration",
        also_write=False,
        spec={"pg-main": {"tables": {"reading": READING_ROWS, "sample": SAMPLE_ROWS}}},
        backfill={"error": "has no fan-out target in this map"},
        why=(
            "A migration reaches a library as a placement map with `also_write` and nothing else - "
            "there is no phase name in the document and no second channel - so a map without that "
            "key says this group is not being migrated. Refused rather than answered with an empty "
            "progress record, because a backfill that copies nothing and reports success is the "
            "worst outcome this module has available."
        ),
    )
    _run_case(
        "007-a-backfill-copies-in-chunks-and-marks-after-each",
        spec={
            "pg-main": {"tables": {"reading": READING_ROWS, "sample": SAMPLE_ROWS}},
            "pg-copy": {"tables": {}},
        },
        backfill={"options": {"chunk_rows": 3}},
        why=(
            "The order in `calls.json` is the point: for each chunk, `copy_in` then "
            "`record_backfill_marker`, never the other way round. A crash between them costs a "
            "recopy, which the target's key semantics absorb; the other order costs the chunk, "
            "permanently. The last chunk of seven rows at three per chunk comes back **short**, "
            "which is what ends the loop - reaching the end of the table once is enough, because "
            "dual write precedes backfill and rows above that point are the fan-out's. The "
            "composite-key entity shows the whole key tuple travelling in `after`."
        ),
    )
    _run_case(
        "008-a-backfill-resumes-from-a-row-count-not-a-key",
        spec={
            "pg-main": {"tables": {"reading": READING_ROWS, "sample": SAMPLE_ROWS}},
            "pg-copy": {
                "tables": {"reading": READING_ROWS[:4], "sample": SAMPLE_ROWS[:2]},
                "markers": {"Reading@pg2|Reading": 4, "Reading@pg2|Sample": 2},
            },
        },
        backfill={"options": {"chunk_rows": 2}},
        why=(
            "The marker is a row **count**, so resuming asks the source for the key of row N - the "
            "`nth_key` call in this sequence - and carries on strictly after it. A key marker would "
            "resume exactly and would need a codec in every language that writes an adapter, and a "
            "lossy round trip there puts the resume point *after* rows nobody copied: silent data "
            "loss, different per language. The `OFFSET`-shaped scan is paid once per resume rather "
            "than once per chunk, which is the trade that makes the count acceptable."
        ),
    )
    _run_case(
        "009-a-source-that-has-shrunk-refuses-to-resume",
        spec={
            "pg-main": {"tables": {"reading": READING_ROWS[:2], "sample": SAMPLE_ROWS}},
            "pg-copy": {"tables": {}, "markers": {"Reading@pg2|Reading": 5}},
        },
        backfill={"error": "does not have that many"},
        why=(
            "Rows left the source outside this library, so the marker describes a table that no "
            "longer exists and resuming from it would be guessing. The refusal says nothing has "
            "been copied by this call, which matters: an operator reading it needs to know whether "
            "to expect a partial chunk."
        ),
    )
    _run_case(
        "010-verify-splits-its-counters-at-the-marker",
        spec={
            "pg-main": {"tables": {"reading": READING_ROWS, "sample": SAMPLE_ROWS}},
            "pg-copy": {
                "tables": {"reading": READING_ROWS[:4], "sample": SAMPLE_ROWS},
                "markers": {"Reading@pg2|Reading": 4, "Reading@pg2|Sample": 4},
            },
        },
        verify={"options": {"chunk_rows": 2}},
        why=(
            "Below the marker the copy matches, so `chunks_compared` moves and "
            "`chunks_mismatched` does not. Above it the three uncopied rows are read as tail and "
            "reported as missing - which is correct and is **not** a failure of the backfill: they "
            "are rows the fan-out would have carried in a real migration. Two pairs of counters "
            "because two mechanisms can fail, and a single pair would send an operator to the wrong "
            "half of the system. `rows_source` and `rows_target` are reported and not gated on: two "
            "counts of live tables are taken at different instants."
        ),
    )
    _run_case(
        "011-verify-names-the-column-that-differs",
        spec={
            "pg-main": {"tables": {"reading": READING_ROWS, "sample": SAMPLE_ROWS}},
            "pg-copy": {
                "tables": {
                    "reading": [
                        *READING_ROWS[:3],
                        {**(READING_ROWS[3]), "celsius": -1},
                        *READING_ROWS[4:],
                    ],
                    "sample": SAMPLE_ROWS,
                },
                "markers": {"Reading@pg2|Reading": 7, "Reading@pg2|Sample": 4},
            },
        },
        verify={"options": {"chunk_rows": 3}},
        why=(
            "Compared value by value rather than by digest, and the reason is diagnostics: both "
            "copies are read by one process on one machine, so a checksum would compress a "
            "comparison that costs nothing to do exactly - and an exact comparison can say *which "
            "column* differs, which is the difference between an operator who can fix a migration "
            "and one who can only stop it. The differing row is in the chunk that is counted as "
            "mismatched, and the `differences` list is **not** part of the record that crosses the "
            "boundary: it holds the client's own key values."
        ),
    )
    _run_case(
        "012-an-engine-that-cannot-migrate-is-a-named-refusal",
        spec={
            "pg-main": {"tables": {"reading": READING_ROWS, "sample": SAMPLE_ROWS}},
            "pg-copy": {"tables": {}, "migratable": False},
        },
        backfill={"error": "cannot act as the target of a migration"},
        why=(
            "Refused by name rather than skipped. An engine whose schema is fixed in its own source "
            "has nowhere to keep a progress marker and no table to scan in key order, so this is a "
            "property of the engine rather than a missing feature - and a migration that quietly "
            "copies nothing is the worst thing this module could do. Asked as 'will this object "
            "answer these calls' rather than by type, because a client wrapping one of our adapters "
            "for metrics or retries must not be refused for a property of their wrapper."
        ),
    )
    _session_case(
        "013-a-fan-out-writes-the-source-first-and-then-the-copy",
        spec={"pg-main": {"tables": {}}, "pg-copy": {"tables": {}}},
        operations=[
            {"op": "save", "entity": "Reading", "values": {"id": 1, "celsius": 20}},
            {"op": "save", "entity": "Reading", "values": {"id": 2, "celsius": 21}},
        ],
        why=(
            "The order in `calls.json` is the guarantee: the source, then the copy, per row. The "
            "copy is additional and never authoritative, so a reader can see from this sequence "
            "that a row is in the source before anything is attempted against the copy. The "
            "fan-out is also inside the timed region the telemetry records, which is a decision - "
            "it makes the client's write slower and the measurement has to say so, or a placement "
            "would be scored against a latency the application is not experiencing."
        ),
    )
    _session_case(
        "014-a-fan-out-inside-a-transaction-waits-for-the-commit",
        spec={"pg-main": {"tables": {}}, "pg-copy": {"tables": {}}},
        operations=[
            {
                "op": "transaction",
                "entities": ["Reading"],
                "body": [
                    {"op": "save", "entity": "Reading", "values": {"id": 1, "celsius": 20}},
                    {"op": "save", "entity": "Reading", "values": {"id": 2, "celsius": 21}},
                ],
            }
        ],
        why=(
            "Both inserts into the source come before either insert into the copy, which is what "
            "'deferred to commit' means in a call sequence. Inline would be wrong: the target is a "
            "different engine, so it is outside the source's transaction, and a rolled-back row "
            "would exist in the copy - after the switch, a row the client explicitly undid, "
            "readable. Skipping would be wrong more quietly: those rows are above the backfill "
            "marker, so nothing else copies them, and verify's tail check would then refuse the "
            "migration of every group that uses a transaction."
        ),
    )
    _session_case(
        "015-a-rolled-back-transaction-never-reaches-the-copy",
        spec={"pg-main": {"tables": {}}, "pg-copy": {"tables": {}}},
        operations=[
            {"op": "save", "entity": "Reading", "values": {"id": 1, "celsius": 20}},
            {
                "op": "transaction",
                "entities": ["Reading"],
                "rollback": True,
                "body": [
                    {"op": "save", "entity": "Reading", "values": {"id": 9, "celsius": 99}}
                ],
            },
        ],
        why=(
            "The rolled-back row never existed in the source, so it must never exist in the copy - "
            "dropping it is the whole reason the fan-out was deferred rather than done inline. "
            "`calls.json` shows the insert reaching the source (and being undone by the "
            "transaction) and **no** insert reaching the copy for it. The row saved before the "
            "transaction is in both, which is what makes this case a comparison rather than an "
            "absence."
        ),
    )
    _session_case(
        "016-a-copy-that-refuses-a-write-does-not-fail-the-caller",
        spec={
            "pg-main": {"tables": {}},
            "pg-copy": {"tables": {}, "fail_inserts": {"reading": 1}},
        },
        operations=[
            {"op": "save", "entity": "Reading", "values": {"id": 1, "celsius": 20}},
            {"op": "save", "entity": "Reading", "values": {"id": 2, "celsius": 21}},
        ],
        why=(
            "The copy refuses the first row and the client's `save` returns normally: the row is in "
            "the source, which is the copy that counts, and turning a migration into an application "
            "outage would make the safest thing this product does the most dangerous. The "
            "divergence is not lost - `copies.json` carries it as a **failure** rather than as "
            "lateness, because a copy missing a thousand rows can have an excellent p99, and "
            "verify is the gate that refuses to switch reads while any remain. Two writes and one "
            "failure, so `complete` is false."
        ),
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
