"""PigWeedSAT comparison figure: python -m satlat.figure -> build/fig_paper_comparison.{png,pdf}

(a) Table 3 solve times (ECP5 measured, SAT-Accel / SAT-Hard from the papers,
MiniSat measured), (b) Table 2 block-RAM by module, (c) platform specs + cost.
"""

from __future__ import annotations

import csv
import datetime as dt
import json
import math
import subprocess
import os
import textwrap

import matplotlib

matplotlib.use("Agg")
import matplotlib.image as mpimg  # noqa: E402
import matplotlib.pyplot as plt  # noqa: E402
from matplotlib.patches import Patch  # noqa: E402

from .report import ROOT, TABLE2_BRAM  # noqa: E402

SURFACE = "#fcfcfb"
INK, INK2, MUTED, GRID = "#0b0b0b", "#52514e", "#8a8984", "#e6e5e1"
C_ECP5, C_SA, C_SH, C_MS = "#2a78d6", "#eb6834", "#1baf7a", "#eda100"   # slots 1-4 (validated, adjacent)
LOGO = os.path.join(ROOT, "Assets", "logo.jpg")

# Platform reference (c).  Prices: mean of single-unit US list prices, Oct 2026.
PLATFORMS = [
    # name, board, FPGA, node, clock, LUT, FF, BRAM, DSP, on-board memory, price
    ("PigWeedSAT (this work)", "Lattice ECP5-5G Eval", "ECP5 LFE5UM5G-85F", "40 nm",
     None, "84K", "84K", "208 × 18 Kb  (3.7 Mb)", "156", "—", "~$140"),
    ("SAT-Accel (FPGA ’25)", "AMD Alveo U55C", "Virtex US+ XCU55P", "16 nm",
     "230 MHz", "1.30M", "2.61M", "2,016 × 36 Kb + 960 URAM", "9,024", "16 GB HBM2", "~$5,400"),
    ("SAT-Hard (DSD ’19)", "Digilent ZedBoard", "Zynq-7000 XC7Z020", "28 nm",
     "n/r", "53K", "106K", "140 × 36 Kb  (4.9 Mb)", "220", "512 MB DDR3", "~$620"),
]


def _token_series():
    """Cumulative tokens per model response (from tools/token_log.py's CSV)."""
    subprocess.run(["python3", os.path.join(ROOT, "tools", "token_log.py"), "--all"],
                   check=True, capture_output=True)
    rows = list(csv.DictReader(open(os.path.join(ROOT, "build", "token_usage.csv"))))
    rows.sort(key=lambda r: r["timestamp"])
    keys = ["input_tokens", "output_tokens", "cache_creation_input_tokens",
            "cache_read_input_tokens"]
    t, cum, tot = [], [], 0
    parts = {k: 0 for k in keys}
    for r in rows:
        for k in keys:
            parts[k] += int(r[k])
            tot += int(r[k])
        t.append(dt.datetime.fromisoformat(r["timestamp"].replace("Z", "+00:00")))
        cum.append(tot)
    return t, cum, parts, len(rows), sorted({r["model"] for r in rows})


MILESTONES = [   # git commit subject prefix -> label
    ("Monolithic", "port verified\non hardware"),
    ("Refactor", "kernel\nsubmodules"),
    ("Pipeline occurrence", "pipelined\nwalks"),
    ("PQ command FIFO", "PQ FIFO +\nbacktrack ∥ min"),
    ("Timing:", "timing\n(72 MHz Fmax)"),
    ("65 MHz", "65 MHz on\nhardware"),
]


def _milestones():
    out = subprocess.run(["git", "-C", ROOT, "log", "--reverse", "--format=%aI|%s"],
                         capture_output=True, text=True).stdout.splitlines()
    res = []
    for prefix, label in MILESTONES:
        for line in out:
            when, subj = line.split("|", 1)
            if subj.startswith(prefix):
                res.append((dt.datetime.fromisoformat(when), label))
                break
    return res


