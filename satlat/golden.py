"""Golden reference model of the accelerator.

The HLS design is seven communicating kernels (solver, clause_store_handler,
location_handler, pqHandler, restartCalculator, timer, message) whose
dataflow regions run concurrently.  The algorithm they implement is
sequentially deterministic up to stream timing, and the RTL in design.py
executes it as one sequencer.  This model is that sequence, operation for
operation, so RTL simulation can be checked against it counter-for-counter.

Mapping to the HLS sources:
  bcp()            discover.cpp: discover/colorStream/updateStatesForward/
                   controlSink, decide.cpp: checkUndecided
  learn()          learn.cpp: learnClause, merge_resolution_sort(+part_2),
                   findNextCls, writeClauseStream/saveClause
  _undo()          backtrack.cpp: undoStates/updateStatesBackward
  _minimize()      minimize.cpp
  _save_clause()   clause_store_handler.cpp: SAVE/saveData + location SAVE
  _alloc_page()    manage.cpp: allocatePage
  _prune()         clause_store_handler.cpp: DELETE/getDeletedClsID/
                   deleteClauses + manage.cpp: deleteTransposedClauses +
                   location_handler.cpp UPDATE
  pq_*             priority_queue_functions.cpp + pq_handler.cpp
  _luby()          restart.cpp

Documented deviations from the HLS (each a latent bug there, or a
U55C-only resource):
  * VSIDS scores are an exp8/frac17 positive float, not IEEE double, and the
    rescale threshold is 2^100 instead of 1e100.  The bumped element's own
    score is rescaled too (HLS kept the pre-rescale value).
  * One minimizer instead of two parallel ones sharing work by timing.
  * saveData no longer grabs (and leaks) an extra clause page when a clause
    exactly fills its last page.
  * Prev-page pointers of initial literal pages are valid (see host.py).
"""

from __future__ import annotations

from dataclasses import dataclass, field

from . import host as H
from .host import D

FRAC = D.FRAC_W
FMASK = (1 << FRAC) - 1
ONE = D.EXP_BIAS << FRAC
RESCALE_AT = (D.EXP_BIAS + D.RESCALE_EXP)
SCAN_BATCH = 1023


def fp_add(a: int, b: int) -> int:
    if a == 0:
        return b
    if b == 0:
        return a
    if a < b:
        a, b = b, a           # larger encoding = larger exponent (or equal)
    ea, ma = a >> FRAC, (a & FMASK) | (1 << FRAC)
    eb, mb = b >> FRAC, (b & FMASK) | (1 << FRAC)
    d = ea - eb
    mb = mb >> d if d <= FRAC + 1 else 0
    s = ma + mb
    if s >> (FRAC + 1):
        s >>= 1
        ea += 1
    return (ea << FRAC) | (s & FMASK)


def fp_mul(a: int, b: int) -> int:
    if a == 0 or b == 0:
        return 0
    ea, ma = a >> FRAC, (a & FMASK) | (1 << FRAC)
    eb, mb = b >> FRAC, (b & FMASK) | (1 << FRAC)
    e = ea + eb - D.EXP_BIAS
    p = ma * mb
    if p >> (2 * FRAC + 1):
        m = p >> (FRAC + 1)
        e += 1
    else:
        m = p >> FRAC
    return (e << FRAC) | (m & FMASK)


def fp_rescale(a: int) -> int:
    e = a >> FRAC
    if e <= D.RESCALE_EXP:
        return 0
    return a - (D.RESCALE_EXP << FRAC)


class SolverError(Exception):
    pass


@dataclass
class VarMeta:          # the non-list half of literalMetaData
    insert_lvl: int = 0
    dec_lvl: int = 0
    in_stack: bool = False
    phase: int = 0
    unit_by_lit: int = 0
    shortest: int = 0


@dataclass
class Lmmd:             # literalMinimizeMetaData
    min_keep: int = 0
    decide: bool = False
    fix: bool = False


