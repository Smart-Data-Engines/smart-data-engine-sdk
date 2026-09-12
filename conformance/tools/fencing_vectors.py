#!/usr/bin/env python3
"""Write native-fencing fixtures from their specified states/traces, without importing either SDK.

No model or row fixture is involved. These cases exercise the migration control primitive, using
an observed metadata snapshot and a recording DDL backend; real engine effects have separate tests.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

P = "1" * 32
H = "2" * 32
OTHER = "3" * 32
PREFIX = "__sde_f_"
COLUMN = "__sde_write_epoch"
TABLE = "events"


def metadata(
    epoch: int | None = None, *, holds: tuple[str, ...] = (), retired: tuple[str, ...] = ()
) -> dict[str, Any]:
    constraints: dict[str, str] = {}
    if epoch is not None:
        constraints = {
            PREFIX + "owner_" + P: "1",
            PREFIX + f"min_{epoch}": f"{COLUMN} >= {epoch}",
            PREFIX + f"max_{epoch}": f"{COLUMN} <= {epoch}",
        }
    constraints.update({PREFIX + "hold_" + h: "0" for h in holds})
    constraints.update({PREFIX + "retired_" + h: "1" for h in retired})
    return {
        "identity": "table-identity",
        "column": "absent" if epoch is None else "valid",
        "constraints": constraints,
    }


def state(
    epoch: int, *, holds: tuple[str, ...] = (), retired: tuple[str, ...] = ()
) -> dict[str, Any]:
    return {
        "identity": "table-identity",
        "project_id": P,
        "column": "valid",
        "lower_epoch": epoch,
        "upper_epoch": epoch,
        "holds": sorted(holds),
        "retired": sorted(retired),
        "closed": bool(holds),
    }


def add(suffix: str, expression: str) -> list[str]:
    return ["add", TABLE, PREFIX + suffix, expression]


def drop(suffix: str) -> list[str]:
    return ["drop", TABLE, PREFIX + suffix]


def prepare(epoch: int) -> list[list[str]]:
    return [
        add("owner_" + P, "1"),
        add("setup", "0"),
        ["column", TABLE],
        add(f"min_{epoch}", f"{COLUMN} >= {epoch}"),
        add(f"max_{epoch}", f"{COLUMN} <= {epoch}"),
        ["drain", TABLE, P, "setup"],
        drop("setup"),
    ]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--i-am-changing-the-contract", action="store_true", required=True)
    parser.parse_args()
    root = Path(__file__).resolve().parents[1] / "vectors" / "migration"
    cases: list[tuple[str, dict[str, Any], list[dict[str, Any]], list[list[str]], str]] = []

    def case(
        name: str,
        initial: dict[str, Any],
        steps: list[dict[str, Any]],
        calls: list[list[str]],
        project: str = P,
    ) -> None:
        cases.append((name, initial, steps, calls, project))

    def refusal(op: str, match: str, **arguments: Any) -> dict[str, Any]:
        return {"op": op, **arguments, "error": "MigrationRefused", "match": match}

    case(
        "046-write-fence-lifecycle",
        metadata(),
        [
            {"op": "prepare", "epoch": 1, "state": state(1)},
            {"op": "freeze", "request_id": H, "state": state(1, holds=(H,))},
            {"op": "advance", "epoch": 2, "state": state(2, holds=(H,))},
            {"op": "release", "request_id": H, "state": state(2, retired=(H,))},
        ],
        [
            *prepare(1),
            add("hold_" + H, "0"),
            ["drain", TABLE, P, H],
            add("min_2", f"{COLUMN} >= 2"),
            add("max_2", f"{COLUMN} <= 2"),
            drop("max_1"),
            drop("min_1"),
            add("retired_" + H, "1"),
            drop("hold_" + H),
        ],
    )
    case(
        "047-write-fence-refuses-another-project",
        metadata(1),
        [refusal("prepare", "another project", epoch=1)],
        [],
        OTHER,
    )
    bad = metadata(1)
    bad["constraints"][PREFIX + "min_1"] = 'CHECK (("__sde_write_ epoch" >= 1)) NOT VALID'
    case(
        "048-write-fence-checks-the-predicate", bad, [refusal("state", "unexpected predicate")], []
    )
    case(
        "049-write-fence-needs-a-barrier-to-advance",
        metadata(1),
        [refusal("advance", "named write barrier", epoch=2)],
        [],
    )
    case(
        "050-write-fence-never-lowers-its-epoch",
        metadata(2, holds=(H,)),
        [refusal("advance", "backwards", epoch=1)],
        [],
    )
    case(
        "051-write-fence-releases-only-one-hold",
        metadata(1, holds=(H, OTHER)),
        [{"op": "release", "request_id": H, "state": state(1, holds=(OTHER,), retired=(H,))}],
        [add("retired_" + H, "1"), drop("hold_" + H)],
    )
    case(
        "052-write-fence-retry-drains-again",
        metadata(1, holds=(H,)),
        [{"op": "freeze", "request_id": H, "state": state(1, holds=(H,))}],
        [add("hold_" + H, "0"), ["drain", TABLE, P, H]],
    )
    partial = metadata(1, holds=(H,))
    partial["constraints"].update(
        {PREFIX + "min_2": f"{COLUMN} >= 2", PREFIX + "max_2": f"{COLUMN} <= 2"}
    )
    case(
        "053-write-fence-will-not-release-a-partial-change",
        partial,
        [refusal("release", "incomplete epoch change", request_id=H)],
        [],
    )
    case(
        "054-write-fence-resumes-a-partial-change",
        partial,
        [{"op": "advance", "epoch": 2, "state": state(2, holds=(H,))}],
        [
            add("min_2", f"{COLUMN} >= 2"),
            add("max_2", f"{COLUMN} <= 2"),
            drop("max_1"),
            drop("min_1"),
        ],
    )
    case(
        "055-write-fence-normalizes-integral-json-numbers",
        metadata(),
        [{"op": "prepare", "epoch": 1.0, "state": state(1)}],
        prepare(1),
    )
    bad = metadata(1)
    bad["constraints"][PREFIX + "mystery"] = "1"
    case(
        "056-write-fence-reserved-names-are-closed",
        bad,
        [refusal("state", "unrecognized constraint")],
        [],
    )
    bad = metadata(1)
    bad["column"] = "conflict"
    case(
        "057-write-fence-refuses-an-incompatible-column",
        bad,
        [refusal("prepare", "incompatible definition", epoch=1)],
        [],
    )
    bad = metadata(1)
    bad["constraints"][PREFIX + "owner_" + OTHER] = "1"
    case("058-write-fence-refuses-two-owners", bad, [refusal("state", "more than one project")], [])
    bad = metadata()
    bad["column"] = "valid"
    case(
        "059-write-fence-refuses-an-unowned-column",
        bad,
        [refusal("prepare", "not owned", epoch=1)],
        [],
    )
    bad = metadata(2**53)
    case("060-write-fence-refuses-an-unsafe-epoch", bad, [refusal("state", "safe integer")], [])
    case(
        "061-write-fence-retirement-prevents-reuse",
        metadata(2, retired=(H,)),
        [refusal("freeze", "cannot be reused", request_id=H)],
        [],
    )
    for name, initial, steps, calls, project in cases:
        directory = root / name
        directory.mkdir(exist_ok=True)
        document = {"table": TABLE, "project_id": project, "metadata": initial, "steps": steps}
        for filename, body in (("fencing.json", document), ("calls.json", calls)):
            (directory / filename).write_text(json.dumps(body, ensure_ascii=False, indent=2) + "\n")
    print(f"Wrote {len(cases)} native write-fencing cases without importing an SDK")


if __name__ == "__main__":
    main()
