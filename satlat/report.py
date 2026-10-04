"""Build the Table 2 / Table 3 comparison report: python -m satlat.report"""

from __future__ import annotations

import json
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Per-memory DP16KD counts from yosys synth_ecp5 of design.py (build/synth/cells.txt),
# attributed to the paper's Table 2 modules.
TABLE2_BRAM = {
    "Decision (VSIDS heap)": {"pq_heap": 4, "pq_pos": 2},
    "Propagation": {"cls_states": 6},
    "Learn": {"merge_scratch": 2, "valid_learn": 1, "resolution": 2},
    "Min/Btrk": {"min_scratch": 1, "valid_min": 1, "to_minimize": 1, "min_queue": 1},
    "Deletion": {"bucket_next": 3, "free_cls_id": 3},
    "Cls Store": {"cls_store": 14, "cmd": 6, "free_cls_pages": 4},
    "Tran. Store": {"lit_store": 14, "occ": 12, "free_lit_pages": 4},
    "Tran<->Cls Position": {"lit_to_cls": 14, "cls_to_lit": 14},
    "Variable state (lmd, trail)": {"meta": 6, "lmmd": 1, "answer_stack": 2,
                                    "unit_by_cls": 2, "stack_end": 3},
    "Heuristics (PigWeedSAT)": {"used": 1, "best": 1, "dl5": 1},
}
# Paper Table 2 (U55C): BRAM share per module.
PAPER_T2_BRAM_PCT = {"Decision (VSIDS heap)": 2, "Propagation": 35, "Learn": 51, "Min/Btrk": 1,
                     "Deletion": 0, "Cls Store": 9, "Tran. Store": 0, "Tran<->Cls Position": 0}
ECP5 = {"BRAM": 208, "DSP": 156, "FF": 83640, "LUT": 83640}
USER = {"BRAM": 126, "DSP": 2, "FF": 5072, "LUT": 11470}             # synth_ecp5, design only (LUT4)
SOC = {"BRAM": 150, "DSP": 7, "FF": 7746, "LUT": 21249}              # full-SoC PnR (TRELLIS_COMB)
PAPER_ABS = {"BRAM": (419, 2016), "DSP": (48, 9024), "FF": (324891, 2607360),
             "LUT": (251283, 1303680), "URAM": (778, 960)}


def table2() -> str:
    out = ["### Table 2 — resource utilization",
           "",
           "| | SAT-Accel (U55C, paper) | This work: solver only (ECP5-85F) | This work: full SoC incl. CPU+Ethernet |",
           "|---|---|---|---|"]
    for k in ("BRAM", "DSP", "FF", "LUT"):
        used, avail = PAPER_ABS[k]
        out.append(f"| {k} | {used:,} / {avail:,} ({100 * used / avail:.0f}%) | "
                   f"{USER[k]:,} / {ECP5[k]:,} ({100 * USER[k] / ECP5[k]:.1f}%) | "
                   f"{SOC[k]:,} / {ECP5[k]:,} ({100 * SOC[k] / ECP5[k]:.1f}%) |")
    out.append(f"| URAM | {PAPER_ABS['URAM'][0]} / {PAPER_ABS['URAM'][1]} (81%) | — (ECP5 has none) | — |")
    out += ["", "**Block RAM by module** (the ECP5 has no URAM, so the URAM-resident stores "
            "move into DP16KD; logic is one shared sequencer, so LUT/FF are not split by module):",
            "", "| Module | DP16KD | share of solver BRAM | paper BRAM share | memories |",
            "|---|---|---|---|---|"]
    tot = sum(sum(v.values()) for v in TABLE2_BRAM.values())
    for mod, mems in TABLE2_BRAM.items():
        n = sum(mems.values())
        p = PAPER_T2_BRAM_PCT.get(mod)
        out.append(f"| {mod} | {n} | {100 * n / tot:.0f}% | {'—' if p is None else f'{p}% (+URAM)' if mod in ('Cls Store', 'Tran. Store', 'Tran<->Cls Position') else f'{p}%'} | "
                   + ", ".join(f"`{k}` {v}" for k, v in mems.items()) + " |")
    out.append(f"| **Total** | **{tot}** | | | |")
    return "\n".join(out)