def _compute_panel(fig):
    """(d) Compute spend: cumulative model tokens over the session, milestones marked."""
    t, cum, parts, n, models = _token_series()
    t0 = t[0]
    hrs = [(x - t0).total_seconds() / 3600 for x in t]
    M = [c / 1e6 for c in cum]
    dx = fig.add_axes([0.10, 0.072, 0.53, 0.09])
    dx.plot(hrs, M, color=C_ECP5, linewidth=2, zorder=3)
    dx.set_facecolor(SURFACE)
    for s in ("top", "right"):
        dx.spines[s].set_visible(False)
    dx.grid(axis="y", color=GRID, linewidth=0.8, zorder=0)
    dx.set_xlim(0, hrs[-1] * 1.02)
    dx.set_ylim(0, M[-1] * 1.45)
    dx.set_xlabel("Hours into the Claude Code session", fontsize=8)
    dx.set_ylabel("Cumulative tokens (M)", fontsize=8)
    dx.tick_params(labelsize=7.5)
    for k, (when, label) in enumerate(_milestones()):
        h = (when - t0).total_seconds() / 3600
        if 0 <= h <= hrs[-1]:
            y = next((m for x, m in zip(hrs, M) if x >= h), M[-1])
            dx.plot([h], [y], marker="o", markersize=5, color=C_ECP5,
                    markeredgecolor=SURFACE, markeredgewidth=1.5, zorder=4)
            dx.annotate(label, (h, y), xytext=(0, 8 + 22 * (k % 2)), textcoords="offset points",
                        ha="center", va="bottom", fontsize=6.6, color=INK2,
                        arrowprops=dict(arrowstyle="-", color=GRID, linewidth=0.8,
                                        shrinkA=0, shrinkB=3))
    fig.text(0.06, 0.178, "(d) Compute spend — tokens used to build and measure PigWeedSAT",
             fontsize=11, fontweight="bold", color=INK, va="bottom")
    total = sum(parts.values())
    lines = [
        (f"{total / 1e6:,.1f}M", "tokens in total"),
        (f"{parts['cache_read_input_tokens'] / 1e6:,.1f}M",
         f"cache reads ({100 * parts['cache_read_input_tokens'] / total:.0f}%): context re-read each turn"),
        (f"{parts['cache_creation_input_tokens'] / 1e6:,.2f}M", "cache writes (new context)"),
        (f"{parts['output_tokens'] / 1e6:,.2f}M", "output tokens (code, reasoning, tool calls)"),
        (f"{n:,}", f"model responses · {', '.join(models)}"),
    ]
    y = 0.150
    for big, small in lines:
        fig.text(0.67, y, big, fontsize=12 if big == lines[0][0] else 9.5, fontweight="bold",
                 color=INK, va="baseline")
        fig.text(0.755, y, small, fontsize=7.6, color=INK2, va="baseline")
        y -= 0.02


def _fmt(ms: float) -> str:
    return f"{ms:,.0f}" if ms >= 100 else f"{ms:.3g}"


def _ms(r):
    return r.get("ms", r.get("ms_50mhz"))


