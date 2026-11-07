"""Where a move's repair spends its time: one cutover under cProfile, the top functions printed.

``measure_phases.py`` showed which phase of the pause grows with the group; this names the code
inside it. One move of N rows from ``--source``, the same fixtures and seeding as the other two
scripts, with only ``LocalCutover.execute`` profiled. The profiler slows Python down, so the times
it prints are larger than the pause measured without it; the ranking is what it is for.

    cd python/tests
    ../.venv/bin/python ../../docs/qualification/cutover-limits/profile_move.py \
        --source clickhouse --rows 50000 > profile-ch-pg.txt
"""

from __future__ import annotations

import argparse
import cProfile
import io
import pstats
import sys
from pathlib import Path
from tempfile import TemporaryDirectory

sys.path.insert(0, str(Path.cwd()))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from measure_moves import OTHER, seed
from test_staging_operator_live import cutover, initial


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source", choices=sorted(OTHER), required=True)
    parser.add_argument("--rows", type=int, required=True)
    parser.add_argument("--top", type=int, default=30)
    args = parser.parse_args()
    with TemporaryDirectory() as tmp, initial(
        args.source, Path(tmp), target=OTHER[args.source]
    ) as (operator, stage, _old, roles, model, public, signed, _m, _s, _t):
        seed(roles[args.source].operator, "initial_events", 2, args.rows - 1)
        operator.stage(stage)
        packet = cutover(stage, signed, model, public)
        profile = cProfile.Profile()
        receipt = profile.runcall(operator.execute, packet).as_record()
    print(f"source={args.source} rows={args.rows} outcome={receipt['outcome']} "
          f"reason={receipt['reason']} pause_ms={receipt['elapsed_ms']}")
    for order in ("tottime", "cumulative"):
        out = io.StringIO()
        pstats.Stats(profile, stream=out).strip_dirs().sort_stats(order).print_stats(args.top)
        print(f"\n=== by {order} ===")
        print(out.getvalue())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
