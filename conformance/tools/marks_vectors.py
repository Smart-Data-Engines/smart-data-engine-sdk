#!/usr/bin/env python3
"""Write conformance/vectors/marks/: where each mark a library writes is taken (contract 7r).

    python3 conformance/tools/marks_vectors.py [--check]

`--check` writes nothing and fails if a vector would change, or if a case directory is there that
this file no longer writes.

A copy's table is one a library created and marked; a source's table is one no library marked as a
copy. Each case gives a project, a group, a table and the mark read on it (`null` where there is
none), and two answers: `copy`, whether the mark makes the table a copy of that group in that
project, and `source`, whether a table carrying it is taken as a source. The marks are computed
here with `hashlib` alone, from the forms the contract gives; the answers are written by hand, not
computed by any library: a vector computed by the implementation it checks would check nothing.
"""

from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1] / "vectors" / "marks"

PROJECT = "5eed" * 8
OTHER_PROJECT = "0dd5" * 8
STAGE = "a1" * 16
OTHER_STAGE = "b2" * 16
STAGED = f"sde_m_{STAGE}_000001"


def _d16(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def copy1(project: str, group: str, table: str) -> str:
    return f"sde.copy1.{project}.{_d16(group)}.{_d16(table)}"


def stage1(project: str, stage: str, table: str) -> str:
    return f"sde.stage1.{project}.{stage}.{_d16(table)}"


def stage2(project: str, stage: str, group: str, table: str) -> str:
    return f"sde.stage2.{project}.{stage}.{_d16(group)}.{_d16(table)}"


def source1(project: str, group: str, table: str) -> str:
    return f"sde.source1.{project}.{_d16(group)}.{_d16(table)}"


def case(
    table: str, mark: str | None, copy: bool, source: bool, why: str, group: str = "Event"
) -> dict[str, Any]:
    return {"project_id": PROJECT, "group": group, "table": table, "mark": mark,
            "copy": copy, "source": source, "why": why}


CASES: list[dict[str, Any]] = [
    case("event_copy", copy1(PROJECT, "Event", "event_copy"), True, False,
         "prepare_schema's mark for this project, group and table: a copy, never a source"),
    case("event_copy", copy1(PROJECT, "Account", "event_copy"), False, False,
         "another group's copy: a forged map naming it Event's copy would write Event's rows "
         "over it"),
    case("event_copy", copy1(OTHER_PROJECT, "Event", "event_copy"), False, False,
         "another project's copy"),
    case("event_copy", copy1(PROJECT, "Event", "other_table"), False, False,
         "the mark of another table name, as a table renamed under its mark would carry"),
    case(STAGED, stage2(PROJECT, STAGE, "Event", STAGED), True, False,
         "a staging's mark for this group, on the copy it named after its stage"),
    case(STAGED, stage2(PROJECT, STAGE, "Account", STAGED), False, False,
         "another group's staged copy: its mark names its group"),
    case(STAGED, stage2(PROJECT, OTHER_STAGE, "Event", STAGED), False, False,
         "a staging's mark of another stage than the one the table is named after"),
    case(STAGED, stage1(PROJECT, STAGE, STAGED), True, False,
         "an earlier release's staging mark names no group: taken on a staged copy's table, where "
         "the operator checks the stage's group in its records; never on a source, to which "
         "sde-operator mark-sources gives a source's mark where its records show the stage "
         "completed for the group and a cutover of the group completed"),
    case(STAGED, stage1(PROJECT, STAGE, STAGED), True, False,
         "the same mark for another group is taken too: it names no group, so only the operator's "
         "records of the stage can tell, and a library takes it as any group's staged copy",
         group="Account"),
    case(STAGED, stage1(OTHER_PROJECT, STAGE, STAGED), False, False,
         "an earlier release's staging mark of another project"),
    case(STAGED, stage2(OTHER_PROJECT, STAGE, "Event", STAGED), False, False,
         "a staging's mark of another project"),
    case("event_copy", stage1(PROJECT, STAGE, "event_copy"), False, False,
         "a staging's mark on a table not named after its stage"),
    case("event", source1(PROJECT, "Event", "event"), False, True,
         "a cutover's mark on the copy it made the source: a table that has been a source is never "
         "a copy again"),
    case("event", source1(PROJECT, "Account", "event"), False, True,
         "another group's source mark is taken on a source: a group renamed in the model keeps its "
         "source"),
    case("event", None, False, True, "no mark: a table of the client's from before SDE"),
    case("event", "", False, True, "an empty comment is no mark (ClickHouse's table without one)"),
    case("event", "the client's own note", False, True, "a comment of the client's is no mark"),
    case("event_copy", copy1(PROJECT, "Event", "event_copy")[:-1], False, True,
         "a copy's mark one digit short is no mark of a library's, and a table carrying it is not "
         "a copy"),
    case("event_copy", copy1(PROJECT, "Event", "event_copy") + "\n", False, True,
         "a mark is the whole comment: one with a line break after it is no mark"),
    case(STAGED, stage2(PROJECT, STAGE, "Event", STAGED).upper().replace("SDE.STAGE2", "sde.stage2"),
         False, True, "a mark's digits are lowercase hexadecimal: one in capitals is no mark"),
]


def write(check: bool) -> int:
    target = ROOT / "001-where-a-mark-is-taken" / "cases.json"
    text = json.dumps(CASES, indent=1, ensure_ascii=True) + "\n"
    present = {p.name for p in ROOT.iterdir()} if ROOT.exists() else set()
    if check:
        problems = []
        if present - {"001-where-a-mark-is-taken"}:
            problems.append(f"case directories this file does not write: {sorted(present)}")
        if not target.exists() or target.read_text() != text:
            problems.append(f"{target} would change")
        for problem in problems:
            print(problem)
        return 1 if problems else 0
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text)
    return 0


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true")
    raise SystemExit(write(parser.parse_args().check))
