"""CDCL SAT accelerator for the Lattice ECP5-85F, in Amaranth HDL.

A reimplementation of the openhw-2025 U55C HLS SAT solver
(openhw-2025-SAT-FPGA/hls/src) as one Wishbone B4 slave for the Manhattan
Reasoning cloud-FPGA SoC.  The seven HLS kernels become sections of a single
sequencer sharing on-chip block RAM:

  solver.cpp / decide.cpp / discover.cpp / color.cpp   -> MAIN, FT_*, BCP_*
  learn.cpp (resolution, findNextCls, writeClause)      -> LN_*, MG_*, FN_*, SV_*
  minimize.cpp                                          -> MN_*, MM_*
  backtrack.cpp (undoStates/updateStatesBackward)       -> UD_*
  manage.cpp (allocatePage, deleteTransposedClauses)    -> SV_AL*, DX_*
  clause_store_handler.cpp (+ LBD buckets, mmuStream)   -> SV_*, PR_*, DL_*
  location_handler.cpp                                  -> c2l / l2c memories
  pq_handler.cpp + priority_queue_functions.cpp         -> H*, SL*, UH*, BM*, *_SH*
  restart.cpp (Luby)                                    -> luby_* registers
  timer.cpp                                             -> per-phase cycle counters
  message.cpp                                           -> status registers

The exact operation order is specified by satlat/golden.py; simulation is
checked against it counter for counter.

Host protocol (32-bit word registers; byte address = 4 * word):
  load config registers and memory images through MEM_SEL/MEM_PTR/MEM_DATA,
  write CTRL.start, poll CTRL until done, read RESULT and the answer stack.
  Words 256..511 alias MEM_DATA, so a normal incrementing burst read or write
  of up to 256 words streams through the memory port.
"""

from amaranth.hdl import (Array, C, Cat, Elaboratable, Module, Mux, Signal,
                          Value, signed, unsigned)
from amaranth.lib import data
from amaranth.lib.memory import Memory

# --------------------------------------------------------------------------
# Capacity parameters.  Everything the hardware stores is sized from these.
# (HLS originals in brackets: the U55C build used URAM/HBM we don't have.)
# --------------------------------------------------------------------------
N_MAX = 2048          # variables                       [_FPGA_MAX_LITERALS 32768]
C_MAX = 4096          # clauses, original + learned     [_FPGA_MAX_CLAUSES 131072]
LE_MAX = 16384        # literal-store words (occurrence lists)  [1M]
CE_MAX = 16384        # clause-store words (clause bodies)      [1M]
MAX_LEARN = 1024      # longest learned clause before resolution  [_FPGA_MAX_LEARN_ELE]
LBD_BUCKETS = 10      # [_FPGA_MAX_LBD_BUCKETS]
CLS_PAGE = 4          # clause-store page: 3 literals + next pointer
SCAN_BATCH = 1023     # pqHandler removeLiterals stream depth

# Derived widths.
VAR_W = (N_MAX - 1).bit_length()            # variable index (0-based)
LIT_W = VAR_W + 2                           # signed literal, +/-N_MAX
LVL_W = VAR_W + 1                           # decision level / trail height
CLS_W = (C_MAX - 1).bit_length()            # clause index (0-based)
CID_W = C_MAX.bit_length()                  # clause id + 1 (0 = none)
LA_W = (LE_MAX - 1).bit_length()            # literal-store address
CA_W = (CE_MAX - 1).bit_length()            # clause-store address
LRN_W = MAX_LEARN.bit_length() + 1          # learned-clause position
NUM_W = 11                                  # clause length (cmd / shortest)
REM_W = 12                                  # clause-state remaining counter
FREE_W = 6                                  # free slots in a literal page (P <= 64)
LS_W = max(CID_W, LA_W)                     # literal-store word
CS_W = max(LIT_W, CA_W)                     # clause-store word
CNT_W = 16                                  # learn/minimize counters

# VSIDS score: a positive-only float, exp:8 (bias 127) | frac:17.
# Ordered by plain unsigned compare, like the HLS comp_fp64 trick.
FRAC_W = 17
EXP_W = 8
SCORE_W = EXP_W + FRAC_W
EXP_BIAS = 127
RESCALE_EXP = 100     # rescale by 2^-100 once a score reaches 2^100  [1e100]
SCORE_ONE = EXP_BIAS << FRAC_W

MAGIC = 0x5A7ACCE1
VERSION = 1

# Wishbone register map (word addresses).
R_CTRL, R_RESULT = 0, 1
R_NVARS, R_NCLS, R_LITELEMS, R_CLSELEMS, R_FIXED = 2, 3, 4, 5, 6
R_POSPHASE, R_LITPAGE, R_RESETMULT, R_PRUNE, R_INVDECAY = 7, 8, 9, 10, 11
R_MEMSEL, R_MEMPTR, R_MEMDATA = 12, 13, 14
R_MEMWIN = 256       # words 256..511 all alias MEM_DATA, so incrementing bursts stream it
R_CAPS = 16           # 16.. N_MAX, C_MAX, LE_MAX, CE_MAX, MAX_LEARN, FRAC_W, MAGIC, VERSION
R_STATS = 32          # see STAT_NAMES
R_CYCLES = 64         # 64 + 2k (lo), 65 + 2k (hi): HLS cycleCounter[k]
STAT_NAMES = [
    "total", "decide", "retry", "backtrack", "reset", "height",
    "learn_iter", "learn_merge", "min_iter", "min_merge", "simplified",
    "longest", "longest_simplified", "check_cnt", "deleted", "level",
    "fixed", "state", "cycles_lo", "cycles_hi",
]
N_PHASES = 9          # COPY, PQ-FIND, BRANCH, LEARN, LEARN_MIN, SAVE, RESIZE, BACKTRACK, DELETE
PH_INIT, PH_FIND, PH_BCP, PH_LEARN, PH_MIN, PH_SAVE, PH_DELETE = 0, 1, 2, 3, 4, 5, 8

# MEM_SEL targets for the host load / readback port.
M_LS, M_CS, M_CMD, M_CST, M_OCC, M_STK = 0, 1, 2, 3, 4, 5

# Memory record layouts.
L_CMD = data.StructLayout({"start": unsigned(CA_W), "num": unsigned(NUM_W)})
L_CST = data.StructLayout({"comp": signed(LIT_W), "rem": unsigned(REM_W)})
L_OCC = data.StructLayout({
    "start": unsigned(LA_W), "latest": unsigned(LA_W),
    "num": unsigned(LA_W + 1), "free": unsigned(FREE_W)})
L_META = data.StructLayout({
    "ins": unsigned(LVL_W), "dec": unsigned(LVL_W), "stk": unsigned(1),
    "phase": unsigned(1), "ubl": signed(LIT_W), "short": unsigned(NUM_W)})
L_LMMD = data.StructLayout({"keep": unsigned(2), "decide": unsigned(1), "fix": unsigned(1)})
L_SCR = data.StructLayout({"st": unsigned(2), "pos": unsigned(LRN_W)})
L_HEAP = data.StructLayout({"s": unsigned(SCORE_W), "var": unsigned(VAR_W)})


def fit(x, w):
    """Truncate or (sign-/zero-) extend a value to exactly w bits."""
    x = Value.cast(x)
    xw = len(x)
    if xw >= w:
        return x[:w]
    if x.shape().signed:
        return Cat(x, x[-1].replicate(w - xw))
    return Cat(x, C(0, w - xw))


def pack(layout, **fields):
    parts = []
    for name, fld in layout:
        parts.append(fit(fields[name], fld.shape.width))
    return Cat(*parts)


def var_of(x):
    """|lit| - 1 as a VAR_W-bit index."""
    x = Value.cast(x)
    return (Mux(x < 0, -x, x) - 1)[:VAR_W]


def fp_add(a, b):
    """Positive float add (truncating), matching golden.fp_add."""
    big = Mux(a < b, b, a)
    small = Mux(a < b, a, b)
    ea = big[FRAC_W:]
    eb = small[FRAC_W:]
    ma = Cat(big[:FRAC_W], C(1, 1))
    mb = Cat(small[:FRAC_W], C(1, 1))
    d = (ea - eb)[:EXP_W]
    s = ma + (mb >> d)
    hi = s[FRAC_W + 1]
    frac = Mux(hi, s[1:FRAC_W + 1], s[:FRAC_W])
    exp = (ea + hi)[:EXP_W]
    res = Cat(frac, exp)
    return Mux(a == 0, b, Mux(b == 0, a, res))


def fp_mul(a, b):
    ea = a[FRAC_W:]
    eb = b[FRAC_W:]
    ma = Cat(a[:FRAC_W], C(1, 1))
    mb = Cat(b[:FRAC_W], C(1, 1))
    p = ma * mb
    hi = p[2 * FRAC_W + 1]
    frac = Mux(hi, p[FRAC_W + 1:2 * FRAC_W + 1], p[FRAC_W:2 * FRAC_W])
    exp = (ea + eb - EXP_BIAS + hi)[:EXP_W]
    return Mux((a == 0) | (b == 0), 0, Cat(frac, exp))


def fp_rescale(a):
    return Mux(a[FRAC_W:] <= RESCALE_EXP, 0, (a - (RESCALE_EXP << FRAC_W))[:SCORE_W])


