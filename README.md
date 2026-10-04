# PigWeedSAT

*An Amaranth HDL Implementation of SAT-Accel on the Lattice ECP5.*

The openhw-2025 U55C HLS CDCL SAT solver (SAT-Accel, FPGA '25;
`openhw-2025-SAT-FPGA/hls/src`), reimplemented in **Amaranth HDL** for a
**Lattice ECP5-85F** on the Manhattan Reasoning cloud-FPGA platform.  (Amaranth
is a genus of pigweed.)

| file | what |
|---|---|
| `design.py` | the hardware: one self-contained Amaranth Wishbone slave (`SATAccel`) |
| `satlat/host.py` | port of `host.cpp` `parseDIMACS`: CNF -> paged memory images |
| `satlat/golden.py` | sequential reference model of exactly what the RTL executes |
| `satlat/driver.py` | load/solve/readback over any bus (`CloudBus` = mrg stream) |
| `satlat/rtlsim.py` | cycle-accurate Amaranth simulation, checked against golden counter-for-counter |
| `app.py` | `mrg run app.py`: build, program a real board, solve, verify |
| `tests/` | `pytest -q tests` |

## Quick start

```sh
python -m venv .venv && .venv/bin/pip install manhattan-reasoning-gym amaranth==0.5.8 pytest
docker pull ghcr.io/manhattanreasoning/mrg-sandbox:latest

.venv/bin/python -m satlat.run_golden                      # golden model on all test cases
.venv/bin/python -m satlat.rtlsim SAT_test_cases/sat/uf20.dimacs   # RTL vs golden
.venv/bin/pytest -q tests
.venv/bin/mrg pnr design.py                                 # full-SoC fit + timing
.venv/bin/mrg run app.py                                    # real silicon
.venv/bin/mrg reset <fpga_id>                               # release the board afterwards
SAT_CNFS="path/a.cnf path/b.cnf" .venv/bin/mrg run app.py --no-program --fpga-id <id>
```

## Architecture

The HLS design is seven kernels joined by AXI streams.  On one ECP5 they
become one sequencer FSM over shared block RAM; every HLS function maps to a
group of states (see the header of `design.py`).  Data structures are the
originals, narrowed to ECP5 widths:

* **literal store**: per-literal occurrence lists in `LIT_PAGE`-word pages
  (data, prev-page, next-page), with `occ` metadata (start, latest page, count, free).
* **clause store**: 3 literals + next pointer per 4-word page, `cmd` = (start, length).
* **clause states**: XOR-compressed literal + remaining-unassigned counter
  (BCP finds units/conflicts without watched literals, as in the HLS).
* **location handler**: `cls_to_lit` / `lit_to_cls` maps for O(1) deletion.
* **VSIDS**: a binary heap with the HLS's index-order GET_UNDECIDED scan
  (1023-entry batches), hide/unhide, bump, decay, rescale.
* **learning**: 1-UIP resolution with the merge scratchpad and lazily-cleared
  valid bits, trail walk (`findNextCls`), resolution-based minimization, LBD.
* **restarts**: Luby x `RESET_MULTIPLIER`, pruning `PRUNE` of learned clauses
  from LBD buckets (highest LBD, oldest first) with the swap-delete.

### Capacity (ECP5-85F)

`design.py` top: `N_MAX=2048` variables, `C_MAX=4096` clauses,
`LE_MAX=CE_MAX=16384` store words, 1024-literal learned clauses.
Full-SoC PnR: 147/208 DP16KD, 17.5K/84K LUT, 7 DSP, user clock Fmax 63.5 MHz
(runs at 50 MHz).  Change the constants and re-run `mrg pnr`; the host
refuses instances that don't fit, and a solve that runs out of clause/page
memory returns the HLS codes `-4`/`-5`, as on the U55C.

### Deviations from the HLS (all documented in `satlat/golden.py`)

* VSIDS scores: exp8/frac17 float instead of IEEE double; rescale at 2^100.
* One minimizer instead of two timing-dependent parallel ones; one sequential
  BCP pipeline instead of 4 clause-state partitions (same algorithm, deterministic).
* Bug fixes: the rescaled bump keeps its rescaled score; `saveData` no longer
  leaks a clause page when a clause exactly fills a page; initial literal
  pages carry real prev-page pointers; `stack_end` is reset each solve.

## Host protocol

Word registers (byte address = 4 x word): `0` CTRL (W bit0 start / R bit0
busy, bit1 done), `1` RESULT (1 SAT, 0 UNSAT, <0 HLS error), `2..11` config,
`12..14` MEM_SEL / MEM_PTR / MEM_DATA, `16..23` capabilities + magic,
`32..51` statistics, `64..81` per-phase cycle counters (HLS `cycleCounter`).
Words 256..511 alias MEM_DATA so a normal 256-word burst streams a memory.
