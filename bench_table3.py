"""SAT-Accel paper (FPGA '25) Table 3 -- the SAT-Hard SATLIB comparison -- on the ECP5.

    mrg run bench_table3.py            # results -> build/table3_hw.json

Uses the paper's solver configuration: prune 10%, reset multiplier 100,
VSIDS decay 0.95, transposed (literal) page 16 words, clause page 4 words.
"""

import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import manhattan_reasoning_gym as mrg  # noqa: E402

from satlat import driver as DRV  # noqa: E402
from satlat import golden as G  # noqa: E402
from satlat import host as H  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
BENCH = os.path.join(HERE, "benchmarks", "satlib", "table3")
OUT = os.path.join(HERE, "build", "table3_hw.json")
CLK_HZ = int(os.environ.get("MRG_SYS_CLK_FREQ", 50_000_000))   # user-design clock
CFG = H.Config(lit_page=16, reset_multiplier=100, positive_phase=False, decay=0.95, prune=0.1)

# (name, vars, clauses, expected, SAT-Accel ms, SAT-Hard ms) -- Table 3 of the paper
TABLE3 = [
    ("hole7", 56, 204, 0, 125, 330),
    ("hole8", 72, 297, 0, 691, 2270),
    ("hole9", 90, 415, 0, None, 15290),
    ("uf100-010", 100, 430, 1, 1, 580),
    ("uuf100-02", 100, 430, 0, 4, 4940),
    ("uf125-01", 125, 538, 1, 4, 1160),
    ("uuf125-05", 125, 538, 0, 7, 4900),
    ("uf150-08", 150, 645, 1, 1, 3920),
    ("CBS_k3_n100_m403_b10_1", 100, 403, 1, 2, 2340),
    ("aim-200-3_4-yes1-4", 200, 680, 1, 4, 1200),
    ("aim-200-1_6-no-4", 200, 320, 0, 0.3, 10),
    ("ii16e2", 532, 7825, 1, 4, 5760),
    ("ii32e1", 222, 1186, 1, 0.1, 20),
]

app = mrg.cloud.App("pigweedsat_table3", design=os.path.join(HERE, "design.py"))


@app.local_entrypoint()
def main():
    rows = []
    with app.stream() as s:
        bus = DRV.CloudBus(s)
        print(f"landed on fpga{app.fpga_id}, user clock {CLK_HZ / 1e6:g} MHz")
        for name, nv, nc, expect, sa_ms, sh_ms in TABLE3:
            row = dict(name=name, vars=nv, clauses=nc, expect=expect, sa_ms=sa_ms, sh_ms=sh_ms,
                       fpga=app.fpga_id)
            try:
                img = H.load_cnf(os.path.join(BENCH, name + ".cnf"), CFG)
            except H.Unsupported as e:
                row.update(status="does not fit", detail=str(e))
                print(f"SKIP {name}: {e}")
                rows.append(row)
                continue
            gres, g = G.solve(img, 5_000_000)
            t = time.time()
            res, stats, model = DRV.solve(bus, img, timeout=600)
            wall = time.time() - t
            cyc = stats["cycles_lo"] | (stats["cycles_hi"] << 32)
            row.update(result=res, golden=gres, cycles=cyc, clk_hz=CLK_HZ, ms=cyc / CLK_HZ * 1e3,
                       ms_50mhz=cyc / 50e3,
                       decisions=stats["decide"], conflicts=stats["backtrack"],
                       restarts=stats["reset"], deleted=stats["deleted"],
                       phase_cycles=stats["cycle_counters"], host_load_s=stats["host_load_s"],
                       wall_s=wall,
                       trajectory_matches_golden=all(
                           stats[n] == getattr(g.stats, n)
                           for n in ("decide", "backtrack", "reset", "learn_iter", "check_cnt")))
            if res == 1:
                bad = H.check_model(img.clauses, model)
                row["model_verified"] = not bad
            if res in (0, 1):
                row["status"] = "ok" if res == expect and row.get("model_verified", True) else "WRONG"
            else:
                row["status"] = {-2: "learned clause too long", -4: "out of clause memory",
                                 -5: "out of literal-page memory"}.get(res, f"error {res}")
            rows.append(row)
            print(f"{row['status']:>22} {name}: res={res} golden={gres} {cyc:,} cycles = "
                  f"{cyc / CLK_HZ * 1e3:.3f} ms @{CLK_HZ / 1e6:g}MHz  dec={stats['decide']} confl={stats['backtrack']} "
                  f"(paper SA {sa_ms} ms, SAT-Hard {sh_ms} ms)")
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    with open(OUT, "w") as f:
        json.dump(rows, f, indent=1)
    print(f"wrote {OUT}. Free the board with: mrg reset {app.fpga_id}")
