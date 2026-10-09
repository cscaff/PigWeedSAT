"""Bucket every CNF in the Global Benchmark Database by whether it fits an ECP5-85F.

    python tools/gbd_tiers.py                  # -> build/gbd_tiers.csv + build/gbd_tiers.md
    python tools/gbd_tiers.py --tier S --tier M --csv picks.csv

GBD (benchmark-database.de) publishes two SQLite databases: `meta` (track,
family, result) and `base` (size features).  Both are cached in build/gbd/.
Literal count is clauses * vcg_cdegree_mean (mean clause length), so it is exact
up to rounding.

Tiers, from smallest to largest (the first that applies wins):

  S        loads on PigWeedSAT as built, with LEARN x the original free in every store
  S-tight  loads on PigWeedSAT, but without that much room to learn
  M        fits a full-chip single-85F design with LEARN x the original of learned clauses
  X        fits that design with no room to learn (stretch: needs a cleverer design)
  OUT      does not fit the solver budget, but the clause literals alone would fit on-chip
  IMPOSSIBLE  the clause literals alone exceed all on-chip RAM (EBR + distributed)

The S rules mirror satlat/host.py build_images(); the M/X rules are the
idealized model in ideal_bits() and competition_tiers.md in the FPSAT repo.  Each instance is
downloadable at https://benchmark-database.de/file/<hash>.
"""

import argparse
import collections
import csv
import math
import os
import sqlite3
import sys
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import design as D  # noqa: E402

GBD = "https://benchmark-database.de/getdatabase/"
CACHE = os.path.join(ROOT, "build", "gbd")

# LFE5U-85: 208 sysMEM blocks x 18 Kb = 3,744 Kb EBR; 669 Kb distributed RAM
# (Lattice ECP5 family data sheet FPGA-DS-02012, family selection table).
EBR_BLOCKS, EBR_BITS = 208, 18 * 1024
DIST_BITS = 669 * 1024
SOC_BLOCKS = 25            # platform VexRiscv + LiteEth (mrg base SoC)
BUDGET = (EBR_BLOCKS - SOC_BLOCKS) * EBR_BITS
PACKING = 0.8              # DP16KD widths are 1/2/4/9/18/36; fields rarely pack perfectly
# Peak live learned clauses / original clauses, MiniSat 2.2 median over the 40-instance
# sample (zero-learning runs excluded): FPSAT analysis/hw_clause_capacity/learned_summary.csv.
LEARN = 1.86
TIERS = ["S", "S-tight", "M", "X", "OUT", "IMPOSSIBLE"]


def fetch(name):
    os.makedirs(CACHE, exist_ok=True)
    path = os.path.join(CACHE, name + ".db")
    if not os.path.exists(path):
        print(f"downloading {GBD}{name} -> {path}", file=sys.stderr)
        urllib.request.urlretrieve(GBD + name, path)
    return path


def bits(n):
    return max(1, math.ceil(math.log2(n + 1)))


def pigweed_words(v, c, lits, cls):
    """Clause- and literal-store words of host.build_images(); the literal store
    is an upper bound since GBD has no per-variable occurrence counts."""
    cls10_lits = lits - sum(k * cls[k] for k in range(1, 10))
    cs = 4 * (sum(math.ceil(k / 3) * cls[k] for k in range(1, 10)) + cls10_lits / 3 + cls[10])
    ls = min(p * (2 * v + lits / (p - 2)) for p in (4, 8, 16))
    return cs, ls


def ideal_bits(v, c, lits, learn):
    """A compact single-chip CDCL: packed clause arena, two watches per clause,
    per-variable trail/heap state.  `learn` = learned-clause room as a multiple
    of the original formula."""
    lw = bits(2 * v)
    db_lits, db_cls = lits * (1 + learn), c * (1 + learn)
    cw = bits(db_lits + db_cls)                       # clause address width
    arena = db_lits * lw + db_cls * 32                # literals + header (len, LBD, flags)
    watches = 2 * db_cls * cw
    per_var = v * (3 * bits(v) + cw + 32)             # level, heap idx, trail, reason, score+flags
    return (arena + watches + per_var) / PACKING