def main():
    rows = json.load(open(os.path.join(ROOT, "build", "table3_hw.json")))
    mini = json.load(open(os.path.join(ROOT, "build", "minisat_table3.json")))
    for r in rows:
        r["ms_ms"] = mini["results"][r["name"]]["ms"]
    clk_mhz = next((r["clk_hz"] / 1e6 for r in rows if "clk_hz" in r), 50.0)
    plt.rcParams.update({"font.family": "DejaVu Sans", "font.size": 9,
                         "axes.edgecolor": MUTED, "axes.labelcolor": INK2,
                         "xtick.color": INK2, "ytick.color": INK})

    W, H = 12.5, 15.4
    fig = plt.figure(figsize=(W, H), facecolor=SURFACE)
    gs = fig.add_gridspec(1, 2, width_ratios=[1.9, 1], wspace=0.62,
                          left=0.13, right=0.97, top=0.865, bottom=0.375)

    # ------------------------------------------------------------ header
    logo = mpimg.imread(LOGO)
    lh = 0.062
    lw = lh * logo.shape[1] / logo.shape[0] * (H / W)
    lx = fig.add_axes([0.06, 0.925, lw, lh])
    lx.imshow(logo)
    lx.axis("off")
    fig.text(0.06 + lw + 0.02, 0.964, "From the seed of the SAT-Accel grows PigWeed (SAT).",
             fontsize=14, fontweight="bold", color=INK, va="center")
    fig.text(0.06 + lw + 0.02, 0.940,
             "An Amaranth HDL implementation of SAT-Accel (Lo, Chang & Cong, FPGA ’25) "
             "on a Lattice ECP5-85F,\nmeasured on Manhattan Reasoning cloud FPGAs.",
             fontsize=9, color=INK2, va="center")

    # ---------------------------------------------------------- (a) Table 3
    ax = fig.add_subplot(gs[0, 0])
    series = [(f"PigWeedSAT — ECP5-85F @ {clk_mhz:g} MHz (measured)", C_ECP5, "ecp5"),
              ("SAT-Accel — U55C @ 230 MHz (paper)", C_SA, "sa_ms"),
              ("SAT-Hard — ZedBoard (paper)", C_SH, "sh_ms"),
              (f"MiniSat 2.2 — {mini['cpu']} (measured)", C_MS, "ms_ms")]
    h = 0.21
    for i, r in enumerate(rows):
        y = len(rows) - 1 - i
        for j, (_, color, key) in enumerate(series):
            val = (_ms(r) if r.get("status") == "ok" else None) if key == "ecp5" else r[key]
            yy = y + (1.5 - j) * h
            if val is None:
                note = ("PigWeedSAT: " + ("does not fit on-chip" if r.get("status") == "does not fit"
                                          else "out of clause memory")) if key == "ecp5" \
                    else "SAT-Accel: N/A (out of memory)"
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
                 fontsize=11, color=INK, fontweight="bold", pad=44)

    ok = [r for r in rows if r.get("status") == "ok"]
    gm = lambda xs: math.exp(sum(math.log(x) for x in xs) / len(xs))  # noqa: E731
    gm_sh = gm([r["sh_ms"] / _ms(r) for r in ok])
    gm_ms = gm([_ms(r) / r["ms_ms"] for r in ok])
    gm_sa = gm([_ms(r) / r["sa_ms"] for r in ok])

    # ---------------------------------------------------------- (b) Table 2
    bx = fig.add_subplot(gs[0, 1])
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
                 color=INK, fontweight="bold", pad=44)
    bx.text(0, 1.03, "solver 123 / 208 DP16KD · full SoC 147\nSAT-Accel (U55C): 419 BRAM + 778 URAM",
            transform=bx.transAxes, fontsize=7.5, color=INK2)

    # ---------------------------------------------------------- (c) platforms
    cx = fig.add_axes([0.06, 0.205, 0.91, 0.11])
    cx.axis("off")
    cx.set_title("(c) Platforms", loc="left", fontsize=11, color=INK, fontweight="bold", pad=6)
    cols = ["", "Board", "FPGA", "Node", "Clock", "LUTs", "FFs", "Block RAM", "DSPs",
            "Board DRAM", "Board cost"]
    cells = []
    for p in PLATFORMS:
        p = list(p)
        if p[4] is None:
            p[4] = f"{clk_mhz:g} MHz"
        cells.append(p)
    tb = cx.table(cellText=cells, colLabels=cols, loc="upper left", cellLoc="left",
                  colLoc="left", bbox=[0, 0.0, 1, 0.9],
                  colWidths=[0.14, 0.11, 0.115, 0.05, 0.06, 0.05, 0.05, 0.16, 0.05, 0.09, 0.075])
    tb.auto_set_font_size(False)
    tb.set_fontsize(7.6)
    for (r, c), cell in tb.get_celld().items():
        cell.set_edgecolor(GRID)
        cell.set_linewidth(0.6)
        cell.set_facecolor(SURFACE)
        cell.get_text().set_color(INK if r else INK2)
        if r == 0:
            cell.get_text().set_fontweight("bold")
        if c == 0 and r:
            cell.get_text().set_fontweight("bold")
            cell.get_text().set_color([C_ECP5, C_SA, C_SH][r - 1])

    # ------------------------------------------------------------ caption
    cap = (f"PigWeedSAT: on-chip solve cycles at {clk_mhz:g} MHz on a Manhattan Reasoning cloud ECP5-85F, "
           f"host load excluded; all {len(ok)} solved answers correct and SAT models re-verified.  "
           f"Geometric mean over those {len(ok)}: {gm_sh:.0f}× faster than SAT-Hard, "
           f"{gm_sa:.1f}× slower than SAT-Accel, {gm_ms:.1f}× slower than MiniSat on the M4 Pro.  "
           "SAT-Accel and SAT-Hard times from Lo et al., FPGA ’25, Table 3; SAT-Hard platform from "
           "Ustaoglu et al., DSD ’19 (clock not reported, n/r).  MiniSat 2.2: default options, median CPU time of 11 runs "
           "(includes parsing; ~1 ms floor).  Board cost: mean of single-unit US list prices "
           "(Oct 2026), board only, no host.  Compute spend: Claude Code session transcripts "
           "(tools/token_log.py); tokens as reported by the API, not dollars.")
    cap = "\n".join(textwrap.wrap(cap, 205))
    _compute_panel(fig)
    fig.text(0.06, 0.008, cap, fontsize=7.3, color=INK2, va="bottom", ha="left")
    out = os.path.join(ROOT, "build", "fig_paper_comparison")
    fig.savefig(out + ".png", dpi=200, facecolor=SURFACE)
    fig.savefig(out + ".pdf", facecolor=SURFACE)
    print(out + ".png")


if __name__ == "__main__":
    main()
