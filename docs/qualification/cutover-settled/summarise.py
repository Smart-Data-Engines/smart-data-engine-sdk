"""The tables of README.md, from the measurement files beside this script.

    python3 docs/qualification/cutover-settled/summarise.py

Prints, for each file of ``final/`` and each direction, size and way in it: the range of the pause
over the runs, of its two phases that read rows, of what a settle costs before it - the advance's
own pause and the settle's time - and of the backfill before both, with the rows written since the
settle and the rows the pause wrote. A range is its lowest and highest run; nothing is averaged.
"""

from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent


def span(values: list[int | None], unit: float = 1.0, digits: int = 0) -> str:
    known = [value for value in values if value is not None]
    if not known:
        return "-"
    low, high = f"{min(known) / unit:.{digits}f}", f"{max(known) / unit:.{digits}f}"
    return low if low == high else f"{low}-{high}"


def outcome(run: dict[str, Any]) -> str:
    if run.get("pause_ms") is None:
        return f"stopped at the budget, then {run['resumed_outcome']} ({run['resumed_reason']})"
    return f"{run['outcome']} ({run['reason']})"


def main() -> None:
    for name in sorted((HERE / "final").glob("*.json")):
        report = json.loads(name.read_text(encoding="utf-8"))
        if "results" not in report:
            print(f"\n### {name.name}: unfinished, {len(report.get('partial', []))} runs\n")
            continue
        groups: dict[tuple[Any, ...], list[dict[str, Any]]] = defaultdict(list)
        for result in report["results"]:
            key = (result["source"], result["target"], result["rows"], result["way"])
            groups[key].append(result)
        commit = report["commit"][:7] if report.get("commit") else "?"
        print(f"\n### {name.name} ({commit})\n")
        print(
            "| move | rows | way | runs | pause, s | repair, ms | comparison, ms "
            "| written since the settle | rows the pause wrote | advance's pause, ms | settle, s "
            "| backfill before, s | outcome |"
        )
        print("|---|---|---|---|---|---|---|---|---|---|---|---|---|")
        for (source, target, rows, way), runs in groups.items():
            since = sorted({run.get("rows_written_since_the_settle", 0) for run in runs})
            wrote = sorted({(run.get("repair") or {}).get("written", "-") for run in runs}, key=str)
            pauses = [run.get("pause_ms") or run.get("interrupted_after_ms") for run in runs]
            advances = [(run.get("advance") or {}).get("pause_ms") for run in runs]
            print(
                f"| {source} → {target} | {rows} | `{way}` | {len(runs)} "
                f"| {span(pauses, 1000, 2)} | {span([run.get('repair_ms') for run in runs])} "
                f"| {span([run.get('verify_frozen_ms') for run in runs])} "
                f"| {', '.join(str(value) for value in since)} "
                f"| {', '.join(str(value) for value in wrote)} | {span(advances)} "
                f"| {span([run.get('settle_ms') for run in runs], 1000, 1)} "
                f"| {span([run.get('backfill_ms') for run in runs], 1000, 1)} "
                f"| {', '.join(sorted({outcome(run) for run in runs}))} |"
            )


if __name__ == "__main__":
    main()
