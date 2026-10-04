### Table 2 — resource utilization

| | SAT-Accel (U55C, paper) | This work: solver only (ECP5-85F) | This work: full SoC incl. CPU+Ethernet |
|---|---|---|---|
| BRAM | 419 / 2,016 (21%) | 123 / 208 (59.1%) | 147 / 208 (70.7%) |
| DSP | 48 / 9,024 (1%) | 3 / 156 (1.9%) | 7 / 156 (4.5%) |
| FF | 324,891 / 2,607,360 (12%) | 3,997 / 83,640 (4.8%) | 6,642 / 83,640 (7.9%) |
| LUT | 251,283 / 1,303,680 (19%) | 10,224 / 83,640 (12.2%) | 17,551 / 83,640 (21.0%) |
| URAM | 778 / 960 (81%) | — (ECP5 has none) | — |

**Block RAM by module** (the ECP5 has no URAM, so the URAM-resident stores move into DP16KD; logic is one shared sequencer, so LUT/FF are not split by module):

| Module | DP16KD | share of solver BRAM | paper BRAM share | memories |
|---|---|---|---|---|
| Decision (VSIDS heap) | 6 | 5% | 2% | `pq_heap` 4, `pq_pos` 2 |
| Propagation | 6 | 5% | 35% | `cls_states` 6 |
| Learn | 5 | 4% | 51% | `merge_scratch` 2, `valid_learn` 1, `resolution` 2 |
| Min/Btrk | 4 | 3% | 1% | `min_scratch` 1, `valid_min` 1, `to_minimize` 1, `min_queue` 1 |
| Deletion | 6 | 5% | 0% | `bucket_next` 3, `free_cls_id` 3 |
| Cls Store | 24 | 20% | 9% (+URAM) | `cls_store` 14, `cmd` 6, `free_cls_pages` 4 |
| Tran. Store | 30 | 24% | 0% (+URAM) | `lit_store` 14, `occ` 12, `free_lit_pages` 4 |
| Tran<->Cls Position | 28 | 23% | 0% (+URAM) | `lit_to_cls` 14, `cls_to_lit` 14 |
| Variable state (lmd, trail) | 14 | 11% | — | `meta` 6, `lmmd` 1, `answer_stack` 2, `unit_by_cls` 2, `stack_end` 3 |
| **Total** | **123** | | | |

### Table 3 — SATLIB instances used by SAT-Hard

| Problem | Var | Cls | ECP5 result | ECP5 ms @50 MHz | SAT-Accel ms (U55C @230 MHz) | SAT-Hard ms | MiniSat ms (M4 Pro) | vs SAT-Hard | vs SAT-Accel | dec / confl / rst |
|---|---|---|---|---|---|---|---|---|---|---|
| hole7 | 56 | 204 | N/A — out of clause memory after 895 conflicts | — | 125 | 330 | 24.26 | — | — | — |
| hole8 | 72 | 297 | N/A — out of clause memory after 572 conflicts | — | 691 | 2,270 | 147.44 | — | — | — |
| hole9 | 90 | 415 | N/A — out of clause memory after 393 conflicts | — | N/A | 15,290 | 1552.32 | — | — | — |
| uf100-010 | 100 | 430 | SAT ✓ | 4.800 | 1 | 580 | 1.09 | 121x | 0.21x | 253 / 181 / 1 |
| uuf100-02 | 100 | 430 | UNSAT ✓ | 17.659 | 4 | 4,940 | 1.93 | 280x | 0.23x | 712 / 568 / 4 |
| uf125-01 | 125 | 538 | SAT ✓ | 11.769 | 4 | 1,160 | 1.27 | 99x | 0.34x | 492 / 374 / 2 |
| uuf125-05 | 125 | 538 | UNSAT ✓ | 41.417 | 7 | 4,900 | 2.82 | 118x | 0.17x | 1366 / 1088 / 6 |
| uf150-08 | 150 | 645 | SAT ✓ | 52.843 | 1 | 3,920 | 1.20 | 74x | 0.02x | 1477 / 1151 / 6 |
| CBS_k3_n100_m403_b10_1 | 100 | 403 | SAT ✓ | 6.573 | 2 | 2,340 | 1.06 | 356x | 0.30x | 321 / 238 / 2 |
| aim-200-3_4-yes1-4 | 200 | 680 | SAT ✓ | 4.273 | 4 | 1,200 | 0.95 | 281x | 0.94x | 436 / 173 / 1 |
| aim-200-1_6-no-4 | 200 | 320 | UNSAT ✓ | 0.388 | 0.3 | 10 | 0.93 | 26x | 0.77x | 236 / 24 / 0 |
| ii16e2 | 532 | 7825 | N/A — does not fit | — | 4 | 5,760 | 3.61 | — | — | — |
| ii32e1 | 222 | 1186 | SAT ✓ | 0.254 | 0.1 | 20 | 2.06 | 79x | 0.39x | 41 / 1 / 0 |

Speedup over SAT-Hard on the 9 solved instances: arithmetic mean 159x, geometric mean 122x (paper's SAT-Accel: avg 800x).  Relative to SAT-Accel on the U55C: geometric mean 0.25x over 9 instances (<1 = slower).