class _RAM:
    """One block RAM: a single read port (1-cycle, write-transparent) and one write port."""

    def __init__(self, m, name, shape, depth):
        self.m = m
        self.depth = depth
        self.mem = Memory(shape=shape, depth=depth, init=[])
        m.submodules[name] = self.mem
        self.w = self.mem.write_port()
        self.r = self.mem.read_port(transparent_for=(self.w,))
        self.rdata = self.r.data

    def read(self, addr):
        self.m.d.comb += self.r.addr.eq(addr)

    def write(self, addr, value):
        self.m.d.comb += [self.w.addr.eq(addr), self.w.data.eq(value), self.w.en.eq(1)]


class _FreeList:
    """mmuStream: bump-allocate from `start` (step `inc`) to `limit`, then a ring."""

    def __init__(self, m, name, addr_w, ring_depth, limit, inc):
        self.m = m
        self.ring = _RAM(m, name, unsigned(addr_w), ring_depth)
        iw = (ring_depth - 1).bit_length()
        self.nxt = Signal(addr_w + 2, name=f"{name}_nxt")
        self.head = Signal(iw, name=f"{name}_head")
        self.tail = Signal(iw, name=f"{name}_tail")
        self.cnt = Signal(iw + 1, name=f"{name}_cnt")
        self.inc = inc
        self.limit = limit
        self.val = Signal(addr_w, name=f"{name}_val")
        self.bump_ok = Signal(name=f"{name}_bump_ok")
        m.d.comb += self.bump_ok.eq(self.nxt + inc <= limit)
        self.empty = ~self.bump_ok & (self.cnt == 0)

    def reset(self, start):
        return [self.nxt.eq(start), self.head.eq(0), self.tail.eq(0), self.cnt.eq(0)]

    def push(self, v):
        self.ring.write(self.tail, v)
        self.m.d.sync += [self.tail.eq(self.tail + 1), self.cnt.eq(self.cnt + 1)]

    def pop_states(self, pfx, then):
        """States {pfx}_P0/{pfx}_P1: self.val <- read(); continue at `then`."""
        m = self.m
        with m.State(f"{pfx}_P0"):
            with m.If(self.bump_ok):
                m.d.sync += [self.val.eq(self.nxt), self.nxt.eq(self.nxt + self.inc)]
                m.next = then
            with m.Else():
                self.ring.read(self.head)
                m.d.sync += [self.head.eq(self.head + 1), self.cnt.eq(self.cnt - 1)]
                m.next = f"{pfx}_P1"
        with m.State(f"{pfx}_P1"):
            m.d.sync += self.val.eq(self.ring.rdata)
            m.next = then


