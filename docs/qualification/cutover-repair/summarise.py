"""The tables of README.md, from the measurement files beside this script.

    python3 docs/qualification/cutover-repair/summarise.py

Prints, for each file of ``final/`` and each direction, way and chunk in it: the range of the pause
over the runs, of the two phases that grow with the group, of the backfill before the pause, and
the rows the repair wrote. A range is its lowest and highest run; nothing here is averaged.
"""

from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent


def span(values: list[int | None], unit: float = 1000.0) -> str:
    known = [value for value in values if value is not None]
    if not known:
        return "-"
    low, high = min(known) / unit, max(known) / unit
    return f"{low:.1f}" if f"{low:.1f}" == f"{high:.1f}" else f"{low:.1f}-{high:.1f}"


def main() -> None:
    for name in sorted((HERE / "final").glob("*.json")):
        report = json.loads(name.read_text(encoding="utf-8"))
        groups: dict[tuple[Any, ...], list[dict[str, Any]]] = defaultdict(list)
        for result in report["results"]:
            key = (result["source"], result["target"], result["rows"], result["chunk_rows"],
                   result["way"])
            groups[key].append(result)
        print(f"\n### {name.name} ({report['commit'][:7] if report.get('commit') else '?'})\n")
        print("| move | rows | chunk | way | runs | pause, s | repair, s | comparison, s "
              "| backfill before, s | rows written | outcome |")
        print("|---|---|---|---|---|---|---|---|---|---|---|")
        for (source, target, rows, chunk, way), runs in groups.items():
            written = sorted({(run.get("repair") or {}).get("written", "-") for run in runs},
                             key=str)
            outcomes = sorted({str(run.get("resumed_outcome") or run["outcome"])
                               + ("" if run.get("pause_ms") is not None else " after the budget")
                               for run in runs})
            pauses = [run.get("pause_ms") or run.get("interrupted_after_ms") for run in runs]
            print(
                f"| {source} → {target} | {rows} | {chunk} | {way} | {len(runs)} "
                f"| {span(pauses)} | {span([run.get('repair_ms') for run in runs])} "
                f"| {span([run.get('verify_frozen_ms') for run in runs])} "
                f"| {span([run.get('backfill_ms') for run in runs])} "
                f"| {', '.join(str(value) for value in written)} | {', '.join(outcomes)} |"
            )


if __name__ == "__main__":
    main()
