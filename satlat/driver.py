"""Host driver: load an instance over the Wishbone register window and solve.

Bus-agnostic.  A bus provides word-addressed write(word, value),
write_fifo(word, values) (same address repeated -- the MEM_DATA push port)
and read(word).  `CloudBus` maps that onto the mrg SDK (byte addresses);
the RTL testbench supplies its own.
"""

from __future__ import annotations

import time

from . import host as H
from .host import D


def config_ops(img: H.Images) -> list[tuple[int, int]]:
    cfg = img.cfg
    return [
        (D.R_NVARS, img.num_vars),
        (D.R_NCLS, img.num_clauses),
        (D.R_LITELEMS, img.lit_elems),
        (D.R_CLSELEMS, img.cls_elems),
        (D.R_FIXED, img.fixed_height),
        (D.R_POSPHASE, int(cfg.positive_phase)),
        (D.R_LITPAGE, cfg.lit_page),
        (D.R_RESETMULT, cfg.reset_multiplier),
        (D.R_PRUNE, H.prune_q16(cfg.prune)),
        (D.R_INVDECAY, H.fp_encode(1.0 / cfg.decay)),
        (D.R_LOWCP, cfg.low_cls_pages),
        (D.R_LOWLP, cfg.low_lit_pages),
        (D.R_GLUE, cfg.glue_buckets),
        (D.R_HFLAGS, (int(cfg.used_bit) << D.HF_USED) | (int(cfg.min_abstract) << D.HF_MINABS)),
        (D.R_REPHASE, cfg.rephase),
    ]


def memory_images(img: H.Images) -> list[tuple[int, list[int]]]:
    """(MEM_SEL target, words) for every memory the host initialises."""
    u = H.u32
    return [
        (D.M_LS, [u(x) for x in img.lit_store]),
        (D.M_CS, [u(x) for x in img.cls_store]),
        (D.M_CMD, [start | (num << 16) for start, num in img.cmd]),
        (D.M_CST, [(comp & 0xFFFF) | (rem << 16) for comp, rem in img.cls_states]),
        (D.M_OCC, [w for (start, num, latest, free) in img.occ
                   for w in (start | (latest << 16), num | (free << 16))]),
        (D.M_STK, [u(x) for x in img.answer_stack]),
    ]


def check_caps(bus, img: H.Images) -> None:
    caps = [bus.read(D.R_CAPS + i) for i in range(8)]
    if caps[6] != D.MAGIC:
        raise RuntimeError(f"no SAT accelerator at this address (magic {caps[6]:#x})")
    if caps[7] < D.VERSION:
        raise RuntimeError(f"bitstream version {caps[7]} predates design.py version {D.VERSION}")
    want = [D.N_MAX, D.C_MAX, D.LE_MAX, D.CE_MAX, D.MAX_LEARN, D.FRAC_W]
    if caps[:6] != want:
        raise RuntimeError(f"bitstream capacities {caps[:6]} != design.py {want}")


def load(bus, img: H.Images) -> None:
    for word, value in config_ops(img):
        bus.write(word, value)
    for sel, words in memory_images(img):
        if not words:
            continue
        bus.write(D.R_MEMSEL, sel)
        bus.write(D.R_MEMPTR, 0)
        bus.write_fifo(D.R_MEMDATA, words)


def read_stats(bus) -> dict[str, int]:
    out = {n: bus.read(D.R_STATS + i) for i, n in enumerate(D.STAT_NAMES)}
    out["cycle_counters"] = [
        bus.read(D.R_CYCLES + 2 * k) | (bus.read(D.R_CYCLES + 2 * k + 1) << 32)
        for k in range(D.N_PHASES)
    ]
    return out


def read_stack(bus, height: int) -> list[int]:
    bus.write(D.R_MEMSEL, D.M_STK)
    bus.write(D.R_MEMPTR, 0)
    return [H.s32(x) for x in bus.read_fifo(height)]


def solve(bus, img: H.Images, timeout: float = 600.0, poll: float = 0.05, verbose=False):
    """Load, run, and return (result, stats, model-or-None)."""
    check_caps(bus, img)
    t0 = time.time()
    load(bus, img)
    t_load = time.time() - t0
    bus.write(D.R_CTRL, 1)
    t1 = time.time()
    while True:
        ctrl = bus.read(D.R_CTRL)
        if ctrl & 2:
            break
        if time.time() - t1 > timeout:
            raise TimeoutError("solver did not finish")
        time.sleep(poll)
    result = H.s32(bus.read(D.R_RESULT))
    stats = read_stats(bus)
    stats["host_load_s"] = t_load
    model = None
    if result == 1:
        stack = read_stack(bus, stats["height"])
        model = H.model_from_stack(img.num_vars, stack)
    return result, stats, model


class CloudBus:
    """Word-addressed adapter over an mrg App stream (byte addresses = 4 * word)."""

    def __init__(self, stream, chunk: int = 256):
        self.s = stream
        self.chunk = chunk

    def write(self, word: int, value: int) -> None:
        self.s.write(4 * word, value & 0xFFFFFFFF)

    def write_fifo(self, word: int, values: list[int]) -> None:
        for i in range(0, len(values), self.chunk):
            self.s.write(4 * word, [v & 0xFFFFFFFF for v in values[i:i + self.chunk]],
                         fixed_address=True)

    def read(self, word: int) -> int:
        return self.s.read(4 * word)

    def read_fifo(self, count: int) -> list[int]:
        out: list[int] = []
        while len(out) < count:
            n = min(256, count - len(out))
            r = self.s.read(4 * D.R_MEMWIN, n)
            out += [r] if n == 1 else r
        return out
