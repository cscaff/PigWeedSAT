"""Run the SAT accelerator on a real ECP5-85F through Manhattan Reasoning.

    mrg run app.py                       # builds design.py, programs a board, solves
    mrg run app.py --no-program --fpga-id N   # reuse a board you already hold
    SAT_CNFS="a.cnf b.cnf" mrg run app.py     # choose instances

The board stays reserved after `mrg run`; free it with `mrg reset <fpga_id>`.
Every SAT model is re-checked on the host against the original clauses, and
the answer is compared to the golden model's (and to the sat/unsat folder).
"""

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import manhattan_reasoning_gym as mrg  # noqa: E402

from satlat import driver as DRV  # noqa: E402
from satlat import golden as G  # noqa: E402
from satlat import host as H  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
T = os.path.join(HERE, "SAT_test_cases")
DEFAULT_CNFS = [
    f"{T}/sat/aalto.dimacs", f"{T}/sat/quinn.dimacs", f"{T}/sat/uf20.dimacs",
    f"{T}/sat/nqueens_4.dimacs", f"{T}/sat/nqueens_8.dimacs", f"{T}/sat/aim50.dimacs",
    f"{T}/sat/sudoku_4_2_2.dimacs", f"{T}/sat/sudoku_6_2_3.dimacs",
    f"{T}/unsat/aalto_2.dimacs", f"{T}/unsat/nqueens_3.dimacs", f"{T}/unsat/4_4_1.txt",
    f"{T}/unsat/aim100.dimacs", f"{T}/unsat/dubois20.dimacs",
    f"{T}/unsat/ssa0432-003.dimacs", f"{T}/unsat/pret150_75.dimacs",
]

app = mrg.cloud.App("pigweedsat", design=os.path.join(HERE, "design.py"))


@app.local_entrypoint()
def main():
    cnfs = os.environ.get("SAT_CNFS", "").split() or DEFAULT_CNFS
    print(f"board: fpga{app.fpga_id}" if app.fpga_id is not None else "programming board ...")
    failures = 0
    with app.stream() as s:
        bus = DRV.CloudBus(s)
        print(f"landed on fpga{app.fpga_id}")
        for path in cnfs:
            name = os.path.relpath(path, HERE)
            try:
                img = H.load_cnf(path)
            except H.Unsupported as e:
                print(f"SKIP {name}: {e}")
                continue
            gres, g = G.solve(img, 2_000_000)
            t = time.time()
            res, stats, model = DRV.solve(bus, img, timeout=300)
            wall = time.time() - t
            ok = res == gres
            note = ""
            if res == 1:
                bad = H.check_model(img.clauses, model)
                ok = ok and not bad
                note = "model verified" if not bad else f"model violates {len(bad)} clauses"
            same = all(stats[n] == getattr(g.stats, n) for n in ("decide", "backtrack", "reset"))
            cyc = stats["cycles_lo"] | (stats["cycles_hi"] << 32)
            print(f"{'OK ' if ok else 'BAD'} {name}: {'SAT' if res == 1 else 'UNSAT' if res == 0 else res} "
                  f"dec={stats['decide']} confl={stats['backtrack']} rst={stats['reset']} "
                  f"cycles={cyc:,} ({cyc / 50e6 * 1e3:.2f} ms @50MHz) wall={wall:.1f}s "
                  f"{'trajectory==golden' if same else 'trajectory differs from golden'} {note}")
            failures += not ok
    print(f"done: {failures} failure(s). Free the board with: mrg reset {app.fpga_id}")