def table3(rows) -> str:
    mpath = os.path.join(ROOT, "build", "minisat_table3.json")
    mini = json.load(open(mpath))["results"] if os.path.exists(mpath) else {}
    out = ["### Table 3 — SATLIB instances used by SAT-Hard",
           "",
           "| Problem | Var | Cls | ECP5 result | ECP5 ms @50 MHz | SAT-Accel ms (U55C @230 MHz) | SAT-Hard ms | MiniSat ms (M4 Pro) | vs SAT-Hard | vs SAT-Accel | dec / confl / rst |",
           "|---|---|---|---|---|---|---|---|---|---|---|"]
    sp_sh, sp_sa = [], []
    for r in rows:
        sa = "N/A" if r["sa_ms"] is None else f"{r['sa_ms']:g}"
        if r.get("status") == "ok":
            ms = r.get("ms", r["ms_50mhz"])
            vs_sh = r["sh_ms"] / ms
            sp_sh.append(vs_sh)
            vs_sa = (r["sa_ms"] / ms) if r["sa_ms"] else None
            if vs_sa:
                sp_sa.append(vs_sa)
            res = ("SAT ✓" if r["result"] == 1 else "UNSAT ✓")
            out.append(f"| {r['name']} | {r['vars']} | {r['clauses']} | {res} | {ms:.3f} | {sa} | "
                       f"{r['sh_ms']:,} | {mini.get(r['name'], {}).get('ms', float('nan')):.2f} | {vs_sh:,.0f}x | "
                       f"{'—' if vs_sa is None else f'{vs_sa:.2f}x'} | "
                       f"{r['decisions']} / {r['conflicts']} / {r['restarts']} |")
        else:
            why = r.get("status", "?")
            extra = f" after {r['conflicts']} conflicts" if "conflicts" in r else ""
            out.append(f"| {r['name']} | {r['vars']} | {r['clauses']} | N/A — {why}{extra} | — | {sa} | "
                       f"{r['sh_ms']:,} | {mini.get(r['name'], {}).get('ms', float('nan')):.2f} | — | — | — |")
    import math
    gm = lambda xs: math.exp(sum(math.log(x) for x in xs) / len(xs)) if xs else float("nan")
    out += ["", f"Speedup over SAT-Hard on the {len(sp_sh)} solved instances: "
            f"arithmetic mean {sum(sp_sh) / len(sp_sh):,.0f}x, geometric mean {gm(sp_sh):,.0f}x "
            f"(paper's SAT-Accel: avg 800x).  Relative to SAT-Accel on the U55C: geometric mean "
            f"{gm(sp_sa):.2f}x over {len(sp_sa)} instances (<1 = slower)."]
    return "\n".join(out)


def heuristics(rows, pw) -> str:
    """HLS algorithm vs. PigWeedSAT heuristics, same board and bitstream."""
    import math
    by = {r["name"]: r for r in pw}
    out = ["### Heuristics — HLS algorithm vs. `host.pigweed()` (same bitstream)",
           "",
           "| Problem | HLS ms | heuristics ms | speedup | conflicts (HLS → heuristics) |",
           "|---|---|---|---|---|"]
    sp = []
    for r in rows:
        q = by.get(r["name"])
        if not q or "conflicts" not in r or "conflicts" not in q:
            continue
        cell = lambda x: f"{x['ms']:,.2f}" if x["status"] == "ok" else x["status"]  # noqa: E731
        speed = "—"
        if r["status"] == q["status"] == "ok":
            sp.append(r["ms"] / q["ms"])
            speed = f"{sp[-1]:.2f}x"
        out.append(f"| {r['name']} | {cell(r)} | {cell(q)} | {speed} | "
                   f"{r['conflicts']:,} → {q['conflicts']:,} |")
    gm = math.exp(sum(map(math.log, sp)) / len(sp))
    out += ["", f"Geometric-mean speedup over the {len(sp)} instances both solve: {gm:.2f}x."]
    return "\n".join(out)


def main():
    rows = json.load(open(os.path.join(ROOT, "build", "table3_hw.json")))
    parts = [table2(), table3(rows)]
    pw_path = os.path.join(ROOT, "build", "table3_hw_pigweed.json")
    if os.path.exists(pw_path):
        parts.append(heuristics(rows, json.load(open(pw_path))))
    md = "\n\n".join(parts)
    path = os.path.join(ROOT, "build", "paper_tables.md")
    open(path, "w").write(md + "\n")
    print(md)


if __name__ == "__main__":
    main()