def tier(v, c, lits, cls):
    cs, ls = pigweed_words(v, c, lits, cls)
    if v <= D.N_MAX and c <= D.C_MAX and cs <= D.CE_MAX and ls <= D.LE_MAX:
        k = 1 + LEARN
        roomy = c * k <= D.C_MAX and cs * k <= D.CE_MAX and ls * k <= D.LE_MAX
        return "S" if roomy else "S-tight"
    if ideal_bits(v, c, lits, LEARN) <= BUDGET:
        return "M"
    if ideal_bits(v, c, lits, 0.0) <= BUDGET:
        return "X"
    if lits * bits(2 * v) <= EBR_BLOCKS * EBR_BITS + DIST_BITS:
        return "OUT"
    return "IMPOSSIBLE"


def num(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def load():
    db = sqlite3.connect(fetch("base"))
    db.execute(f"ATTACH '{fetch('meta')}' AS meta")
    tracks = collections.defaultdict(list)
    for h, t in db.execute("SELECT hash, value FROM meta.track"):
        if t != "None":
            tracks[h].append(t)
    cols = ", ".join(f"b.cls{k}" for k in range(1, 10)) + ", b.cls10p"
    q = f"""SELECT b.hash, b.variables, b.clauses, b.vcg_cdegree_mean, {cols},
                   m.family, m.result, m.filename
            FROM features b JOIN meta.features m USING (hash)"""
    rows = []
    for r in db.execute(q):
        h, v, c, mean = r[0], num(r[1]), num(r[2]), num(r[3])
        cls = [0] + [num(x) or 0 for x in r[4:14]]
        if not v or not c or mean is None:
            continue
        lits = round(c * mean)
        rows.append(dict(hash=h, variables=int(v), clauses=int(c), literals=lits,
                         tier=tier(v, c, lits, cls), result=r[15], family=r[14],
                         tracks=" ".join(sorted(tracks[h])), filename=r[16],
                         url=f"https://benchmark-database.de/file/{h}"))
    return rows


def summary(rows):
    by = collections.Counter(r["tier"] for r in rows)
    out = ["| tier | instances | sat | unsat | unknown | families |", "|---|---|---|---|---|---|"]
    for t in TIERS:
        rs = [r for r in rows if r["tier"] == t]
        res = collections.Counter(r["result"] for r in rs)
        fams = len({r["family"] for r in rs})
        out.append(f"| {t} | {by[t]} | {res['sat']} | {res['unsat']} | "
                   f"{len(rs) - res['sat'] - res['unsat']} | {fams} |")
    for t in ["S", "S-tight", "M", "X"]:
        fam = collections.Counter(r["family"] for r in rows if r["tier"] == t)
        out += ["", f"**{t}** top families: " +
                ", ".join(f"{f} ({n})" for f, n in fam.most_common(15))]
    small = [r for r in rows if r["tier"] in ("S", "S-tight", "M", "X")]
    trk = collections.Counter(t.split("_")[0] for r in small for t in r["tracks"].split())
    out += ["", "Tracks contributing S..X instances: " +
            ", ".join(f"{t} ({n})" for t, n in trk.most_common())]
    return "\n".join(out)


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--csv", default=os.path.join(ROOT, "build", "gbd_tiers.csv"))
    ap.add_argument("--md", default=os.path.join(ROOT, "build", "gbd_tiers.md"))
    ap.add_argument("--tier", action="append", choices=TIERS, help="only write these tiers to the CSV")
    a = ap.parse_args()
    rows = sorted(load(), key=lambda r: (TIERS.index(r["tier"]), r["literals"]))
    keep = [r for r in rows if not a.tier or r["tier"] in a.tier]
    os.makedirs(os.path.dirname(a.csv), exist_ok=True)
    with open(a.csv, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0]))
        w.writeheader()
        w.writerows(keep)
    s = summary(rows)
    with open(a.md, "w") as f:
        f.write(f"# GBD instances by ECP5-85F tier ({len(rows)} CNFs)\n\n{s}\n")
    print(s)
    print(f"\n{len(keep)} rows -> {a.csv}\nsummary -> {a.md}")


if __name__ == "__main__":
    main()
