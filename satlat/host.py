"""Host-side preprocessing: DIMACS -> the memory images the accelerator loads.

A port of parseDIMACS() from openhw-2025-SAT-FPGA/host/src/host.cpp, with the
same paged layouts:

  literal store  (per literal side, pages of LIT_PAGE words)
      slots 0..P-3  clause ids (cls+1) of clauses containing the literal
      slot  P-2     previous-page address      (host.cpp wrote 0, see below)
      slot  P-1     next-page address
  clause store   (per clause, pages of 4 words)
      slots 0..2    literals, slot 3 next-page address (0 on the last page)

One intentional difference: host.cpp left the previous-page slot of every
initial page at 0, which deleteTransposedClauses() follows when it frees a
list's tail page -- a latent corruption once a learned-clause page empties
back into an initial page.  We write the real previous-page address instead.
"""

from __future__ import annotations

import os
import struct
import sys
from dataclasses import dataclass, field

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import design as D  # noqa: E402


class Unsupported(Exception):
    """The instance does not fit the hardware capacity parameters."""


@dataclass
class Config:
    """configuration.json equivalents."""
    lit_page: int = 8            # _HOST_LITERAL_PAGE_SIZE (16 on the U55C)
    reset_multiplier: int = 100  # _HOST_RESET_MULTIPLIER
    positive_phase: bool = False  # _HOST_POSITIVE_LIT_PHASE_VAL
    decay: float = 0.95          # _HOST_DECAY_FACTOR
    prune: float = 0.1           # _HOST_PRUNE_PERCENTAGE


@dataclass
class Images:
    num_vars: int
    num_clauses: int
    clauses: list[list[int]]           # as parsed (deduped, sorted by |lit|)
    lit_store: list[int]               # LE words used
    cls_store: list[int]               # CE words used (signed literals / ptrs)
    cmd: list[tuple[int, int]]         # (addressStart, numElements) per clause
    cls_states: list[tuple[int, int]]  # (compressedList, remainingUnassigned)
    occ: list[tuple[int, int, int, int]]  # per 2*v+side: (start, num, latest, free)
    answer_stack: list[int]            # initial fixed decisions
    lit_elems: int
    cls_elems: int
    cfg: Config = field(default_factory=Config)

    @property
    def fixed_height(self) -> int:
        return len(self.answer_stack)


# ---------------------------------------------------------------- floats ----
FRAC_MASK = (1 << D.FRAC_W) - 1


def fp_encode(x: float) -> int:
    """Positive float -> exp:8|frac:17 (truncating), 0 -> 0."""
    if x <= 0:
        return 0
    import math
    m, e = math.frexp(x)          # x = m * 2^e, m in [0.5, 1)
    e -= 1
    m *= 2                        # m in [1, 2)
    exp = e + D.EXP_BIAS
    if not 0 < exp < (1 << D.EXP_W):
        raise ValueError(f"float {x} out of range")
    frac = int((m - 1.0) * (1 << D.FRAC_W))
    return (exp << D.FRAC_W) | frac


def fp_decode(v: int) -> float:
    if v == 0:
        return 0.0
    exp = v >> D.FRAC_W
    frac = v & FRAC_MASK
    return (1 + frac / (1 << D.FRAC_W)) * 2.0 ** (exp - D.EXP_BIAS)


# ---------------------------------------------------------------- DIMACS ----
def parse_dimacs(path: str) -> tuple[int, list[list[int]]]:
    nv = nc = None
    clauses: list[list[int]] = []
    cur: list[int] = []
    seen: set[int] = set()
    with open(path) as f:
        for line in f:
            tok = line.split()
            if not tok or tok[0] == "c" or tok[0] == "%":
                continue
            if tok[0] == "p":
                nv, nc = int(tok[2]), int(tok[3])
                continue
            for t in tok:
                v = int(t)
                if v == 0:
                    cur.sort(key=abs)
                    clauses.append(cur)
                    cur, seen = [], set()
                elif v not in seen:      # host.cpp drops repeated literals
                    seen.add(v)
                    cur.append(v)
    if cur:
        cur.sort(key=abs)
        clauses.append(cur)
    if nv is None:
        raise ValueError(f"{path}: no 'p cnf' header")
    clauses = [c for c in clauses if c][:nc]
    return nv, clauses


