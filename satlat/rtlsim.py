"""Cycle-accurate simulation of design.SATAccel through its Wishbone port.

Runs the same load sequence as the host driver, then compares the result and
every solver counter against the golden model.

    python -m satlat.rtlsim SAT_test_cases/sat/uf20.dimacs [...]
"""

from __future__ import annotations

import sys
import time

from amaranth.sim import Simulator

from . import driver as DRV
from . import golden as G
from . import host as H
from .host import D

COMPARED = ["total", "decide", "retry", "backtrack", "reset", "learn_iter",
            "learn_merge", "min_iter", "min_merge", "simplified", "check_cnt",
            "deleted", "longest"]


def simulate(img, max_cycles: int = 50_000_000, poll_every: int = 2000,
             vcd: str | None = None):
    """Simulate one image, or a list of images solved back to back on one DUT
    (as on the board, where BRAM keeps whatever the previous solve left)."""
    imgs = img if isinstance(img, list) else [img]
    dut = D.SATAccel()
    outs: list = []

    async def bench(ctx):
      for img in imgs:
        out: dict = {}
        outs.append(out)
        async def wb(adr, dat=None):
            ctx.set(dut.wb_cyc, 1)
            ctx.set(dut.wb_stb, 1)
            ctx.set(dut.wb_we, dat is not None)
            ctx.set(dut.wb_adr, adr)
            if dat is not None:
                ctx.set(dut.wb_dat_w, dat & 0xFFFFFFFF)
            for _ in range(4):
                await ctx.tick()
                if ctx.get(dut.wb_ack):
                    break
            else:
                raise AssertionError(f"no ack at adr {adr}")
            val = ctx.get(dut.wb_dat_r)
            ctx.set(dut.wb_stb, 0)
            ctx.set(dut.wb_cyc, 0)
            ctx.set(dut.wb_we, 0)
            await ctx.tick()
            return val

        await ctx.tick()
        assert await wb(D.R_CAPS + 6) == D.MAGIC
        for word, value in DRV.config_ops(img):
            await wb(word, value)
        for sel, words in DRV.memory_images(img):
            if not words:
                continue
            await wb(D.R_MEMSEL, sel)
            await wb(D.R_MEMPTR, 0)
            for w in words:
                await wb(D.R_MEMDATA, w)
        # spot-check readback of the clause store
        await wb(D.R_MEMSEL, D.M_CS)
        await wb(D.R_MEMPTR, 0)
        rb = [await wb(D.R_MEMDATA) for _ in range(min(8, len(img.cls_store)))]
        want = [x & ((1 << D.CS_W) - 1) for x in img.cls_store[:len(rb)]]
        assert rb == want, f"clause-store readback {rb} != {want}"

        await wb(D.R_CTRL, 1)
        cycles = 0
        while True:
            await ctx.tick().repeat(poll_every)
            cycles += poll_every
            if await wb(D.R_CTRL) & 2:
                break
            if cycles > max_cycles:
                raise TimeoutError(f"no result after {cycles} cycles")
        out["result"] = H.s32(await wb(D.R_RESULT))
        out["stats"] = {n: await wb(D.R_STATS + i) for i, n in enumerate(D.STAT_NAMES)}
        out["cyc"] = [(await wb(D.R_CYCLES + 2 * k)) | ((await wb(D.R_CYCLES + 2 * k + 1)) << 32)
                      for k in range(D.N_PHASES)]
        if out["result"] == 1:
            h = out["stats"]["height"]
            await wb(D.R_MEMSEL, D.M_STK)
            await wb(D.R_MEMPTR, 0)
            out["stack"] = [H.s32(await wb(D.R_MEMWIN + (i % 256))) for i in range(h)]

    sim = Simulator(dut)
    sim.add_clock(1 / 50e6)
    sim.add_testbench(bench)
    if vcd:
        with sim.write_vcd(vcd):
            sim.run()
    else:
        sim.run()
    return outs if isinstance(img, list) else outs[0]


def check(path: str, cfg: H.Config | None = None, verbose=True) -> bool:
    return check_seq([path], cfg, verbose)


def check_seq(paths: list[str], cfg: H.Config | None = None, verbose=True) -> bool:
    """Solve several instances back to back on one simulated DUT."""
    imgs = [H.load_cnf(p, cfg) for p in paths]
    t = time.time()
    outs = simulate(imgs)
    dt = time.time() - t
    return all([_compare(p, img, out, dt, verbose) for p, img, out in zip(paths, imgs, outs)])


def _compare(path, img, out, dt, verbose) -> bool:
    gres, g = G.solve(img, 2_000_000)
    ok = out["result"] == gres
    gs = g.stats
    diffs = []
    for n in COMPARED:
        if getattr(gs, n) != out["stats"][n]:
            diffs.append(f"{n}: rtl={out['stats'][n]} golden={getattr(gs, n)}")
    if out["result"] == 1:
        bad = H.check_model(img.clauses, H.model_from_stack(img.num_vars, out["stack"]))
        if bad:
            diffs.append(f"model violates {len(bad)} clauses")
    ok = ok and not diffs
    if verbose:
        print(f"{'OK ' if ok else 'BAD'} {path}: rtl={out['result']} golden={gres} "
              f"cycles={out['stats']['cycles_lo']} sim {dt:.1f}s")
        for d in diffs:
            print("    ", d)
    return ok


if __name__ == "__main__":
    args = sys.argv[1:]
    if args and args[0] == "--seq":
        sys.exit(0 if check_seq(args[1:]) else 1)
    bad = sum(not check(p) for p in args)
    sys.exit(1 if bad else 0)