class SATAccel(Elaboratable):
    """CDCL SAT solver behind the Cloud-FPGA Wishbone B4 slave contract."""

    def __init__(self):
        self.wb_cyc = Signal()
        self.wb_stb = Signal()
        self.wb_we = Signal()
        self.wb_adr = Signal(9)
        self.wb_dat_w = Signal(32)
        self.wb_sel = Signal(4)
        self.wb_dat_r = Signal(32)
        self.wb_ack = Signal()

    def elaborate(self, platform):  # noqa: C901 -- one sequencer, by design
        m = Module()


        # ------------------------------------------------------- memories --
        ls = _RAM(m, "lit_store", unsigned(LS_W), LE_MAX)       # mLitStore
        cs = _RAM(m, "cls_store", unsigned(CS_W), CE_MAX)       # mClsStore
        cmd = _RAM(m, "cmd", L_CMD, C_MAX)                       # mCmd
        cst = _RAM(m, "cls_states", L_CST, C_MAX)                # mClsStates
        occ = _RAM(m, "occ", L_OCC, 2 * N_MAX)                   # lmd list half
        meta = _RAM(m, "meta", L_META, N_MAX)                    # lmd var half
        lmmd = _RAM(m, "lmmd", L_LMMD, N_MAX)
        ubc = _RAM(m, "unit_by_cls", unsigned(CID_W), N_MAX)
        stk = _RAM(m, "answer_stack", signed(LIT_W), N_MAX)
        send = _RAM(m, "stack_end", unsigned(LVL_W), 2 * N_MAX)  # decisionLevelStackEnd
        c2l = _RAM(m, "cls_to_lit", unsigned(LA_W), CE_MAX)     # location_handler
        l2c = _RAM(m, "lit_to_cls", unsigned(CA_W), LE_MAX)
        scr = _RAM(m, "merge_scratch", L_SCR, N_MAX)
        val = _RAM(m, "valid_learn", unsigned(32), N_MAX // 32)
        scm = _RAM(m, "min_scratch", unsigned(2), N_MAX)
        vam = _RAM(m, "valid_min", unsigned(32), N_MAX // 32)
        rc = _RAM(m, "resolution", signed(LIT_W), 2 * MAX_LEARN)
        tomin = _RAM(m, "to_minimize", signed(LIT_W), MAX_LEARN)  # also the GET_UNDECIDED scan list
        mq = _RAM(m, "min_queue", unsigned(VAR_W), MAX_LEARN)
        heap = _RAM(m, "pq_heap", L_HEAP, N_MAX)                 # mPriorityQueue
        pos = _RAM(m, "pq_pos", unsigned(VAR_W), N_MAX)          # mPositioning
        bnext = _RAM(m, "bucket_next", unsigned(CLS_W), C_MAX)   # usedClsIDBuckets

        # ------------------------------------------------- configuration --
        n_vars = Signal(LVL_W)
        n_cls = Signal(CID_W)
        lit_elems = Signal(LA_W + 1)
        cls_elems = Signal(CA_W + 1)
        fixed_init = Signal(LVL_W)
        pos_phase = Signal()
        P = Signal(7, init=8)
        reset_mult = Signal(32, init=100)
        prune_q16 = Signal(17, init=6553)
        inv_decay = Signal(SCORE_W, init=0x7F0D79)

        flp = _FreeList(m, "free_lit_pages", LA_W, LE_MAX // 4, LE_MAX, P)
        fcp = _FreeList(m, "free_cls_pages", CA_W, CE_MAX // 4, CE_MAX, CLS_PAGE)
        fid = _FreeList(m, "free_cls_id", CLS_W, C_MAX, C_MAX, 1)

        # ------------------------------------------------- host interface --
        done = Signal()
        result = Signal(signed(32))
        mem_sel = Signal(4)
        mem_ptr = Signal(18)
        occ_stage = Signal(32)
        rd_odd = Signal()           # OCC readback: which half of the entry
        ack = self.wb_ack
        stb = self.wb_cyc & self.wb_stb & ~ack
        wb_w = stb & self.wb_we
        wb_r = stb & ~self.wb_we
        adr = self.wb_adr
        dw = self.wb_dat_w

        with m.If(stb):
            m.d.sync += ack.eq(1)
        with m.Else():
            m.d.sync += ack.eq(0)

        # ------------------------------------------------- solver state ---
        height = Signal(LVL_W)
        fixed = Signal(LVL_W)
        level = Signal(LVL_W)
        do_bt = Signal()
        use_flipped = Signal()
        flipped = Signal(signed(LIT_W))
        commit = Signal(signed(LVL_W + 1))
        qh = Signal(LVL_W)
        conflict = Signal()
        nunsat = Signal(2)
        unsat0 = Signal(CID_W)
        unsat1 = Signal(CID_W)
        bL = Signal(signed(LIT_W))          # literal being propagated / undone
        top_var = Signal(VAR_W)
        u_lit = Signal(signed(LIT_W))
        u_m = Signal(L_META)
        u_len = Signal(NUM_W)

        remaining = Signal(LVL_W)
        mult = Signal(SCORE_W)

        limit = Signal(32)
        limit_count = Signal(32)
        luby_u = Signal(32)
        luby_vlog = Signal(5)
        reset_all = Signal()

        # learn
        next_c = Signal(CID_W)
        best_len = Signal(32)
        num_el = Signal(CNT_W)
        stream = Signal(CNT_W)
        highest = Signal(LVL_W)
        fixed_cnt = Signal(signed(CNT_W))
        trail_end = Signal(signed(LVL_W + 1))
        set_once = Signal()
        is_uip = Signal()
        found_abs = Signal()
        rh = Signal(2)
        res_pos = Signal(LRN_W)
        poss = Signal(signed(LIT_W))
        once = Signal()
        fn_i = Signal(signed(LVL_W + 1))
        fn_save = Signal(signed(LVL_W + 1))
        fn_v = Signal(VAR_W)
        vc_i = Signal(VAR_W)

        level_before = Signal(signed(LVL_W + 1))
        uip = Signal(signed(LIT_W))
        ins0 = Signal(signed(LIT_W))
        non_rem = Signal(CNT_W)
        tomin_n = Signal(LRN_W)
        bl_i = Signal(CNT_W)
        bl_x = Signal(signed(LIT_W))
        mode_a = Signal()
        bt_count = Signal(LVL_W)
        ud_v = Signal(VAR_W)

        # minimize
        mn_j = Signal(LRN_W)
        g_v = Signal(VAR_W)
        g_mm = Signal(L_LMMD)
        mq_h = Signal(LRN_W)
        mq_t = Signal(LRN_W)
        num_e = Signal(signed(CNT_W))
        cmk = Signal(CNT_W)
        hit = Signal()
        did = Signal()

        # save
        sv_addr = Signal(CA_W)
        sv_id = Signal(CLS_W)
        sv_i = Signal(CNT_W)
        sv_x = Signal(signed(LIT_W))
        sv_v = Signal(VAR_W)
        sv_entry = Signal(VAR_W + 1)
        comp = Signal(signed(LIT_W))
        udl = [Signal(LVL_W, name=f"udl{k}") for k in range(LBD_BUCKETS + 1)]
        udl_n = Signal(4)
        sub = Signal(2)
        kept = Signal(CNT_W)
        o_start = Signal(LA_W)
        o_latest = Signal(LA_W)
        o_num = Signal(LA_W + 1)
        o_free = Signal(FREE_W)

        # LBD buckets (clause_store_handler tracker[])
        bhead = Array(Signal(CLS_W, name=f"bhead{k}") for k in range(LBD_BUCKETS))
        btail = Array(Signal(CLS_W, name=f"btail{k}") for k in range(LBD_BUCKETS))
        bcnt = Array(Signal(CLS_W + 1, name=f"bcnt{k}") for k in range(LBD_BUCKETS))
        used_total = Signal(CLS_W + 1)
        last_ins = Signal(CLS_W + 1)
        remove_total = Signal(CLS_W + 1)
        removed = Signal(CLS_W + 1)
        pb = Signal(signed(5))
        d_r = Signal(CLS_W)
        dx_entry = Signal(VAR_W + 1)
        dx_la = Signal(LA_W)
        dx_latest = Signal(LA_W)
        dx_free = Signal(FREE_W)
        dx_swap = Signal(LA_W)
        dx_moved = Signal(LS_W)
        dx_ca = Signal(CA_W)

        # pq scratch
        ft_inc = Signal(LVL_W)
        ft_cnt = Signal(LRN_W)
        ft_found = Signal()
        hide_i = Signal(LRN_W)
        hv = Signal(VAR_W)
        pq_p = Signal(VAR_W)
        pq_r = Signal(LVL_W)
        pq_swap = Signal(L_HEAP)
        pq_last = Signal(L_HEAP)
        sl_x = Signal(L_HEAP)
        sl_p = Signal(LVL_W + 1)
        sl_rem = Signal(LVL_W)
        sl_l = Signal(LVL_W + 2)
        sl_left = Signal(L_HEAP)
        sl_save = Signal(LVL_W + 1)
        sl_use_var = Signal(VAR_W)
        sl_done = Signal()
        sh_x = Signal(L_HEAP)
        sh_p = Signal(VAR_W)
        sh_save = Signal(VAR_W)
        bv = Signal(VAR_W)
        bm_s = Signal(SCORE_W)
        rs_i = Signal(LVL_W)
        init_i = Signal(LVL_W + 1)

        # clause walk (clause store) / occurrence walk (literal store)
        cw_c = Signal(CLS_W)
        cw_addr = Signal(CA_W)
        cw_num = Signal(NUM_W)
        cw_k = Signal(NUM_W)
        cw_sub = Signal(2)
        cw_lit = Signal(signed(LIT_W))
        cw_cur = Signal(CA_W)
        cw_cursub = Signal(2)
        ow_e = Signal(VAR_W + 1)
        ow_addr = Signal(LA_W)
        ow_num = Signal(LA_W + 1)
        ow_k = Signal(LA_W + 1)
        ow_i = Signal(FREE_W + 1)
        ow_c1 = Signal(CID_W)

        # stats (HLS miscCounters / learnedStats / cycleCounter)
        st = {n: Signal(32, name=f"stat_{n}") for n in STAT_NAMES}
        phase = Signal(4)
        cyc = [Signal(64, name=f"cyc{k}") for k in range(N_PHASES)]
        cycles = Signal(64)

        def inc(name, by=1):
            m.d.sync += st[name].eq(st[name] + by)

        # ------------------------------------------------- walk helpers ---
        def clause_walk(pfx, on_item, on_end):
            """Iterate the literals of clause cw_c: item state sees cw_lit/cw_cur."""
            with m.State(f"{pfx}_CW0"):
                cmd.read(cw_c)
                m.next = f"{pfx}_CW1"
            with m.State(f"{pfx}_CW1"):
                m.d.sync += [cw_addr.eq(cmd.rdata.start), cw_num.eq(cmd.rdata.num),
                         cw_k.eq(0), cw_sub.eq(0)]
                m.next = f"{pfx}_CWN"
            with m.State(f"{pfx}_CWN"):
                with m.If(cw_k == cw_num):
                    m.next = on_end
                with m.Else():
                    cs.read(cw_addr)
                    m.next = f"{pfx}_CWG"
            with m.State(f"{pfx}_CWG"):
                m.d.sync += [cw_lit.eq(cs.rdata[:LIT_W].as_signed()), cw_cur.eq(cw_addr),
                         cw_cursub.eq(cw_sub), cw_addr.eq(cw_addr + 1),
                         cw_sub.eq(cw_sub + 1), cw_k.eq(cw_k + 1)]
                with m.If((cw_sub == CLS_PAGE - 2) & (cw_k + 1 != cw_num)):
                    cs.read(cw_addr + 1)
                    m.next = f"{pfx}_CWJ"
                with m.Else():
                    m.next = on_item
            with m.State(f"{pfx}_CWJ"):
                m.d.sync += [cw_addr.eq(cs.rdata), cw_sub.eq(0)]
                m.next = on_item

        def occ_walk(pfx, on_item, on_end):
            """Iterate the occurrence list occ[ow_e]: item state sees ow_c1."""
            with m.State(f"{pfx}_OW0"):
                occ.read(ow_e)
                m.next = f"{pfx}_OW1"
            with m.State(f"{pfx}_OW1"):
                m.d.sync += [ow_addr.eq(occ.rdata.start), ow_num.eq(occ.rdata.num),
                         ow_k.eq(0), ow_i.eq(0)]
                m.next = f"{pfx}_OWN"
            with m.State(f"{pfx}_OWN"):
                with m.If(ow_k == ow_num):
                    m.next = on_end
                with m.Else():
                    ls.read(ow_addr)
                    m.next = f"{pfx}_OWG"
            with m.State(f"{pfx}_OWG"):
                m.d.sync += [ow_c1.eq(ls.rdata), ow_addr.eq(ow_addr + 1),
                         ow_i.eq(ow_i + 1), ow_k.eq(ow_k + 1)]
                with m.If(ow_i + 1 == P - 2):
                    ls.read(ow_addr + 2)
                    m.next = f"{pfx}_OWJ"
                with m.Else():
                    m.next = on_item
            with m.State(f"{pfx}_OWJ"):
                m.d.sync += [ow_addr.eq(ls.rdata), ow_i.eq(0)]
                m.next = on_item

        def swap_higher(pfx, ret):
            """swapHigher(sh_x, sh_p): entry {pfx}_SH1 with sh_save == sh_p."""
            with m.State(f"{pfx}_SH1"):
                with m.If(sh_p == 0):
                    m.next = f"{pfx}_SHE"
                with m.Else():
                    heap.read((sh_p - 1) >> 1)
                    m.next = f"{pfx}_SH2"
            with m.State(f"{pfx}_SH2"):
                np_ = ((sh_p - 1) >> 1)[:VAR_W]
                with m.If(heap.rdata.s < sh_x.s):
                    heap.write(sh_p, heap.rdata)
                    pos.write(heap.rdata.var, sh_p)
                    m.d.sync += [sh_save.eq(np_), sh_p.eq(np_)]
                    m.next = f"{pfx}_SH1"
                with m.Else():
                    m.next = f"{pfx}_SHE"
            with m.State(f"{pfx}_SHE"):
                pos.write(sh_x.var, sh_save)
                heap.write(sh_save, sh_x)
                m.next = ret

        # -------------------------------------------------------- the FSM --
        with m.FSM(name="seq") as fsm:
            busy = ~fsm.ongoing("IDLE")

            # ================================================ IDLE / host ==
            with m.State("IDLE"):
                is_mem = (adr == R_MEMDATA) | adr[8]
                with m.If(wb_w & is_mem):
                    with m.Switch(mem_sel):
                        with m.Case(M_LS):
                            ls.write(mem_ptr, dw)
                        with m.Case(M_CS):
                            cs.write(mem_ptr, dw)
                        with m.Case(M_CMD):
                            cmd.write(mem_ptr, pack(L_CMD, start=dw[:16], num=dw[16:]))
                        with m.Case(M_CST):
                            cst.write(mem_ptr, pack(L_CST, comp=dw[:16].as_signed(), rem=dw[16:]))
                        with m.Case(M_OCC):
                            with m.If(mem_ptr[0] == 0):
                                m.d.sync += occ_stage.eq(dw)
                            with m.Else():
                                occ.write(mem_ptr >> 1, pack(
                                    L_OCC, start=occ_stage[:16], latest=occ_stage[16:],
                                    num=dw[:16], free=dw[16:24]))
                        with m.Case(M_STK):
                            stk.write(mem_ptr, dw.as_signed())
                with m.If(wb_r & is_mem):
                    with m.Switch(mem_sel):
                        with m.Case(M_LS):
                            ls.read(mem_ptr)
                        with m.Case(M_CS):
                            cs.read(mem_ptr)
                        with m.Case(M_CMD):
                            cmd.read(mem_ptr)
                        with m.Case(M_CST):
                            cst.read(mem_ptr)
                        with m.Case(M_OCC):
                            occ.read(mem_ptr >> 1)
                        with m.Case(M_STK):
                            stk.read(mem_ptr)
                with m.If(wb_w):
                    with m.Switch(adr):
                        with m.Case(R_NVARS):
                            m.d.sync += n_vars.eq(dw)
                        with m.Case(R_NCLS):
                            m.d.sync += n_cls.eq(dw)
                        with m.Case(R_LITELEMS):
                            m.d.sync += lit_elems.eq(dw)
                        with m.Case(R_CLSELEMS):
                            m.d.sync += cls_elems.eq(dw)
                        with m.Case(R_FIXED):
                            m.d.sync += fixed_init.eq(dw)
                        with m.Case(R_POSPHASE):
                            m.d.sync += pos_phase.eq(dw[0])
                        with m.Case(R_LITPAGE):
                            m.d.sync += P.eq(dw)
                        with m.Case(R_RESETMULT):
                            m.d.sync += reset_mult.eq(dw)
                        with m.Case(R_PRUNE):
                            m.d.sync += prune_q16.eq(dw)
                        with m.Case(R_INVDECAY):
                            m.d.sync += inv_decay.eq(dw)
                        with m.Case(R_MEMSEL):
                            m.d.sync += mem_sel.eq(dw)
                        with m.Case(R_MEMPTR):
                            m.d.sync += mem_ptr.eq(dw)
                        with m.Case(R_CTRL):
                            with m.If(dw[0]):
                                m.d.sync += [done.eq(0), result.eq(0), init_i.eq(0),
                                             phase.eq(PH_INIT), cycles.eq(0),
                                             *[c.eq(0) for c in cyc]]
                                m.next = "INIT"
                with m.If((wb_w | wb_r) & is_mem):
                    m.d.sync += [mem_ptr.eq(mem_ptr + 1), rd_odd.eq(mem_ptr[0])]

            # ======================================== INIT (copy_in + pq) ==
            with m.State("INIT"):
                with m.If(init_i == N_MAX):
                    m.d.sync += [
                        remaining.eq(n_vars), mult.eq(SCORE_ONE),
                        height.eq(0), fixed.eq(fixed_init),
                        level.eq(Mux(fixed_init == 0, 1, 0)),
                        do_bt.eq(0), use_flipped.eq(0), commit.eq(-1),
                        limit.eq(reset_mult), limit_count.eq(0),
                        luby_u.eq(2), luby_vlog.eq(0),
                        used_total.eq(0), last_ins.eq(C_MAX),
                        *flp.reset(lit_elems), *fcp.reset(cls_elems), *fid.reset(n_cls),
                        *[s.eq(0) for s in st.values()],
                        *[bcnt[k].eq(0) for k in range(LBD_BUCKETS)],
                    ]
                    m.next = "MAIN"
                with m.Else():
                    heap.write(init_i, pack(L_HEAP, s=0, var=init_i))
                    pos.write(init_i, init_i)
                    send.write(init_i, 0)       # stack_end[0] is read before any level-0 BCP
                    lmmd.write(init_i, 0)
                    meta.write(init_i, pack(L_META, ins=0, dec=0, stk=0, phase=~pos_phase,
                                            ubl=0, short=0))
                    with m.If(init_i < N_MAX // 32):
                        val.write(init_i, 0)
                        vam.write(init_i, 0)
                    m.d.sync += init_i.eq(init_i + 1)

            # ===================================== MAIN (SOLVE_ITERATION) ==
            with m.State("MAIN"):
                inc("total")
                with m.If(~do_bt):
                    with m.If(use_flipped):
                        inc("retry")
                    with m.Else():
                        inc("decide")
                    with m.If(~use_flipped & (level != 0)):
                        m.d.sync += [ft_inc.eq(0), ft_cnt.eq(0), phase.eq(PH_FIND)]
                        m.next = "FT_RD"
                    with m.Else():
                        m.d.sync += phase.eq(PH_BCP)
                        m.next = "BCP_START"
                with m.Else():
                    inc("backtrack")
                    lc = (limit_count + 1)[:32]
                    with m.If(limit == lc):
                        lowbit = (luby_u & -luby_u)[:32]
                        m.d.sync += [limit_count.eq(0),
                                 limit.eq(reset_mult << luby_vlog),
                                 reset_all.eq(1)]
                        with m.If(lowbit == (C(1, 32) << luby_vlog)[:32]):
                            m.d.sync += [luby_u.eq(luby_u + 1), luby_vlog.eq(0)]
                        with m.Else():
                            m.d.sync += luby_vlog.eq(luby_vlog + 1)
                        inc("reset")
                    with m.Else():
                        m.d.sync += [limit_count.eq(lc), reset_all.eq(0)]
                    m.d.sync += phase.eq(PH_LEARN)
                    m.next = "LN_S0"

            # ========================= FIND_TOP (pqHandler GET_UNDECIDED) ==
            with m.State("FT_RD"):
                heap.read(ft_inc)
                m.next = "FT_M"
            with m.State("FT_M"):
                meta.read(heap.rdata.var)
                tomin.write(ft_cnt, heap.rdata.var)
                m.d.sync += [top_var.eq(heap.rdata.var), ft_cnt.eq(ft_cnt + 1),
                         ft_inc.eq(ft_inc + 1)]
                m.next = "FT_C"
            with m.State("FT_C"):
                with m.If(~meta.rdata.stk):
                    m.d.sync += [ft_found.eq(1), hide_i.eq(0)]
                    m.next = "HD_0"
                with m.Elif(ft_cnt == SCAN_BATCH):
                    m.d.sync += [ft_found.eq(0), hide_i.eq(0)]
                    m.next = "HD_0"
                with m.Else():
                    m.next = "FT_RD"
            with m.State("HD_0"):
                with m.If(hide_i == ft_cnt):
                    with m.If(ft_found):
                        m.d.sync += phase.eq(PH_BCP)
                        m.next = "BCP_START"
                    with m.Else():
                        m.d.sync += [ft_inc.eq(0), ft_cnt.eq(0)]
                        m.next = "FT_RD"
                with m.Else():
                    tomin.read(hide_i)
                    m.next = "HD_1"
            with m.State("HD_1"):                   # hideElement
                pos.read(tomin.rdata[:VAR_W])
                m.d.sync += [hv.eq(tomin.rdata[:VAR_W]), hide_i.eq(hide_i + 1)]
                m.next = "H1"
            with m.State("H1"):
                m.d.sync += [pq_p.eq(pos.rdata), pq_r.eq(remaining - 1)]
                heap.read(pos.rdata)
                m.next = "H2"
            with m.State("H2"):
                m.d.sync += pq_swap.eq(heap.rdata)
                heap.read(pq_r)
                m.next = "H3"
            with m.State("H3"):
                m.d.sync += pq_last.eq(heap.rdata)
                heap.write(pq_p, heap.rdata)
                m.next = "H4"
            with m.State("H4"):
                pos.write(pq_last.var, pq_p)
                heap.write(pq_r, pq_swap)
                m.next = "H5"
            with m.State("H5"):
                pos.write(hv, pq_r)
                m.d.sync += [remaining.eq(pq_r), sl_x.eq(pq_last), sl_p.eq(pq_p),
                         sl_rem.eq(pq_r)]
                m.next = "SL0"
            # swapLower
            with m.State("SL0"):
                m.d.sync += sl_l.eq(2 * sl_p + 1)
                heap.read(2 * sl_p + 1)
                m.next = "SL1"
            with m.State("SL1"):
                m.d.sync += sl_left.eq(heap.rdata)
                heap.read(sl_l + 1)
                m.next = "SL2"
            with m.State("SL2"):
                right = heap.rdata
                lsc = Mux(sl_l < sl_rem, sl_left.s, 0)
                rsc = Mux(sl_l + 1 < sl_rem, right.s, 0)
                e1 = lsc > sl_x.s
                e2 = lsc >= rsc
                e3 = rsc > sl_x.s
                m.d.sync += sl_save.eq(sl_p)
                with m.If(e3 & ~e2):
                    heap.write(sl_p, right)
                    m.d.sync += [sl_use_var.eq(right.var), sl_p.eq(sl_l + 1), sl_done.eq(0)]
                with m.Elif(~e1 & ~e3):
                    heap.write(sl_p, sl_x)
                    m.d.sync += [sl_use_var.eq(sl_x.var), sl_done.eq(1)]
                with m.Else():
                    heap.write(sl_p, sl_left)
                    m.d.sync += [sl_use_var.eq(sl_left.var), sl_p.eq(sl_l), sl_done.eq(0)]
                m.next = "SL3"
            with m.State("SL3"):
                pos.write(sl_use_var, sl_save)
                with m.If(sl_done):
                    m.next = "SL4"
                with m.Else():
                    m.next = "SL0"
            with m.State("SL4"):
                pos.write(sl_x.var, sl_save)
                heap.write(sl_save, sl_x)
                m.next = "HD_0"

            # ============================ BCP (checkUndecided + discover) ==
            with m.State("BCP_START"):
                m.d.sync += [qh.eq(height), conflict.eq(0), nunsat.eq(0)]
                with m.If(use_flipped):
                    meta.read(var_of(flipped))
                    lmmd.read(var_of(flipped))
                    m.next = "BCP_FLIP"
                with m.Elif(level == 0):
                    m.next = "FX_RD"
                with m.Else():
                    meta.read(top_var)
                    lmmd.read(top_var)
                    m.next = "BCP_DECIDE"
            with m.State("FX_RD"):                   # WRITE_FIXED_DECISION
                with m.If(height >= fixed):
                    m.next = "BCP_Q"
                with m.Else():
                    stk.read(height)
                    m.next = "FX_M"
            with m.State("FX_M"):
                meta.read(var_of(stk.rdata))
                lmmd.read(var_of(stk.rdata))
                m.d.sync += bL.eq(stk.rdata)
                m.next = "FX_W"
            with m.State("FX_W"):
                v = var_of(bL)
                meta.write(v, pack(L_META, ins=height, dec=0, stk=1,
                                   phase=Mux(bL > 0, pos_phase, ~pos_phase),
                                   ubl=0, short=meta.rdata.short))
                lmmd.write(v, pack(L_LMMD, keep=lmmd.rdata.keep, decide=1, fix=1))
                m.d.sync += height.eq(height + 1)
                m.next = "FX_RD"
            with m.State("BCP_DECIDE"):
                g = Mux(meta.rdata.phase != pos_phase, -(top_var + 1), top_var + 1)
                meta.write(top_var, pack(L_META, ins=height, dec=level, stk=1,
                                         phase=meta.rdata.phase, ubl=0,
                                         short=meta.rdata.short))
                lmmd.write(top_var, pack(L_LMMD, keep=lmmd.rdata.keep, decide=1, fix=0))
                stk.write(height, g)
                m.d.sync += height.eq(height + 1)
                m.next = "BCP_Q"
            with m.State("BCP_FLIP"):
                v = var_of(flipped)
                meta.write(v, pack(L_META, ins=height, dec=level, stk=1,
                                   phase=Mux(flipped > 0, pos_phase, ~pos_phase),
                                   ubl=0, short=meta.rdata.short))
                lmmd.write(v, pack(L_LMMD, keep=lmmd.rdata.keep, decide=0,
                                   fix=lmmd.rdata.fix))
                ubc.write(v, sv_id + 1)
                stk.write(height, flipped)
                m.d.sync += height.eq(height + 1)
                m.next = "BCP_Q"
            with m.State("BCP_Q"):
                with m.If((qh < height) & ~conflict):
                    stk.read(qh)
                    m.next = "BQ_L"
                with m.Else():
                    m.next = "BCP_END"
            with m.State("BQ_L"):
                m.d.sync += [bL.eq(stk.rdata), qh.eq(qh + 1),
                         ow_e.eq(Cat(stk.rdata > 0, var_of(stk.rdata)))]
                inc("check_cnt")
                m.next = "BW_OW0"
            occ_walk("BW", "BI0", "BCP_Q")
            with m.State("BI0"):                     # updateStatesForward
                cst.read(ow_c1 - 1)
                m.next = "BI1"
            with m.State("BI1"):
                ncomp = (cst.rdata.comp.as_unsigned() ^ (-bL)[:LIT_W])[:LIT_W].as_signed()
                nrem = (cst.rdata.rem - 1)[:REM_W]
                cst.write(ow_c1 - 1, pack(L_CST, comp=ncomp, rem=nrem))
                with m.If((nrem == 1) & ~conflict):
                    cmd.read(ow_c1 - 1)
                    meta.read(var_of(ncomp))
                    m.d.sync += u_lit.eq(ncomp)
                    m.next = "BU1"
                with m.Elif(nrem == 0):
                    with m.If(nunsat == 0):
                        m.d.sync += [unsat0.eq(ow_c1), nunsat.eq(1)]
                    with m.Elif(nunsat == 1):
                        m.d.sync += [unsat1.eq(ow_c1), nunsat.eq(2)]
                    m.d.sync += conflict.eq(1)
                    m.next = "BW_OWN"
                with m.Else():
                    m.next = "BW_OWN"
            with m.State("BU1"):                     # discover: a unit literal
                v = var_of(u_lit)
                mr = meta.rdata
                m.d.sync += [u_m.eq(mr), u_len.eq(cmd.rdata.num)]
                with m.If(~mr.stk):
                    meta.write(v, pack(L_META, ins=height, dec=level, stk=1, phase=mr.phase,
                                       ubl=bL, short=cmd.rdata.num))
                    ubc.write(v, ow_c1)
                    stk.write(height, u_lit)
                    lmmd.read(v)
                    m.d.sync += height.eq(height + 1)
                    m.next = "BU2"
                with m.Else():
                    stk.read(mr.ins)
                    m.next = "BU3"
            with m.State("BU2"):
                v = var_of(u_lit)
                lmmd.write(v, pack(L_LMMD, keep=lmmd.rdata.keep, decide=0,
                                   fix=lmmd.rdata.fix | (level == 0)))
                with m.If(level == 0):
                    m.d.sync += fixed.eq(fixed + 1)
                m.next = "BW_OWN"
            with m.State("BU3"):
                v = var_of(u_lit)
                with m.If((stk.rdata == u_lit) & (u_m.ubl == bL) & (u_len < u_m.short)):
                    ubc.write(v, ow_c1)
                    meta.write(v, pack(L_META, ins=u_m.ins, dec=u_m.dec, stk=u_m.stk,
                                       phase=u_m.phase, ubl=u_m.ubl, short=u_len))
                m.next = "BW_OWN"
            with m.State("BCP_END"):
                send.write(level, height)
                new_level = Mux(conflict, level, level + 1)[:LVL_W]
                m.d.sync += [commit.eq(qh - 1), do_bt.eq(conflict), level.eq(new_level),
                         use_flipped.eq(0)]
                with m.If(new_level == 0):
                    m.d.sync += result.eq(0)
                    m.next = "FINISH"
                with m.Elif((height == n_vars) & ~conflict):
                    m.d.sync += result.eq(1)
                    m.next = "FINISH"
                with m.Else():
                    m.next = "MAIN"

            # ============================================ learnClause =====
            with m.State("LN_S0"):                   # GET_SHORTEST_START
                cmd.read(unsat0 - 1)
                m.next = "LN_S1"
            with m.State("LN_S1"):
                m.d.sync += [next_c.eq(unsat0), best_len.eq(cmd.rdata.num)]
                with m.If(nunsat > 1):
                    cmd.read(unsat1 - 1)
                    m.next = "LN_S2"
                with m.Else():
                    m.next = "LN_INIT"
            with m.State("LN_S2"):
                with m.If(cmd.rdata.num < best_len):
                    m.d.sync += next_c.eq(unsat1)
                m.next = "LN_INIT"
            with m.State("LN_INIT"):
                m.d.sync += [num_el.eq(0), stream.eq(0), highest.eq(0), fixed_cnt.eq(0),
                         trail_end.eq(0), set_once.eq(0), is_uip.eq(0), found_abs.eq(0)]
                m.next = "LN_ITER"
            with m.State("LN_ITER"):                 # RESOLUTION loop head
                with m.If(is_uip | found_abs):
                    m.d.sync += mult.eq(fp_mul(mult, inv_decay))   # decayEveryElement exit
                    m.d.sync += vc_i.eq(0)
                    m.next = "LN_CLR"
                with m.Else():
                    inc("learn_iter")
                    m.d.sync += [rh.eq(0), res_pos.eq(0), once.eq(highest != 0),
                             cw_c.eq(next_c - 1), poss.eq(0)]
                    with m.If(num_el > 0):
                        rc.read(num_el - 1)
                        m.next = "LN_P"
                    with m.Else():
                        m.next = "MG_CW0"
            with m.State("LN_P"):
                m.d.sync += poss.eq(rc.rdata)
                m.next = "MG_CW0"
            clause_walk("MG", "MG_A", "MG_END")
            with m.State("MG_A"):                    # merge_resolution_sort
                v = var_of(cw_lit)
                scr.read(v)
                val.read(v >> 5)
                meta.read(v)
                lmmd.read(v)
                inc("learn_merge")
                m.next = "MG_B"
            with m.State("MG_B"):
                v = var_of(cw_lit)
                vbit = val.rdata.bit_select(v[:5], 1)
                st0 = Mux(vbit, scr.rdata.st, 0)
                pos0 = Mux(vbit, scr.rdata.pos, 0)
                val.write(v >> 5, val.rdata | (C(1, 32) << v[:5])[:32])
                bit = Mux(cw_lit > 0, 1, 2)
                ins = (st0 & bit) == 0
                st1 = (st0 | bit)[:2]
                mr, lr = meta.rdata, lmmd.rdata
                with m.If(st1 == 3):
                    with m.If(pos0 != (num_el - 1)[:CNT_W]):
                        rc.write(pos0, poss)
                        m.d.sync += [res_pos.eq(pos0), rh.eq(1)]
                    with m.Else():
                        m.d.sync += rh.eq(2)
                    m.d.sync += num_el.eq(num_el - 1)
                    scr.write(v, 0)
                    with m.If(lr.fix):
                        m.d.sync += fixed_cnt.eq(fixed_cnt - 1)
                    lmmd.write(v, pack(L_LMMD, keep=0, decide=lr.decide, fix=lr.fix))
                    m.next = "MG_CWN"
                with m.Elif(ins):
                    rc.write(num_el, cw_lit)
                    m.d.sync += num_el.eq(num_el + 1)
                    with m.If(rh == 0):
                        m.d.sync += poss.eq(cw_lit)
                    scr.write(v, pack(L_SCR, st=st1, pos=num_el))
                    with m.If(mr.dec == level):
                        with m.If((highest < mr.ins) & ~once):
                            m.d.sync += highest.eq(mr.ins)
                        m.d.sync += stream.eq(stream + 1)
                    with m.If(lr.fix):
                        m.d.sync += fixed_cnt.eq(fixed_cnt + 1)
                    lmmd.write(v, pack(L_LMMD, keep=1, decide=lr.decide, fix=lr.fix))
                    with m.If(mr.dec > 0):
                        pos.read(v)
                        m.d.sync += bv.eq(v)
                        m.next = "BM1"
                    with m.Else():
                        m.next = "MG_CWN"
                with m.Else():
                    m.next = "MG_CWN"
            # pq UPDATE: bump one variable's activity
            with m.State("BM1"):
                m.d.sync += pq_p.eq(pos.rdata)
                heap.read(pos.rdata)
                m.next = "BM2"
            with m.State("BM2"):
                m.d.sync += bm_s.eq(fp_add(heap.rdata.s, mult))
                m.next = "BM3"
            with m.State("BM3"):
                heap.write(pq_p, pack(L_HEAP, s=bm_s, var=bv))
                with m.If(bm_s[FRAC_W:] >= EXP_BIAS + RESCALE_EXP):
                    m.d.sync += rs_i.eq(0)
                    m.next = "RS0"
                with m.Else():
                    m.next = "BM4"
            with m.State("RS0"):                     # the 1e100 ADJUST loop
                with m.If(rs_i == n_vars):
                    m.d.sync += [mult.eq(fp_rescale(mult)), bm_s.eq(fp_rescale(bm_s))]
                    m.next = "BM4"
                with m.Else():
                    heap.read(rs_i)
                    m.next = "RS1"
            with m.State("RS1"):
                heap.write(rs_i, pack(L_HEAP, s=fp_rescale(heap.rdata.s), var=heap.rdata.var))
                m.d.sync += rs_i.eq(rs_i + 1)
                m.next = "RS0"
            with m.State("BM4"):
                with m.If(pq_p < remaining):
                    m.d.sync += [sh_x.eq(pack(L_HEAP, s=bm_s, var=bv)), sh_p.eq(pq_p),
                             sh_save.eq(pq_p)]
                    m.next = "BH_SH1"
                with m.Else():
                    m.next = "MG_CWN"
            swap_higher("BH", "MG_CWN")
            with m.State("MG_END"):
                with m.If(rh == 1):
                    scr.read(var_of(poss))
                    m.next = "MG_E2"
                with m.Else():
                    m.next = "LN_AFTER"
            with m.State("MG_E2"):
                scr.write(var_of(poss), pack(L_SCR, st=scr.rdata.st, pos=res_pos))
                m.next = "LN_AFTER"
            with m.State("LN_AFTER"):
                te = Mux(set_once, trail_end, highest)
                m.d.sync += [trail_end.eq(te), set_once.eq(1)]
                with m.If(num_el > st["longest"]):
                    m.d.sync += st["longest"].eq(num_el)
                with m.If(num_el > MAX_LEARN):
                    m.d.sync += result.eq(-2)
                    m.next = "FINISH"
                with m.Else():
                    with m.If((num_el == 1) | ((num_el - fixed_cnt) == 1)):
                        m.d.sync += found_abs.eq(1)
                    with m.If(stream == 1):
                        m.d.sync += is_uip.eq(1)
                        m.next = "LN_ITER"
                    with m.Else():
                        m.d.sync += [fn_i.eq(te), fn_save.eq(te)]
                        m.next = "FN_RD"
            with m.State("FN_RD"):                   # findNextCls
                with m.If(fn_i < 0):
                    m.next = "FN_DONE"
                with m.Else():
                    stk.read(fn_i)
                    m.next = "FN_V"
            with m.State("FN_V"):
                v = var_of(stk.rdata)
                val.read(v >> 5)
                scr.read(v)
                m.d.sync += [fn_v.eq(v), trail_end.eq(trail_end - 1)]
                inc("learn_merge")
                m.next = "FN_C"
            with m.State("FN_C"):
                vbit = val.rdata.bit_select(fn_v[:5], 1)
                s_ = scr.rdata.st
                with m.If(vbit & ((s_ == 1) | (s_ == 2))):
                    ubc.read(fn_v)
                    m.d.sync += fn_save.eq(trail_end)
                    m.next = "FN_U"
                with m.Else():
                    m.d.sync += fn_i.eq(fn_i - 1)
                    m.next = "FN_RD"
            with m.State("FN_U"):
                m.d.sync += next_c.eq(ubc.rdata)
                m.next = "FN_DONE"
            with m.State("FN_DONE"):
                m.d.sync += [trail_end.eq(fn_save), stream.eq(stream - 1)]
                m.next = "LN_ITER"
            with m.State("LN_CLR"):                  # ZERO_SEQ
                with m.If(vc_i == N_MAX // 32):
                    m.d.sync += [bl_i.eq(0), level_before.eq(-1), uip.eq(0), ins0.eq(0),
                             non_rem.eq(0), tomin_n.eq(0),
                             mode_a.eq(~reset_all & ~found_abs)]
                    m.next = "BL_RD"
                with m.Else():
                    val.write(vc_i, 0)
                    m.d.sync += vc_i.eq(vc_i + 1)
            # GET_BT_LEVEL / FIND_ABSOLUTE / GET_MINIMIZE
            with m.State("BL_RD"):
                with m.If(bl_i == num_el):
                    send.read(Mux(mode_a, level_before, 0)[:LVL_W + 1])
                    m.next = "BL_T"
                with m.Else():
                    rc.read(bl_i)
                    m.next = "BL_V"
            with m.State("BL_V"):
                meta.read(var_of(rc.rdata))
                lmmd.read(var_of(rc.rdata))
                m.d.sync += bl_x.eq(rc.rdata)
                m.next = "BL_C"
            with m.State("BL_C"):
                mr, lr = meta.rdata, lmmd.rdata
                with m.If(found_abs):
                    with m.If(~lr.fix):
                        m.d.sync += ins0.eq(bl_x)
                with m.Else():
                    with m.If(mode_a):
                        with m.If(~lr.fix & (mr.dec != 0) & (level_before < mr.dec)
                                  & (mr.dec != level)):
                            m.d.sync += level_before.eq(mr.dec)
                        with m.If(mr.dec == level):
                            m.d.sync += uip.eq(bl_x)
                    with m.If(lr.decide & ~lr.fix):
                        m.d.sync += non_rem.eq(non_rem + 1)
                    with m.If(~(lr.fix | lr.decide)):
                        tomin.write(tomin_n, bl_x)
                        m.d.sync += tomin_n.eq(tomin_n + 1)
                m.d.sync += bl_i.eq(bl_i + 1)
                m.next = "BL_RD"
            with m.State("BL_T"):
                m.d.sync += [bt_count.eq(height - send.rdata), phase.eq(PH_MIN)]
                m.next = "UD_TOP"

            # ============================ undoStates (backtrack) =========
            with m.State("UD_TOP"):
                with m.If(bt_count == 0):
                    with m.If(found_abs):
                        m.d.sync += phase.eq(PH_SAVE)
                        m.next = "SV_0"
                    with m.Else():
                        m.d.sync += mn_j.eq(0)
                        m.next = "MN_RD"
                with m.Else():
                    stk.read(height - 1)
                    m.d.sync += [height.eq(height - 1), bt_count.eq(bt_count - 1)]
                    m.next = "UD_L"
            with m.State("UD_L"):
                m.d.sync += [bL.eq(stk.rdata), ud_v.eq(var_of(stk.rdata)),
                         ow_e.eq(Cat(stk.rdata > 0, var_of(stk.rdata)))]
                with m.If(height <= commit):
                    m.next = "UD_OW0"
                with m.Else():
                    m.next = "UD_META"
            occ_walk("UD", "UI0", "UH0")
            with m.State("UI0"):                     # updateStatesBackward
                cst.read(ow_c1 - 1)
                m.next = "UI1"
            with m.State("UI1"):
                cst.write(ow_c1 - 1, pack(
                    L_CST, comp=(cst.rdata.comp.as_unsigned() ^ (-bL)[:LIT_W])[:LIT_W].as_signed(),
                    rem=cst.rdata.rem + 1))
                m.next = "UD_OWN"
            with m.State("UH0"):                     # unhideElement
                pos.read(ud_v)
                m.next = "UH1"
            with m.State("UH1"):
                with m.If(pos.rdata < remaining):
                    m.next = "UD_META"
                with m.Else():
                    m.d.sync += [pq_p.eq(pos.rdata), pq_r.eq(remaining)]
                    heap.read(pos.rdata)
                    m.next = "UH2"
            with m.State("UH2"):
                m.d.sync += pq_swap.eq(heap.rdata)
                heap.read(pq_r)
                m.next = "UH3"
            with m.State("UH3"):
                heap.write(pq_p, heap.rdata)
                pos.write(heap.rdata.var, pq_p)
                m.next = "UH4"
            with m.State("UH4"):
                heap.write(pq_r, pq_swap)
                pos.write(ud_v, pq_r)
                m.d.sync += [sh_x.eq(pq_swap), sh_p.eq(pq_r), sh_save.eq(pq_r),
                         remaining.eq(pq_r + 1)]
                m.next = "UH_SH1"
            swap_higher("UH", "UD_META")
            with m.State("UD_META"):
                meta.read(ud_v)
                m.next = "UD_MW"
            with m.State("UD_MW"):
                mr = meta.rdata
                meta.write(ud_v, pack(L_META, ins=mr.ins, dec=mr.dec, stk=0,
                                      phase=Mux(bL > 0, pos_phase, ~pos_phase),
                                      ubl=mr.ubl, short=mr.short))
                m.next = "UD_TOP"

            # ============================================ minimize ========
            with m.State("MN_RD"):
                with m.If(mn_j == tomin_n):
                    m.d.sync += phase.eq(PH_SAVE)
                    m.next = "SV_0"
                with m.Else():
                    tomin.read(mn_j)
                    m.next = "MN_G"
            with m.State("MN_G"):
                v = var_of(tomin.rdata)
                lmmd.read(v)
                ubc.read(v)
                m.d.sync += g_v.eq(v)
                m.next = "MN_G2"
            with m.State("MN_G2"):
                m.d.sync += [g_mm.eq(lmmd.rdata), next_c.eq(ubc.rdata)]
                with m.If(lmmd.rdata.fix | lmmd.rdata.decide):
                    m.d.sync += mn_j.eq(mn_j + 1)
                    m.next = "MN_RD"
                with m.Else():
                    m.d.sync += [mq_h.eq(0), mq_t.eq(0), num_e.eq(0), cmk.eq(0)]
                    m.next = "MN_ITER"
            with m.State("MN_ITER"):
                inc("min_iter")
                m.d.sync += [hit.eq(0), cw_c.eq(next_c - 1)]
                m.next = "MM_CW0"
            clause_walk("MM", "MM_A", "MM_END")
            with m.State("MM_A"):                    # minimize_resolution_sort
                u = var_of(cw_lit)
                scm.read(u)
                vam.read(u >> 5)
                lmmd.read(u)
                inc("min_merge")
                m.next = "MM_B"
            with m.State("MM_B"):
                u = var_of(cw_lit)
                vbit = vam.rdata.bit_select(u[:5], 1)
                st0 = Mux(vbit, scm.rdata, 0)
                vam.write(u >> 5, vam.rdata | (C(1, 32) << u[:5])[:32])
                bit = Mux(cw_lit > 0, 1, 2)
                ins = (st0 & bit) == 0
                st1 = (st0 | bit)[:2]
                lr = lmmd.rdata
                cmp_ = (lr.keep > 0) | lr.fix
                with m.If(st1 == 3):
                    m.d.sync += num_e.eq(num_e - 1)
                with m.Elif(ins):
                    m.d.sync += num_e.eq(num_e + 1)
                    scm.write(u, st1)
                    with m.If(~hit):
                        with m.If(cmp_):
                            m.d.sync += cmk.eq(cmk + 1)
                        with m.Else():
                            mq.write(mq_h, u)
                            m.d.sync += mq_h.eq(mq_h + 1)
                            with m.If(lr.decide):
                                m.d.sync += hit.eq(1)
                m.next = "MM_CWN"
            with m.State("MM_END"):
                with m.If(~hit & (cmk == num_e)):
                    lmmd.write(g_v, pack(L_LMMD, keep=2, decide=g_mm.decide, fix=g_mm.fix))
                    m.d.sync += [did.eq(1), vc_i.eq(0)]
                    m.next = "MN_CLR"
                with m.Elif(hit | (mq_t >= mq_h)):
                    m.d.sync += [non_rem.eq(non_rem + 1), vc_i.eq(0)]
                    m.next = "MN_CLR"
                with m.Else():
                    mq.read(mq_t)
                    m.d.sync += mq_t.eq(mq_t + 1)
                    m.next = "MN_Q"
            with m.State("MN_Q"):
                ubc.read(mq.rdata)
                m.next = "MN_Q2"
            with m.State("MN_Q2"):
                m.d.sync += next_c.eq(ubc.rdata)
                m.next = "MN_ITER"
            with m.State("MN_CLR"):                  # ZERO_SEQ_2
                with m.If(vc_i == N_MAX // 32):
                    m.d.sync += mn_j.eq(mn_j + 1)
                    m.next = "MN_RD"
                with m.Else():
                    vam.write(vc_i, 0)
                    m.d.sync += vc_i.eq(vc_i + 1)

            # ============= SAVE: csh SAVE + writeClauseStream + allocate ===
            with m.State("SV_0"):
                cls_size = ((CE_MAX - fcp.nxt) >> 2)[:CA_W + 1] + fcp.cnt
                with m.If(found_abs):
                    m.d.sync += level.eq(0)
                    m.next = "LEARN_DONE"
                with m.Elif((cls_size * (CLS_PAGE - 1) < non_rem) | fid.empty):
                    m.d.sync += result.eq(-4)
                    m.next = "FINISH"
                with m.Else():
                    m.next = "SVA_P0"
            fcp.pop_states("SVA", "SV_A2")
            with m.State("SV_A2"):
                m.d.sync += sv_addr.eq(fcp.val)
                m.next = "SVB_P0"
            fid.pop_states("SVB", "SV_C")
            with m.State("SV_C"):
                m.d.sync += [sv_id.eq(fid.val), last_ins.eq(fid.val), comp.eq(0), udl_n.eq(0),
                         sub.eq(0), kept.eq(0), sv_i.eq(0)]
                cmd.write(fid.val, pack(L_CMD, start=sv_addr, num=non_rem))
                m.next = "SV_RD"
            with m.State("SV_RD"):
                with m.If(sv_i == num_el):
                    m.next = "SV_END"
                with m.Else():
                    rc.read(sv_i)
                    m.next = "SV_V"
            with m.State("SV_V"):
                v = var_of(rc.rdata)
                meta.read(v)
                lmmd.read(v)
                m.d.sync += [sv_x.eq(rc.rdata), sv_v.eq(v)]
                m.next = "SV_K"
            with m.State("SV_K"):
                mr, lr = meta.rdata, lmmd.rdata
                keep = ~lr.fix & (lr.keep == 1)
                lmmd.write(sv_v, pack(L_LMMD, keep=0, decide=lr.decide, fix=lr.fix))
                with m.If(keep):
                    m.d.sync += comp.eq(comp ^ sv_x)
                    found = Cat(*[(udl[k] == mr.dec) & (k < udl_n)
                                  for k in range(LBD_BUCKETS + 1)]).any()
                    with m.If(~found & (udl_n < LBD_BUCKETS + 1)):
                        with m.Switch(udl_n):
                            for k in range(LBD_BUCKETS + 1):
                                with m.Case(k):
                                    m.d.sync += udl[k].eq(mr.dec)
                        m.d.sync += udl_n.eq(udl_n + 1)
                    e = Cat(sv_x < 0, sv_v)
                    occ.read(e)
                    m.d.sync += sv_entry.eq(e)
                    m.next = "SV_O"
                with m.Else():
                    m.d.sync += sv_i.eq(sv_i + 1)
                    m.next = "SV_RD"
            with m.State("SV_O"):
                o = occ.rdata
                la = (o.latest + P - o.free - 2)[:LA_W]
                ca = (sv_addr + sub)[:CA_W]
                ls.write(la, sv_id + 1)
                occ.write(sv_entry, pack(L_OCC, start=o.start, latest=o.latest,
                                         num=o.num + 1, free=o.free - 1))
                cs.write(ca, fit(sv_x, CS_W))
                c2l.write(ca, la)
                l2c.write(la, ca)
                m.d.sync += [o_start.eq(o.start), o_latest.eq(o.latest), o_num.eq(o.num + 1),
                         o_free.eq(o.free - 1), kept.eq(kept + 1)]
                with m.If(sub == CLS_PAGE - 2):
                    m.d.sync += sub.eq(0)
                    with m.If(kept + 1 != non_rem):
                        m.next = "SVN_P0"
                    with m.Else():
                        m.next = "SV_Z"
                with m.Else():
                    m.d.sync += sub.eq(sub + 1)
                    m.next = "SV_AC"
            fcp.pop_states("SVN", "SV_NP")
            with m.State("SV_NP"):
                cs.write(sv_addr + CLS_PAGE - 1, fcp.val)
                m.d.sync += sv_addr.eq(fcp.val)
                m.next = "SV_AC"
            with m.State("SV_Z"):
                cs.write(sv_addr + CLS_PAGE - 1, 0)
                m.next = "SV_AC"
            with m.State("SV_AC"):                   # allocatePage when the page filled
                with m.If(o_free == 0):
                    with m.If(flp.empty):
                        m.d.sync += result.eq(-5)
                        m.next = "FINISH"
                    with m.Else():
                        m.next = "SVL_P0"
                with m.Else():
                    m.d.sync += sv_i.eq(sv_i + 1)
                    m.next = "SV_RD"
            flp.pop_states("SVL", "SV_AL1")
            with m.State("SV_AL1"):
                ls.write(o_latest + P - 1, flp.val)
                m.next = "SV_AL2"
            with m.State("SV_AL2"):
                ls.write(flp.val + P - 2, o_latest)
                occ.write(sv_entry, pack(L_OCC, start=o_start, latest=flp.val, num=o_num,
                                         free=P - 2))
                m.d.sync += sv_i.eq(sv_i + 1)
                m.next = "SV_RD"
            with m.State("SV_END"):                  # csh BUCKET + clsStates
                b = Mux(udl_n < 2, LBD_BUCKETS - 1, udl_n - 2)[:4]
                with m.If(bcnt[b] == 0):
                    m.d.sync += bhead[b].eq(sv_id)
                with m.Else():
                    bnext.write(btail[b], sv_id)
                m.d.sync += [btail[b].eq(sv_id), bcnt[b].eq(bcnt[b] + 1),
                         used_total.eq(used_total + 1)]
                cst.write(sv_id, Mux(reset_all, pack(L_CST, comp=comp, rem=non_rem),
                                     pack(L_CST, comp=uip, rem=1)))
                with m.If(did):
                    inc("simplified")
                    with m.If(non_rem > st["longest_simplified"]):
                        m.d.sync += st["longest_simplified"].eq(non_rem)
                m.d.sync += [level.eq(level_before), did.eq(0)]
                m.next = "LEARN_DONE"

            with m.State("LEARN_DONE"):
                with m.If(reset_all):
                    rt = ((used_total * prune_q16) >> 16)[:CLS_W + 1]
                    m.d.sync += [level.eq(0), remove_total.eq(rt), used_total.eq(used_total - rt),
                             pb.eq(LBD_BUCKETS - 1), removed.eq(0), phase.eq(PH_DELETE)]
                    m.next = "PR_SEL"
                with m.Else():
                    m.next = "POST"

            # ================================= DELETE (restart pruning) ===
            with m.State("PR_SEL"):                  # getDeletedClsID
                bi = pb[:4]
                with m.If((removed == remove_total) | (pb < 0)):
                    m.next = "POST"
                with m.Elif((bcnt[bi] == 0) | (bhead[bi] == last_ins)):
                    m.d.sync += pb.eq(pb - 1)
                with m.Else():
                    m.d.sync += d_r.eq(bhead[bi])
                    bnext.read(bhead[bi])
                    m.next = "PR_POP"
            with m.State("PR_POP"):
                bi = pb[:4]
                m.d.sync += [bhead[bi].eq(bnext.rdata), bcnt[bi].eq(bcnt[bi] - 1),
                         removed.eq(removed + 1), cw_c.eq(d_r)]
                inc("deleted")
                fid.push(d_r)
                m.next = "DL_CW0"
            clause_walk("DL", "DX_0", "PR_SEL")
            with m.State("DX_0"):                    # deleteClauses + deleteTransposedClauses
                with m.If(cw_cursub == 0):
                    fcp.push(cw_cur)
                c2l.read(cw_cur)
                e = Cat(cw_lit < 0, var_of(cw_lit))
                occ.read(e)
                m.d.sync += dx_entry.eq(e)
                m.next = "DX_1"
            with m.State("DX_1"):
                o = occ.rdata
                m.d.sync += [dx_la.eq(c2l.rdata), o_start.eq(o.start), o_num.eq(o.num)]
                with m.If(o.free == P - 2):
                    flp.push(o.latest)
                    ls.read(o.latest + P - 2)
                    m.next = "DX_2"
                with m.Else():
                    m.d.sync += [dx_latest.eq(o.latest), dx_free.eq(o.free + 1),
                             dx_swap.eq(o.latest + P - o.free - 3)]
                    m.next = "DX_3"
            with m.State("DX_2"):
                m.d.sync += [dx_latest.eq(ls.rdata), dx_free.eq(1), dx_swap.eq(ls.rdata + P - 3)]
                m.next = "DX_3"
            with m.State("DX_3"):
                ls.read(dx_swap)
                l2c.read(dx_swap)
                m.next = "DX_4"
            with m.State("DX_4"):
                m.d.sync += [dx_moved.eq(ls.rdata), dx_ca.eq(l2c.rdata)]
                ls.write(dx_la, ls.rdata)
                m.next = "DX_5"
            with m.State("DX_5"):                    # location_handler UPDATE
                ls.write(dx_swap, 0)
                occ.write(dx_entry, pack(L_OCC, start=o_start, latest=dx_latest,
                                         num=o_num - 1, free=dx_free))
                with m.If(dx_moved - 1 != d_r):
                    l2c.write(dx_la, dx_ca)
                    c2l.write(dx_ca, dx_la)
                m.next = "DL_CWN"

            # ======================== post-learn (solver.cpp tail) =======
            with m.State("POST"):
                m.d.sync += do_bt.eq(0)
                with m.If(level == 0):
                    nf = Mux(ins0 != 0, fixed + 1, fixed)[:LVL_W]
                    with m.If(ins0 != 0):
                        stk.write(height, ins0)
                    m.d.sync += [fixed.eq(nf), use_flipped.eq(0),
                             level.eq(Mux((ins0 == 0) | (nf == 0), 1, 0))]
                with m.Else():
                    m.d.sync += [use_flipped.eq(1), flipped.eq(uip)]
                m.next = "MAIN"

            with m.State("FINISH"):
                m.d.sync += done.eq(1)
                m.next = "IDLE"

        # simulation-only handles (the bitstream ignores these)
        self._dbg = dict(height=height, level=level, do_bt=do_bt, use_flipped=use_flipped,
                         flipped=flipped, fixed=fixed, total=st["total"], top_var=top_var,
                         next_c=next_c, num_el=num_el, uip=uip, level_before=level_before,
                         non_rem=non_rem, commit=commit, unsat0=unsat0, nunsat=nunsat,
                         remaining=remaining)
        self._mem = dict(stk=stk.mem, cst=cst.mem, heap=heap.mem, meta=meta.mem, lmmd=lmmd.mem)
        self._ram = dict(meta=meta, cst=cst, cmd=cmd)

        # ------------------------------------------- cycle accounting -----
        with m.If(busy):
            m.d.sync += cycles.eq(cycles + 1)
            with m.Switch(phase):
                for k in range(N_PHASES):
                    with m.Case(k):
                        m.d.sync += cyc[k].eq(cyc[k] + 1)

        # ------------------------------------------------- readback mux --
        caps = [N_MAX, C_MAX, LE_MAX, CE_MAX, MAX_LEARN, FRAC_W, MAGIC, VERSION]
        live = {"height": height, "level": level, "fixed": fixed,
                "cycles_lo": cycles[:32], "cycles_hi": cycles[32:]}
        with m.Switch(adr):
            with m.Case(R_CTRL):
                m.d.comb += self.wb_dat_r.eq(Cat(busy, done))
            with m.Case(R_RESULT):
                m.d.comb += self.wb_dat_r.eq(result)
            for a, s in [(R_NVARS, n_vars), (R_NCLS, n_cls), (R_LITELEMS, lit_elems),
                         (R_CLSELEMS, cls_elems), (R_FIXED, fixed_init),
                         (R_POSPHASE, pos_phase), (R_LITPAGE, P),
                         (R_RESETMULT, reset_mult), (R_PRUNE, prune_q16),
                         (R_INVDECAY, inv_decay), (R_MEMSEL, mem_sel), (R_MEMPTR, mem_ptr)]:
                with m.Case(a):
                    m.d.comb += self.wb_dat_r.eq(s)
            with m.Case(R_MEMDATA, "1--------"):
                with m.Switch(mem_sel):
                    with m.Case(M_LS):
                        m.d.comb += self.wb_dat_r.eq(ls.rdata)
                    with m.Case(M_CS):
                        m.d.comb += self.wb_dat_r.eq(cs.rdata)
                    with m.Case(M_CMD):
                        m.d.comb += self.wb_dat_r.eq(Cat(fit(cmd.rdata.start, 16), cmd.rdata.num))
                    with m.Case(M_CST):
                        m.d.comb += self.wb_dat_r.eq(Cat(fit(cst.rdata.comp, 16), cst.rdata.rem))
                    with m.Case(M_OCC):
                        o = occ.rdata
                        m.d.comb += self.wb_dat_r.eq(Mux(rd_odd,
                                                     Cat(fit(o.num, 16), fit(o.free, 16)),
                                                     Cat(fit(o.start, 16), fit(o.latest, 16))))
                    with m.Case(M_STK):
                        m.d.comb += self.wb_dat_r.eq(fit(stk.rdata, 32))
            for i, c in enumerate(caps):
                with m.Case(R_CAPS + i):
                    m.d.comb += self.wb_dat_r.eq(c)
            for i, n in enumerate(STAT_NAMES):
                with m.Case(R_STATS + i):
                    m.d.comb += self.wb_dat_r.eq(live.get(n, st[n]) if n != "state" else 0)
            for k in range(N_PHASES):
                with m.Case(R_CYCLES + 2 * k):
                    m.d.comb += self.wb_dat_r.eq(cyc[k][:32])
                with m.Case(R_CYCLES + 2 * k + 1):
                    m.d.comb += self.wb_dat_r.eq(cyc[k][32:])

        return m