def build_images(num_vars: int, clauses: list[list[int]], cfg: Config | None = None) -> Images:
    cfg = cfg or Config()
    P = cfg.lit_page
    if P < 4:
        raise ValueError("lit_page must be >= 4")
    if num_vars > D.N_MAX:
        raise Unsupported(f"{num_vars} variables > N_MAX {D.N_MAX}")
    if len(clauses) > D.C_MAX:
        raise Unsupported(f"{len(clauses)} clauses > C_MAX {D.C_MAX}")

    occ_lists: list[list[list[int]]] = [[[] for _ in range(num_vars)] for _ in range(2)]
    decision: set[int] = set()
    for i, c in enumerate(clauses):
        for x in c:
            occ_lists[0 if x > 0 else 1][abs(x) - 1].append(i + 1)
        if len(c) == 1:
            decision.add(c[0])

    # Clause store: 3 literals per 4-word page, slot 3 = next page.
    cs: list[int] = []
    cmd: list[tuple[int, int]] = []
    states: list[tuple[int, int]] = []
    for c in clauses:
        start = len(cs)
        x = 0
        for j, lit in enumerate(c):
            cs.append(lit)
            x ^= lit
            if len(cs) % 4 == 3:
                cs.append(len(cs) + 1 if j != len(c) - 1 else 0)
        while len(cs) % 4:
            cs.append(0)
        cmd.append((start, len(c)))
        states.append((x, len(c)))
    if len(cs) > D.CE_MAX:
        raise Unsupported(f"clause store needs {len(cs)} words > CE_MAX {D.CE_MAX}")

    # Literal store: per (variable, side) a chain of P-word pages.
    ls: list[int] = []
    occ: list[tuple[int, int, int, int]] = []
    for v in range(num_vars):
        if not occ_lists[0][v] and not occ_lists[1][v]:
            here = len(ls)
            occ += [(here, 0, here, 0), (here, 0, here, 0)]
            decision.add(v + 1)          # unconstrained variable -> fixed +v
            continue
        for side in (0, 1):
            entries = occ_lists[side][v]
            start = len(ls)
            page = start
            prev = 0
            idx = 0
            page_buf = [0] * P
            for cid in entries:
                page_buf[idx] = cid
                idx += 1
                if idx == P - 2:
                    nxt = page + P
                    page_buf[P - 2] = prev
                    page_buf[P - 1] = nxt
                    ls += page_buf
                    prev, page, idx = page, nxt, 0
                    page_buf = [0] * P
            page_buf[P - 2] = prev
            ls += page_buf
            occ.append((start, len(entries), page, P - idx - 2))
    if len(ls) > D.LE_MAX:
        raise Unsupported(f"literal store needs {len(ls)} words > LE_MAX {D.LE_MAX}")

    return Images(
        num_vars=num_vars, num_clauses=len(clauses), clauses=clauses,
        lit_store=ls, cls_store=cs, cmd=cmd, cls_states=states, occ=occ,
        answer_stack=sorted(decision), lit_elems=len(ls), cls_elems=len(cs),
        cfg=cfg,
    )


def load_cnf(path: str, cfg: Config | None = None) -> Images:
    nv, clauses = parse_dimacs(path)
    return build_images(nv, clauses, cfg)


def check_model(clauses: list[list[int]], assignment: dict[int, bool]) -> list[int]:
    """Indices of clauses the assignment leaves unsatisfied (empty = valid)."""
    bad = []
    for i, c in enumerate(clauses):
        if not any(assignment.get(abs(x), False) == (x > 0) for x in c):
            bad.append(i)
    return bad


def model_from_stack(num_vars: int, stack: list[int]) -> dict[int, bool]:
    a = {v: False for v in range(1, num_vars + 1)}
    for x in stack:
        if x:
            a[abs(x)] = x > 0
    return a


def prune_q16(p: float) -> int:
    return int(p * 65536)


def u32(x: int) -> int:
    return x & 0xFFFFFFFF


def s32(x: int) -> int:
    return struct.unpack("<i", struct.pack("<I", x & 0xFFFFFFFF))[0]
