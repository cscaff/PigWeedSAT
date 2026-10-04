"""Paper-style figure for Tables 2/3: python -m satlat.figure -> build/fig_paper_comparison.{png,pdf}"""

from __future__ import annotations

import json
import os

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
from matplotlib.patches import Patch  # noqa: E402

from .report import ROOT, TABLE2_BRAM  # noqa: E402

SURFACE = "#fcfcfb"
INK, INK2, MUTED, GRID = "#0b0b0b", "#52514e", "#8a8984", "#e6e5e1"
C_ECP5, C_SA, C_SH, C_MS = "#2a78d6", "#eb6834", "#1baf7a", "#eda100"   # slots 1-4 (validated, adjacent)


def _fmt(ms: float) -> str:
    return f"{ms:,.0f}" if ms >= 100 else f"{ms:.3g}"


def main():
    rows = json.load(open(os.path.join(ROOT, "build", "table3_hw.json")))
    ms = json.load(open(os.path.join(ROOT, "build", "minisat_table3.json")))
    for r in rows:
        r["ms_ms"] = ms["results"][r["name"]]["ms"]
    plt.rcParams.update({"font.family": "DejaVu Sans", "font.size": 9,
                         "axes.edgecolor": MUTED, "axes.labelcolor": INK2,
                         "xtick.color": INK2, "ytick.color": INK})

    fig = plt.figure(figsize=(12.5, 9.4), facecolor=SURFACE)
    gs = fig.add_gridspec(1, 2, width_ratios=[1.9, 1], wspace=0.62)
    ax = fig.add_subplot(gs[0])
    bx = fig.add_subplot(gs[1])

    # ---------------------------------------------------------- (a) Table 3
    series = [("This work — ECP5-85F @ 50 MHz", C_ECP5, "ecp5"),
              ("SAT-Accel — U55C @ 230 MHz (paper)", C_SA, "sa_ms"),
              ("SAT-Hard (paper)", C_SH, "sh_ms"),
              (f"MiniSat 2.2 — {ms['cpu']} (measured)", C_MS, "ms_ms")]
    h = 0.21
    names = [r["name"] for r in rows]
    for i, r in enumerate(rows):
        y = len(rows) - 1 - i
        for j, (_, color, key) in enumerate(series):
            val = (r.get("ms_50mhz") if r.get("status") == "ok" else None) if key == "ecp5" else r[key]
            yy = y + (1.5 - j) * h
            if val is None:
                note = ("ECP5: " + ("does not fit on-chip" if r.get("status") == "does not fit"
                                    else "out of clause memory")) if key == "ecp5" else "SAT-Accel: N/A (out of memory)"
                ax.text(0.06, yy, note, va="center", ha="left", fontsize=6.8, color=MUTED,
                        style="italic")
                continue
            ax.barh(yy, val, height=h - 0.04, left=0.05, color=color, edgecolor=SURFACE,
                    linewidth=1.2, zorder=3)
            ax.text((val + 0.05) * 1.1, yy, _fmt(val), va="center", ha="left", fontsize=7, color=INK2)
    ax.set_xscale("log")
    ax.set_xlim(0.05, 2e5)
    ax.set_yticks(range(len(rows)))
    ax.set_yticklabels([f"{r['name']}\n{r['vars']} var · {r['clauses']} cls" for r in reversed(rows)],
                       fontsize=7.5, color=INK)
    ax.set_xlabel("Solve time, ms (log scale) — lower is better")
    ax.grid(axis="x", color=GRID, linewidth=0.8, zorder=0)
    ax.set_axisbelow(True)
    for s in ("top", "right", "left"):
        ax.spines[s].set_visible(False)
    ax.tick_params(axis="y", length=0)
    ax.set_facecolor(SURFACE)
    ax.legend(handles=[Patch(color=c, label=l) for l, c, _ in series], loc="upper center",
              bbox_to_anchor=(0.45, 1.075), ncol=2, frameon=False, fontsize=8, labelcolor=INK)
    ax.set_title("(a) Table 3 — SATLIB instances used by SAT-Hard", loc="left",
                 fontsize=11, color=INK, fontweight="bold", pad=34)

    ok = [r for r in rows if r.get("status") == "ok"]
    import math
    gm = lambda xs: math.exp(sum(math.log(x) for x in xs) / len(xs))
    gm_sh = gm([r["sh_ms"] / r["ms_50mhz"] for r in ok])
    gm_ms = gm([r["ms_50mhz"] / r["ms_ms"] for r in ok])

    # ---------------------------------------------------------- (b) Table 2
    mods = list(TABLE2_BRAM.items())
    counts = [sum(v.values()) for _, v in mods]
    order = sorted(range(len(mods)), key=lambda k: counts[k])
    for pos, k in enumerate(order):
        bx.barh(pos, counts[k], height=0.62, color=C_ECP5, edgecolor=SURFACE, zorder=3)
        bx.text(counts[k] + 0.6, pos, str(counts[k]), va="center", fontsize=8, color=INK2)
    bx.set_yticks(range(len(order)))
    bx.set_yticklabels([mods[k][0] for k in order], fontsize=8, color=INK)
    bx.set_xlabel("DP16KD block RAMs (18 Kbit each)")
    bx.grid(axis="x", color=GRID, linewidth=0.8, zorder=0)
    bx.set_axisbelow(True)
    for s in ("top", "right", "left"):
        bx.spines[s].set_visible(False)
    bx.tick_params(axis="y", length=0)
    bx.set_facecolor(SURFACE)
    bx.set_xlim(0, max(counts) * 1.2)
    bx.set_title("(b) Table 2 — block RAM by module", loc="left", fontsize=11,
                 color=INK, fontweight="bold", pad=34)
    bx.text(0, 1.03, "solver 123 / 208 DP16KD · full SoC 147\nU55C SAT-Accel: 419 BRAM + 778 URAM",
            transform=bx.transAxes, fontsize=7.5, color=INK2)

    fig.suptitle("SAT-Accel reimplemented in Amaranth on a Lattice ECP5-85F",
                 x=0.06, y=0.985, ha="left", fontsize=13, fontweight="bold", color=INK)
    import textwrap
    cap = (f"ECP5: measured on a Manhattan Reasoning cloud ECP5-85F (fpga1), on-chip solve cycles at 50 MHz, host load excluded; "
             f"all {len(ok)} solved answers correct, SAT models re-verified.  Geomean: {gm_sh:.0f}× faster than SAT-Hard, "
             f"{gm_ms:.0f}× slower than MiniSat on the M4 Pro.\n"
             "SAT-Accel and SAT-Hard times from Lo et al., FPGA ’25, Table 3.  MiniSat 2.2 default options, median CPU time of "
             "11 runs (includes parsing; ~1 ms floor).  Resources: ECP5 solver 10.2K LUT, 4.0K FF, 3 DSP, Fmax 63.5 MHz; "
             "the ECP5 has no URAM,\nso the clause / transposed stores and position maps (67% of solver BRAM) live in block RAM.")
    cap = "\n".join(textwrap.wrap(cap.replace("\n", " "), 190))
    fig.text(0.06, 0.025, cap, fontsize=7.5, color=INK2, va="bottom", ha="left")
    fig.subplots_adjust(left=0.13, right=0.97, top=0.86, bottom=0.17)
    out = os.path.join(ROOT, "build", "fig_paper_comparison")
    fig.savefig(out + ".png", dpi=200, facecolor=SURFACE)
    fig.savefig(out + ".pdf", facecolor=SURFACE)
    print(out + ".png")


if __name__ == "__main__":
    main()