class FreeList:
    """mmuStream: a bump allocator from `start` to `limit`, then a ring."""

    def __init__(self, start: int, limit: int, inc: int):
        self.next, self.limit, self.inc = start, limit, inc
        self.ring: list[int] = []

    def size(self) -> int:
        return (self.limit - self.next) // self.inc + len(self.ring)

    def empty(self) -> bool:
        return self.size() == 0

    def read(self) -> int:
        if self.next + self.inc <= self.limit:
            v = self.next
            self.next += self.inc
            return v
        return self.ring.pop(0)

    def write(self, v: int) -> None:
        self.ring.append(v)


@dataclass
class Stats:
    total: int = 0
    decide: int = 0
    retry: int = 0
    backtrack: int = 0
    reset: int = 0
    learn_iter: int = 0
    learn_merge: int = 0
    min_iter: int = 0
    min_merge: int = 0
    simplified: int = 0
    longest: int = 0
    longest_simplified: int = 0
    check_cnt: int = 0
    deleted: int = 0
    lbd: list[int] = field(default_factory=lambda: [0] * D.LBD_BUCKETS)


class Golden:
    def __init__(self, img: H.Images):
        self.img = img
        cfg = img.cfg
        self.N = img.num_vars
        self.P = cfg.lit_page
        self.POS = 1 if cfg.positive_phase else 0
        self.RESET_MULT = cfg.reset_multiplier
        self.PRUNE = H.prune_q16(cfg.prune)
        self.INV_DECAY = H.fp_encode(1.0 / cfg.decay)

        self.ls = list(img.lit_store) + [0] * (D.LE_MAX - len(img.lit_store))
        self.cs = list(img.cls_store) + [0] * (D.CE_MAX - len(img.cls_store))
        self.cmd = list(img.cmd) + [(0, 0)] * (D.C_MAX - len(img.cmd))
        self.st = [list(s) for s in img.cls_states] + [[0, 0] for _ in range(D.C_MAX - len(img.cls_states))]
        self.occ = [list(o) for o in img.occ]
        self.stack = list(img.answer_stack) + [0] * (self.N - len(img.answer_stack))
        self.meta = [VarMeta(phase=1 - self.POS) for _ in range(self.N)]
        self.lmmd = [Lmmd() for _ in range(self.N)]
        self.unit_by_cls = [0] * self.N
        self.stack_end = [0] * (2 * D.N_MAX)
        self.cls_to_lit = [0] * D.CE_MAX     # location_handler mClsToLitStorePos
        self.lit_to_cls = [0] * D.LE_MAX     # location_handler mLitToClsStorePos

        # learn / minimize scratch
        self.scratch = [(0, 0)] * self.N
        self.valid = [False] * self.N
        self.scratch_m = [0] * self.N
        self.valid_m = [False] * self.N

        # pqHandler state (loadPositioning)
        self.heap = [[0, v + 1] for v in range(self.N)]
        self.pos = list(range(self.N))
        self.remaining = self.N
        self.mult = ONE

        # clause_store_handler state
        self.free_lit_pages = FreeList(img.lit_elems, D.LE_MAX, self.P)
        self.free_cls_pages = FreeList(img.cls_elems, D.CE_MAX, D.CLS_PAGE)
        self.free_cls_id = FreeList(img.num_clauses, D.C_MAX, 1)
        self.buckets: list[list[int]] = [[] for _ in range(D.LBD_BUCKETS)]
        self.used_total = 0
        self.last_inserted = -1

        # restartCalculator (Knuth's reluctant doubling, primed past luby(0))
        self.luby_u, self.luby_v = 2, 1

        self.height = 0
        self.fixed = img.fixed_height
        self.commit_idx = -1
        self.stats = Stats()

    # ------------------------------------------------------------ helpers --
    @staticmethod
    def _v(x: int) -> int:
        return abs(x) - 1

    def _occ(self, x: int, walk: bool) -> list[int]:
        """occurrence-list index for literal x.

        walk=True: the list a *true* x falsifies (clauses containing -x);
        walk=False: the list holding clauses containing x itself."""
        side = (1 if x > 0 else 0) if walk else (1 if x < 0 else 0)
        return self.occ[2 * self._v(x) + side]

    def _walk_list(self, o):
        start, num = o[0], o[1]
        addr, idx = start, 0
        for _ in range(num):
            yield self.ls[addr]
            addr += 1
            idx += 1
            if idx == self.P - 2:
                addr = self.ls[addr + 1]
                idx = 0

    def _clause(self, c: int):
        """literals of clause index c (0-based) from the paged clause store."""
        start, num = self.cmd[c]
        addr, sub = start, 0
        for k in range(num):
            yield self.cs[addr]
            addr += 1
            sub += 1
            if sub == D.CLS_PAGE - 1 and k != num - 1:
                addr = self.cs[addr]
                sub = 0

    def _luby(self) -> int:
        v = self.luby_v
        u = self.luby_u
        if (u & -u) == v:
            self.luby_u, self.luby_v = u + 1, 1
        else:
            self.luby_v = 2 * v
        return v

    # ----------------------------------------------------------------- pq --
    def pq_swap_lower(self, x, p, rem):
        save = p
        while True:
            l = 2 * p + 1
            left = self.heap[l] if l < self.N else [0, 0]
            right = self.heap[l + 1] if l + 1 < self.N else [0, 0]
            ls = left[0] if l < rem else 0
            rs = right[0] if l + 1 < rem else 0
            e1, e2, e3 = ls > x[0], ls >= rs, rs > x[0]
            use, save, p = left, p, l
            done = False
            if e3 and not e2:
                use, p = right, l + 1
            elif not e1 and not e3:
                use, done = x, True
            self.heap[save] = list(use)
            self.pos[use[1] - 1] = save
            if done:
                break
        self.pos[x[1] - 1] = save
        self.heap[save] = list(x)

    def pq_swap_higher(self, x, p):
        save = p
        while p != 0:
            np_ = (p - 1) // 2
            parent = self.heap[np_]
            if parent[0] < x[0]:
                self.heap[p] = list(parent)
                self.pos[parent[1] - 1] = p
                save = np_
            else:
                break
            p = np_
        self.pos[x[1] - 1] = save
        self.heap[save] = list(x)

    def pq_hide(self, var):
        p = self.pos[var - 1]
        r = self.remaining - 1
        swap, last = list(self.heap[p]), list(self.heap[r])
        self.heap[p] = list(last)
        self.pos[last[1] - 1] = p
        self.heap[r] = swap
        self.pos[var - 1] = r
        self.remaining = r
        self.pq_swap_lower(last, p, r)

    def pq_unhide(self, var):
        p = self.pos[var - 1]
        if p < self.remaining:
            return
        r = self.remaining
        swap, last = list(self.heap[p]), list(self.heap[r])
        self.heap[p] = list(last)
        self.pos[last[1] - 1] = p
        self.heap[r] = swap
        self.pos[var - 1] = r
        self.pq_swap_higher(swap, r)
        self.remaining = r + 1

    def pq_bump(self, var):
        p = self.pos[var - 1]
        s = fp_add(self.heap[p][0], self.mult)
        self.heap[p][0] = s
        if (s >> FRAC) >= RESCALE_AT:
            for i in range(self.N):
                self.heap[i][0] = fp_rescale(self.heap[i][0])
            self.mult = fp_rescale(self.mult)
            s = self.heap[p][0]
        if p < self.remaining:
            self.pq_swap_higher([s, var], p)

    def pq_decay(self):
        self.mult = fp_mul(self.mult, self.INV_DECAY)

    # ---------------------------------------------------------------- run --
    def run(self, max_iterations: int | None = None) -> int:
        """Returns 1 (SAT), 0 (UNSAT) or a negative HLS error code."""
        N = self.N
        do_bt = False
        level = 1 if self.fixed == 0 else 0
        use_flipped = False
        flipped = 0
        limit, limit_count = self.RESET_MULT, 0
        s = self.stats
        while True:
            s.total += 1
            if max_iterations is not None and s.total > max_iterations:
                raise SolverError("iteration limit")
            if not do_bt:
                if use_flipped:
                    s.retry += 1
                else:
                    s.decide += 1
                top = 0
                if not use_flipped and level != 0:
                    # pqHandler GET_UNDECIDED: scan heap slots 0,1,2,... until an
                    # unassigned variable; hide everything scanned.  The HLS
                    # removeLiterals stream holds 1023, so a longer scan hides
                    # that batch and restarts from slot 0.
                    while not top:
                        scanned, inc = [], 0
                        while True:
                            var = self.heap[inc][1]
                            scanned.append(var)
                            inc += 1
                            if not self.meta[var - 1].in_stack:
                                top = var
                                break
                            if len(scanned) == SCAN_BATCH:
                                break
                        for var in scanned:
                            self.pq_hide(var)
                do_bt = self.bcp(level, top, use_flipped, flipped)
                self.stack_end[level] = self.height
                if not do_bt:
                    level += 1
                if level == 0:
                    return 0
                if self.height == N and not do_bt:
                    return 1
                use_flipped = False
            else:
                s.backtrack += 1
                reset_all = False
                limit_count += 1
                if limit == limit_count:
                    limit_count = 0
                    limit = self.RESET_MULT * self._luby()
                    reset_all = True
                    s.reset += 1
                err, level, ins0, ins1, given = self.learn(level, reset_all)
                if err < 0:
                    return err
                if reset_all:
                    level = 0
                    self._prune()
                do_bt = False
                if level == 0:
                    if ins0 != 0:
                        self.stack[self.height] = ins0
                        self.fixed += 1
                    else:
                        level = 1
                    if self.fixed == 0:
                        level = 1
                    use_flipped = False
                else:
                    use_flipped = True
                    flipped = ins1
                    self.unit_by_cls[self._v(ins1)] = given + 1
                    self.lmmd[self._v(ins1)].decide = False

    # ---------------------------------------------------------------- bcp --
    def bcp(self, level, top, use_flipped, flipped) -> bool:
        start = self.height
        if not use_flipped:
            if level == 0:
                for i in range(self.height, self.fixed):
                    g = self.stack[i]
                    m = self.meta[self._v(g)]
                    m.unit_by_lit, m.insert_lvl, m.dec_lvl, m.in_stack = 0, self.height, 0, True
                    m.phase = self.POS if g > 0 else 1 - self.POS
                    mm = self.lmmd[self._v(g)]
                    mm.decide = mm.fix = True
                    self.height += 1
            else:
                g = top
                m = self.meta[g - 1]
                m.unit_by_lit, m.insert_lvl, m.dec_lvl, m.in_stack = 0, self.height, level, True
                mm = self.lmmd[g - 1]
                mm.decide, mm.fix = True, False
                if m.phase != self.POS:
                    g = -g
                self.stack[self.height] = g
                self.height += 1
        else:
            g = flipped
            m = self.meta[self._v(g)]
            m.insert_lvl, m.dec_lvl, m.in_stack, m.unit_by_lit = self.height, level, True, 0
            m.phase = self.POS if g > 0 else 1 - self.POS
            self.stack[self.height] = g
            self.height += 1

        qh = start
        conflict = False
        self.unsat: list[int] = []
        while qh < self.height and not conflict:
            L = self.stack[qh]
            qh += 1
            self.stats.check_cnt += 1
            for c1 in self._walk_list(self._occ(L, walk=True)):
                st = self.st[c1 - 1]
                st[0] ^= -L
                st[1] -= 1
                if st[1] == 1 and not conflict:
                    self._unit(st[0], c1, L, level)
                elif st[1] == 0:
                    if len(self.unsat) < 2:
                        self.unsat.append(c1)
                    conflict = True
        self.commit_idx = qh - 1
        return conflict

    def _unit(self, U, c1, L, level):
        length = self.cmd[c1 - 1][1]
        v = self._v(U)
        m = self.meta[v]
        if not m.in_stack:
            m.insert_lvl, m.dec_lvl, m.in_stack, m.unit_by_lit = self.height, level, True, L
            self.unit_by_cls[v] = c1
            m.shortest = length
            mm = self.lmmd[v]
            mm.decide = False
            if level == 0:
                mm.fix = True
                self.fixed += 1
            self.stack[self.height] = U
            self.height += 1
        elif self.stack[m.insert_lvl] == U and m.unit_by_lit == L and length < m.shortest:
            self.unit_by_cls[v] = c1
            m.shortest = length

    # -------------------------------------------------------------- learn --
    def learn(self, level, reset_all):
        s = self.stats
        best, best_len = 0, (1 << 32) - 2
        for c in self.unsat:
            ln = self.cmd[c - 1][1]
            if ln < best_len:
                best, best_len = c, ln
        next_c = best

        self.rc: list[int] = []
        num_el = 0
        rc = [0] * (2 * D.MAX_LEARN)       # RTL buffer: 2x headroom, wraps
        stream_size = highest_il = fixed_cnt = 0
        trail_end = 0
        set_once = False
        is_uip = found_abs = False
        error = 0
        while not (is_uip or found_abs):
            s.learn_iter += 1
            # ---- merge_resolution_sort + part_2
            rh, res_pos = 0, 0
            poss = rc[num_el - 1] if num_el > 0 else 0
            once = highest_il != 0
            for x in self._clause(next_c - 1):
                s.learn_merge += 1
                v = self._v(x)
                st, pos = self.scratch[v] if self.valid[v] else (0, 0)
                self.valid[v] = True
                insert = False
                bit = 1 if x > 0 else 2
                if not st & bit:
                    st |= bit
                    insert = True
                if st == 3:
                    if pos != num_el - 1:
                        rc[pos] = poss
                        res_pos = pos
                        rh = 1
                    else:
                        rh = 2
                    num_el -= 1
                    self.scratch[v] = (0, 0)
                    mm = self.lmmd[v]
                    if mm.fix:
                        fixed_cnt -= 1
                    mm.min_keep = 0
                elif insert:
                    rc[num_el % (2 * D.MAX_LEARN)] = x
                    pos = num_el
                    num_el += 1
                    if rh == 0:
                        poss = x
                    self.scratch[v] = (st, pos)
                    m = self.meta[v]
                    if m.dec_lvl > 0:
                        self.pq_bump(v + 1)
                    if m.dec_lvl == level:
                        if highest_il < m.insert_lvl and not once:
                            highest_il = m.insert_lvl
                        stream_size += 1
                    mm = self.lmmd[v]
                    if mm.fix:
                        fixed_cnt += 1
                    mm.min_keep = 1
            if rh == 1:
                pv = self._v(poss)
                self.scratch[pv] = (self.scratch[pv][0], res_pos)
            # ----
            if not set_once:
                trail_end = highest_il
                set_once = True
            s.longest = max(s.longest, num_el)
            if num_el > D.MAX_LEARN:
                error = -2
                break
            if num_el == 1 or num_el - fixed_cnt == 1:
                found_abs = True
            if stream_size == 1:
                is_uip = True
            else:
                i = trail_end
                save = trail_end
                while i >= 0:
                    t = self.stack[i]
                    v = self._v(t)
                    trail_end -= 1
                    s.learn_merge += 1
                    if self.valid[v] and self.scratch[v][0] in (1, 2):
                        next_c = self.unit_by_cls[v]
                        save = trail_end
                        break
                    i -= 1
                trail_end = save
                stream_size -= 1

        self.pq_decay()
        self.valid = [False] * self.N
        if error:
            return error, level, 0, 0, 0
        rc = rc[:num_el]

        non_rem = 0
        to_min: list[int] = []
        level_before = -1
        uip = 0
        ins0 = 0
        if not reset_all and not found_abs:
            for x in rc:
                v = self._v(x)
                m, mm = self.meta[v], self.lmmd[v]
                if not mm.fix and m.dec_lvl != 0 and level_before < m.dec_lvl and m.dec_lvl != level:
                    level_before = m.dec_lvl
                if m.dec_lvl == level:
                    uip = x
                if mm.decide and not mm.fix:
                    non_rem += 1
                if not (mm.fix or mm.decide):
                    to_min.append(x)
            target = self.stack_end[level_before]
        else:
            if found_abs:
                for x in rc:
                    if not self.lmmd[self._v(x)].fix:
                        ins0 = x
            else:
                for x in rc:
                    mm = self.lmmd[self._v(x)]
                    if mm.decide and not mm.fix:
                        non_rem += 1
                    if not (mm.fix or mm.decide):
                        to_min.append(x)
            target = self.stack_end[0]

        self._undo(self.height - target)
        did_simplify = False
        if not found_abs:
            extra, did_simplify = self._minimize(to_min)
            non_rem += extra

        given = 0
        if found_abs:
            level = 0
        else:
            if self.free_cls_pages.size() * (D.CLS_PAGE - 1) < non_rem or self.free_cls_id.empty():
                return -4, level, 0, 0, 0
            given = self._save_clause(rc, non_rem)
            if given < 0:
                return given, level, 0, 0, 0
            if did_simplify:
                s.longest_simplified = max(s.longest_simplified, non_rem)
                s.simplified += 1
            level = level_before
            if not reset_all:
                self.st[given] = [uip, 1]
        return 0, level, ins0, uip, given

    # --------------------------------------------------------------- undo --
    def _undo(self, count):
        for _ in range(count):
            self.height -= 1
            L = self.stack[self.height]
            v = self._v(L)
            if self.height <= self.commit_idx:
                for c1 in self._walk_list(self._occ(L, walk=True)):
                    st = self.st[c1 - 1]
                    st[0] ^= -L
                    st[1] += 1
                self.pq_unhide(v + 1)
            m = self.meta[v]
            m.in_stack = False
            m.phase = self.POS if L > 0 else 1 - self.POS

    # ----------------------------------------------------------- minimize --
    def _minimize(self, to_min):
        s = self.stats
        extra = 0
        did = False
        for g in to_min:
            v = self._v(g)
            mm = self.lmmd[v]
            next_c = self.unit_by_cls[v]
            if mm.fix or mm.decide:
                continue
            queue: list[int] = []
            qt = 0
            num_e = cm = 0
            while True:
                s.min_iter += 1
                hit = False
                for x in self._clause(next_c - 1):
                    s.min_merge += 1
                    u = self._v(x)
                    st = self.scratch_m[u] if self.valid_m[u] else 0
                    self.valid_m[u] = True
                    bit = 1 if x > 0 else 2
                    ins = not st & bit
                    st |= bit
                    if st == 3:
                        num_e -= 1
                    elif ins:
                        num_e += 1
                        self.scratch_m[u] = st
                        cmm = self.lmmd[u]
                        cmp = cmm.min_keep > 0 or cmm.fix
                        if not hit:
                            if cmp:
                                cm += 1
                            else:
                                queue.append(u + 1)
                                if cmm.decide:
                                    hit = True
                if not hit and cm == num_e:
                    mm.min_keep = 2
                    did = True
                    break
                if hit or qt >= len(queue):
                    extra += 1
                    break
                nl = queue[qt]
                qt += 1
                next_c = self.unit_by_cls[nl - 1]
            self.valid_m = [False] * self.N
        return extra, did

    # ---------------------------------------------------------- save/alloc --
    def _save_clause(self, rc, non_rem) -> int:
        addr = self.free_cls_pages.read()
        cid = self.free_cls_id.read()
        self.last_inserted = cid
        self.cmd[cid] = (addr, non_rem)
        comp = 0
        udl: list[int] = []
        sub = 0
        kept = 0
        for x in rc:
            v = self._v(x)
            m, mm = self.meta[v], self.lmmd[v]
            if not mm.fix and mm.min_keep == 1:
                comp ^= x
                if m.dec_lvl not in udl and len(udl) < D.LBD_BUCKETS + 1:
                    udl.append(m.dec_lvl)
                o = self._occ(x, walk=False)
                lit_addr = o[2] + self.P - o[3] - 2
                self.ls[lit_addr] = cid + 1
                o[1] += 1
                o[3] -= 1
                # clause store + location handler SAVE
                self.cs[addr + sub] = x
                self.cls_to_lit[addr + sub] = lit_addr
                self.lit_to_cls[lit_addr] = addr + sub
                kept += 1
                sub += 1
                if sub == D.CLS_PAGE - 1:
                    if kept != non_rem:
                        nxt = self.free_cls_pages.read()
                        self.cs[addr + sub] = nxt
                        addr = nxt
                    else:
                        self.cs[addr + sub] = 0
                    sub = 0
                if o[3] == 0:
                    if not self._alloc_page(o):
                        return -5
            mm.min_keep = 0
        assert kept == non_rem, (kept, non_rem)
        b = len(udl) - 2
        if b < 0 or b > D.LBD_BUCKETS - 1:
            b = D.LBD_BUCKETS - 1
        self.buckets[b].append(cid)
        self.stats.lbd[b] += 1
        self.used_total += 1
        self.st[cid] = [comp, non_rem]
        return cid

    def _alloc_page(self, o) -> bool:
        if self.free_lit_pages.empty():
            return False
        new = self.free_lit_pages.read()
        old = o[2]
        self.ls[old + self.P - 1] = new
        self.ls[new + self.P - 2] = old
        o[2] = new
        o[3] = self.P - 2
        return True

    # -------------------------------------------------------------- prune --
    def _prune(self):
        remove_total = (self.used_total * self.PRUNE) >> 16
        self.used_total -= remove_total
        ids: list[int] = []
        b = D.LBD_BUCKETS - 1
        while len(ids) < remove_total and b >= 0:
            q = self.buckets[b]
            if not q or q[0] == self.last_inserted:
                b -= 1
                continue
            ids.append(q.pop(0))
        for r in ids:
            self._delete_clause(r)
        self.stats.deleted += len(ids)

    def _delete_clause(self, r):
        P = self.P
        self.free_cls_id.write(r)
        start, num = self.cmd[r]
        addr, sub = start, 0
        for k in range(num):
            if sub == 0:
                self.free_cls_pages.write(addr)
            x = self.cs[addr]
            lit_addr = self.cls_to_lit[addr]
            o = self._occ(x, walk=False)
            latest, free = o[2], o[3]
            off = P - free - 3
            if free == P - 2:
                self.free_lit_pages.write(latest)
                prev = self.ls[latest + P - 2]
                o[2], o[3] = prev, 1
                latest, off = prev, P - 3
            else:
                o[3] = free + 1
            swap = latest + off
            moved = self.ls[swap]
            self.ls[lit_addr] = moved
            self.ls[swap] = 0
            o[1] -= 1
            if moved - 1 != r:
                ca = self.lit_to_cls[swap]
                self.lit_to_cls[lit_addr] = ca
                self.cls_to_lit[ca] = lit_addr
            addr += 1
            sub += 1
            if sub == D.CLS_PAGE - 1 and k != num - 1:
                addr = self.cs[addr]
                sub = 0

    def model(self) -> dict[int, bool]:
        return H.model_from_stack(self.N, self.stack[: self.height])


def solve(img: H.Images, max_iterations: int | None = None):
    g = Golden(img)
    res = g.run(max_iterations)
    return res, g
