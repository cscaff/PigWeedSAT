"""Run the golden model over a directory of CNFs: python -m satlat.run_golden [files...]"""

from __future__ import annotations

import glob
import os
import sys
import time

from . import golden as G
from . import host as H

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def expected(path: str) -> int | None:
    parts = path.split(os.sep)
    if "unsat" in parts:
        return 0
    if "sat" in parts:
        return 1
    return None


def main(argv):
    files = argv or sorted(glob.glob(os.path.join(ROOT, "SAT_test_cases", "*", "*")))
    bad = 0
    for f in files:
        name = os.path.relpath(f, ROOT)
        try:
            img = H.load_cnf(f)
        except H.Unsupported as e:
            print(f"SKIP {name}: {e}")
            continue
        t = time.time()
        try:
            res, g = G.solve(img, max_iterations=2_000_000)
        except G.SolverError as e:
            print(f"LIMIT {name}: {e}")
            bad += 1
            continue
        dt = time.time() - t
        exp = expected(f)
        ok = res == exp
        if res == 1:
            unsat_cls = H.check_model(img.clauses, g.model())
            if unsat_cls:
                ok = False
                print(f"   model violates {len(unsat_cls)} clauses")
        s = g.stats
        print(f"{'OK ' if ok else 'BAD'} {name}: res={res} exp={exp} "
              f"dec={s.decide} confl={s.backtrack} rst={s.reset} del={s.deleted} {dt:.1f}s")
        bad += not ok
    print("failures:", bad)
    return bad


if __name__ == "__main__":
    sys.exit(1 if main(sys.argv[1:]) else 0)
