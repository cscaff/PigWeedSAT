"""CDCL SAT accelerator for the Lattice ECP5-85F, in Amaranth HDL.

A reimplementation of the openhw-2025 U55C HLS SAT solver
(openhw-2025-SAT-FPGA/hls/src) as one Wishbone B4 slave for the Manhattan
Reasoning cloud-FPGA SoC.  One Elaboratable per HLS kernel, scheduled by the
top level (solver.cpp) over shared block RAM:

  SATAccel        solver.cpp SOLVE_ITERATION loop + FIND_TOP; timer.cpp counters
  Propagator      decide.cpp checkUndecided, discover.cpp, color.cpp (forward)
  Learner         learn.cpp learnClause: resolution, findNextCls, backtrack level
  Backtracker     backtrack.cpp undoStates / updateStatesBackward
  Minimizer       minimize.cpp
  ClauseSaver     clause_store_handler SAVE/BUCKET, writeClauseStream, allocatePage
  Pruner          clause_store_handler DELETE, deleteTransposedClauses, location UPDATE
  PriorityQueue   pq_handler.cpp + priority_queue_functions.cpp (service)
  FreeList        mmuStream free page / clause-id queues (service)
  LbdBuckets      usedClsIDBuckets LBD FIFOs (service)
  Restart         restart.cpp Luby sequence (service)
  HostInterface   Wishbone register file (host link, message.cpp status)
  Memories        all solver RAMs; each kernel gets its own ports (_Ports),
                  OR-combined at the RAM, so a RAM's clients must not overlap in time

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
from amaranth.lib.fifo import SyncFIFO
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


# ==========================================================================
# Infrastructure: shared block RAMs with per-client ports, engine handshake.
# ==========================================================================

def _or_all(vals, width):
    vals = [Value.cast(v) for v in vals]
    if not vals:
        return C(0, width)
    acc = vals[0]
    for v in vals[1:]:
        acc = acc | v
    return acc


class _Port:
    """One client's view of a shared RAM.  Idle clients drive zeros, so the RAM
    ORs all clients together; the scheduler guarantees only one client of a
    RAM is active in any cycle (or that concurrent clients touch disjoint RAMs)."""

    def __init__(self, ram, owner):
        n = f"{owner}_{ram.name}"
        self.raddr = Signal(ram.addr_w, name=f"{n}_ra")
        self.waddr = Signal(ram.addr_w, name=f"{n}_wa")
        self.wdata = Signal(ram.width, name=f"{n}_wd")
        self.we = Signal(name=f"{n}_we")
        self.rdata = ram.rdata

    def read(self, m, addr):
        m.d.comb += self.raddr.eq(addr)

    def write(self, m, addr, value):
        m.d.comb += [self.waddr.eq(addr), self.wdata.eq(value), self.we.eq(1)]


class _RAM:
    """A block RAM: one read port (1-cycle, write-transparent) and one write port,
    multiplexed over any number of client ports (see _Port)."""

    def __init__(self, name, shape, depth):
        self.name = name
        self.depth = depth
        self.mem = Memory(shape=shape, depth=depth, init=[])
        self.w = self.mem.write_port()
        self.r = self.mem.read_port(transparent_for=(self.w,))
        self.rdata = self.r.data
        self.addr_w = len(self.r.addr)
        self.width = len(Value.cast(self.w.data))
        self.ports: list[_Port] = []

    def port(self, owner):
        p = _Port(self, owner)
        self.ports.append(p)
        return p

    def attach(self, m):
        m.submodules[self.name] = self.mem
        ps = self.ports
        m.d.comb += [
            self.r.addr.eq(_or_all([p.raddr for p in ps], self.addr_w)),
            self.w.addr.eq(_or_all([p.waddr for p in ps], self.addr_w)),
            self.w.data.eq(_or_all([p.wdata for p in ps], self.width)),
            self.w.en.eq(_or_all([p.we for p in ps], 1)),
        ]


class Memories:
    """Every solver memory (HLS: mLitStore, mClsStore, mCmd, mClsStates, mlmd,
    mlmmd, unitByCls, mAnswerStack, location_handler, learn/minimize scratch)."""

    SPEC = [
        ("ls", unsigned(LS_W), LE_MAX),           # literal (transposed) store
        ("cs", unsigned(CS_W), CE_MAX),           # clause store
        ("cmd", L_CMD, C_MAX),                    # clause metadata
        ("cst", L_CST, C_MAX),                    # clause states
        ("occ", L_OCC, 2 * N_MAX),                # literal metadata, list half
        ("meta", L_META, N_MAX),                  # literal metadata, variable half
        ("lmmd", L_LMMD, N_MAX),
        ("ubc", unsigned(CID_W), N_MAX),          # unitByCls
        ("stk", signed(LIT_W), N_MAX),            # answer stack (trail)
        ("send", unsigned(LVL_W), 2 * N_MAX),     # decisionLevelStackEnd
        ("c2l", unsigned(LA_W), CE_MAX),          # location: clause -> literal store
        ("l2c", unsigned(CA_W), LE_MAX),          # location: literal -> clause store
        ("scr", L_SCR, N_MAX),                    # learn merge scratchpad
        ("val", unsigned(32), N_MAX // 32),       # learn valid bits
        ("scm", unsigned(2), N_MAX),              # minimize scratchpad
        ("vam", unsigned(32), N_MAX // 32),       # minimize valid bits
        ("rc", signed(LIT_W), 2 * MAX_LEARN),     # resolution clause
        ("tomin", signed(LIT_W), MAX_LEARN),      # to-minimize list / pq scan list
        ("mq", unsigned(VAR_W), MAX_LEARN),       # minimize queue
    ]

    def __init__(self):
        for name, shape, depth in self.SPEC:
            setattr(self, name, _RAM(name, shape, depth))

    def attach(self, m):
        for name, _, _ in self.SPEC:
            getattr(self, name).attach(m)


class _Ports:
    """A client's ports, as attributes: ports = _Ports(mems, "bcp", "stk", "cst")."""

    def __init__(self, mems, owner, *names):
        for n in names:
            setattr(self, n, getattr(mems, n).port(owner))


class _ClauseWalk:
    """Iterate the literals of clause `c` through the paged clause store."""

    def __init__(self, p, name):
        self.p = p
        self.c = Signal(CLS_W, name=f"{name}_cw_c")
        self.addr = Signal(CA_W, name=f"{name}_cw_addr")
        self.num = Signal(NUM_W, name=f"{name}_cw_num")
        self.k = Signal(NUM_W, name=f"{name}_cw_k")
        self.sub = Signal(2, name=f"{name}_cw_sub")
        self.lit = Signal(signed(LIT_W), name=f"{name}_cw_lit")
        self.cur = Signal(CA_W, name=f"{name}_cw_cur")
        self.cursub = Signal(2, name=f"{name}_cw_cursub")

    def states(self, m, pfx, on_item, on_end):
        p = self.p
        with m.State(f"{pfx}_CW0"):
            p.cmd.read(m, self.c)
            m.next = f"{pfx}_CW1"
        with m.State(f"{pfx}_CW1"):
            m.d.sync += [self.addr.eq(p.cmd.rdata.start), self.num.eq(p.cmd.rdata.num),
                         self.k.eq(0), self.sub.eq(0)]
            m.next = f"{pfx}_CWN"
        with m.State(f"{pfx}_CWN"):
            with m.If(self.k == self.num):
                m.next = on_end
            with m.Else():
                p.cs.read(m, self.addr)
                m.next = f"{pfx}_CWG"
        with m.State(f"{pfx}_CWG"):
            m.d.sync += [self.lit.eq(p.cs.rdata[:LIT_W].as_signed()), self.cur.eq(self.addr),
                         self.cursub.eq(self.sub), self.addr.eq(self.addr + 1),
                         self.sub.eq(self.sub + 1), self.k.eq(self.k + 1)]
            with m.If((self.sub == CLS_PAGE - 2) & (self.k + 1 != self.num)):
                p.cs.read(m, self.addr + 1)
                m.next = f"{pfx}_CWJ"
            with m.Else():
                m.next = on_item
        with m.State(f"{pfx}_CWJ"):
            m.d.sync += [self.addr.eq(p.cs.rdata), self.sub.eq(0)]
            m.next = on_item


class _OccPipe:
    """Pipelined occurrence-list walk with a clause-state update per element
    (HLS colorStream -> updateStates{Forward,Backward} at II=1).

      A  issue ls[addr]              (or, at a page end, the next-page pointer)
      B  ls.rdata = clause id c1 ->  issue cst[c1-1]
      C  cst.rdata                 ->  on_update(c1, state)   (caller writes cst)

    One clause per cycle, one bubble per page jump.  No forwarding is needed:
    a clause occurs at most once in a literal's list, so the updates in flight
    always hit different clause states.  `can_issue` lets the caller throttle A.
    Pulse `start` with `e` set; `done` pulses once the last update has left C."""

    def __init__(self, p, P, name):
        self.p = p
        self.P = P
        self.e = Signal(VAR_W + 1, name=f"{name}_op_e")
        self.start = Signal(name=f"{name}_op_start")
        self.done = Signal(name=f"{name}_op_done")
        self.can_issue = Signal(init=1, name=f"{name}_op_can_issue")
        self.c1 = Signal(CID_W, name=f"{name}_op_c1")   # stage C clause id
        self.name = name

    def elaborate_into(self, m, on_update):
        p, P, n = self.p, self.P, self.name
        addr = Signal(LA_W, name=f"{n}_op_addr")
        num = Signal(LA_W + 1, name=f"{n}_op_num")
        k = Signal(LA_W + 1, name=f"{n}_op_k")
        i = Signal(FREE_W + 1, name=f"{n}_op_i")
        ptr = Signal(name=f"{n}_op_ptr")        # this cycle: issue the next-page pointer read
        jmp = Signal(name=f"{n}_op_jmp")        # this cycle: ls.rdata is the new page address
        b_valid = Signal(name=f"{n}_op_bv")
        c_valid = Signal(name=f"{n}_op_cv")

        m.d.sync += [b_valid.eq(0), c_valid.eq(b_valid)]
        with m.If(b_valid):                      # stage B
            p.cst.read(m, p.ls.rdata - 1)
            m.d.sync += self.c1.eq(p.ls.rdata)
        with m.If(c_valid):                      # stage C
            on_update(self.c1, p.cst.rdata)

        with m.FSM(name=f"{n}_occpipe"):
            with m.State("IDLE"):
                with m.If(self.start):
                    p.occ.read(m, self.e)
                    m.next = "SETUP"
            with m.State("SETUP"):
                m.d.sync += [addr.eq(p.occ.rdata.start), num.eq(p.occ.rdata.num),
                             k.eq(0), i.eq(0), ptr.eq(0), jmp.eq(0)]
                m.next = "RUN"
            with m.State("RUN"):                 # stage A
                cur = Mux(jmp, p.ls.rdata, addr)
                with m.If(jmp):
                    m.d.sync += [addr.eq(p.ls.rdata), jmp.eq(0)]
                with m.If(ptr):
                    p.ls.read(m, addr + 1)
                    m.d.sync += [ptr.eq(0), jmp.eq(1)]
                with m.Elif((k < num) & self.can_issue):
                    p.ls.read(m, cur)
                    m.d.sync += [b_valid.eq(1), k.eq(k + 1), addr.eq(cur + 1)]
                    with m.If((i + 1 == P - 2) & (k + 1 < num)):
                        m.d.sync += [i.eq(0), ptr.eq(1)]
                    with m.Else():
                        m.d.sync += i.eq(i + 1)
                with m.If((k == num) & ~ptr & ~jmp & ~b_valid & ~c_valid):
                    m.next = "DONE"
            with m.State("DONE"):
                m.d.comb += self.done.eq(1)
                m.next = "IDLE"


class _OccWalk:
    """Iterate occurrence list occ[e] through the paged literal store."""

    def __init__(self, p, P, name):
        self.p = p
        self.P = P
        self.e = Signal(VAR_W + 1, name=f"{name}_ow_e")
        self.addr = Signal(LA_W, name=f"{name}_ow_addr")
        self.num = Signal(LA_W + 1, name=f"{name}_ow_num")
        self.k = Signal(LA_W + 1, name=f"{name}_ow_k")
        self.i = Signal(FREE_W + 1, name=f"{name}_ow_i")
        self.c1 = Signal(CID_W, name=f"{name}_ow_c1")

    def states(self, m, pfx, on_item, on_end):
        p = self.p
        with m.State(f"{pfx}_OW0"):
            p.occ.read(m, self.e)
            m.next = f"{pfx}_OW1"
        with m.State(f"{pfx}_OW1"):
            m.d.sync += [self.addr.eq(p.occ.rdata.start), self.num.eq(p.occ.rdata.num),
                         self.k.eq(0), self.i.eq(0)]
            m.next = f"{pfx}_OWN"
        with m.State(f"{pfx}_OWN"):
            with m.If(self.k == self.num):
                m.next = on_end
            with m.Else():
                p.ls.read(m, self.addr)
                m.next = f"{pfx}_OWG"
        with m.State(f"{pfx}_OWG"):
            m.d.sync += [self.c1.eq(p.ls.rdata), self.addr.eq(self.addr + 1),
                         self.i.eq(self.i + 1), self.k.eq(self.k + 1)]
            with m.If(self.i + 1 == self.P - 2):
                p.ls.read(m, self.addr + 2)
                m.next = f"{pfx}_OWJ"
            with m.Else():
                m.next = on_item
        with m.State(f"{pfx}_OWJ"):
            m.d.sync += [self.addr.eq(p.ls.rdata), self.i.eq(0)]
            m.next = on_item


class _Engine(Elaboratable):
    """A kernel: pulse `start` (inputs are sampled then), wait for the `done` pulse.
    `clear` zeroes its statistics at the start of a solve."""

    STATS: tuple = ()

    def __init__(self):
        self.start = Signal()
        self.done = Signal()
        self.clear = Signal()
        self.stats = {n: Signal(32, name=f"{type(self).__name__}_{n}") for n in self.STATS}

    def _stat(self, m, name, by=1):
        m.d.sync += self.stats[name].eq(self.stats[name] + by)

    def _clear_stats(self, m):
        with m.If(self.clear):
            m.d.sync += [s.eq(0) for s in self.stats.values()]


class _Client:
    """Request lines a client drives into a shared service (OR-combined there)."""

    def __init__(self, owner, fields):
        for name, shape in fields:
            setattr(self, name, Signal(shape, name=f"{owner}_{name}"))
        self._fields = [n for n, _ in fields]


class _Service(Elaboratable):
    def __init__(self):
        self.clients: list[_Client] = []

    def _new_client(self, owner, fields):
        c = _Client(owner, fields)
        self.clients.append(c)
        return c

    def _merged(self, name, width):
        return _or_all([getattr(c, name) for c in self.clients], width)


# ==========================================================================
# Services: free-page / free-id queues, LBD buckets, priority queue, restart.
# ==========================================================================

class FreeList(_Service):
    """mmuStream: bump-allocate `start, start+inc, ...` below `limit`, then a ring
    of returned addresses.  Clients: push(v) and pop() -> `val` with `ack`."""

    def __init__(self, name, addr_w, ring_depth, limit, inc):
        super().__init__()
        self.name = name
        self.ring = _RAM(f"{name}_ring", unsigned(addr_w), ring_depth)
        self._rp = self.ring.port("fl")
        self.addr_w = addr_w
        self.limit = limit
        self.inc = inc
        iw = (ring_depth - 1).bit_length()
        self.reset = Signal(name=f"{name}_reset")
        self.start = Signal(addr_w + 1, name=f"{name}_start")
        self.nxt = Signal(addr_w + 2, name=f"{name}_nxt")
        self.head = Signal(iw, name=f"{name}_head")
        self.tail = Signal(iw, name=f"{name}_tail")
        self.cnt = Signal(iw + 1, name=f"{name}_cnt")
        self.val = Signal(addr_w, name=f"{name}_val")
        self.ack = Signal(name=f"{name}_ack")
        self.bump_ok = Signal(name=f"{name}_bump_ok")
        self.empty = Signal(name=f"{name}_empty")

    def client(self, owner):
        return self._new_client(f"{owner}_{self.name}",
                                [("push", 1), ("push_v", self.addr_w), ("pop", 1)])

    def elaborate(self, platform):
        m = Module()
        self.ring.attach(m)
        push = self._merged("push", 1)
        push_v = self._merged("push_v", self.addr_w)
        pop = self._merged("pop", 1)
        m.d.comb += [self.bump_ok.eq(self.nxt + self.inc <= self.limit),
                     self.empty.eq(~self.bump_ok & (self.cnt == 0))]
        ring_pop = Signal()
        with m.If(push):
            self._rp.write(m, self.tail, push_v)
            m.d.sync += self.tail.eq(self.tail + 1)
        m.d.sync += self.cnt.eq(self.cnt + push - ring_pop)
        with m.FSM():
            with m.State("IDLE"):
                with m.If(pop):
                    with m.If(self.bump_ok):
                        m.d.sync += [self.val.eq(self.nxt), self.nxt.eq(self.nxt + self.inc)]
                        m.next = "ACK"
                    with m.Else():
                        self._rp.read(m, self.head)
                        m.d.comb += ring_pop.eq(1)
                        m.d.sync += self.head.eq(self.head + 1)
                        m.next = "RD"
            with m.State("RD"):
                m.d.sync += self.val.eq(self.ring.rdata)
                m.next = "ACK"
            with m.State("ACK"):
                m.d.comb += self.ack.eq(1)
                m.next = "IDLE"
        with m.If(self.reset):
            m.d.sync += [self.nxt.eq(self.start), self.head.eq(0), self.tail.eq(0),
                         self.cnt.eq(0)]
        return m


class LbdBuckets(_Service):
    """clause_store_handler usedClsIDBuckets + tracker[]: one FIFO of learned-clause
    ids per LBD bucket, kept as linked lists through `next`, plus usedTotalIDCount."""

    def __init__(self):
        super().__init__()
        self.next = _RAM("bucket_next", unsigned(CLS_W), C_MAX)
        self._np = self.next.port("bk")
        self.reset = Signal()
        self.used_total = Signal(CLS_W + 1)
        self.head = Array(Signal(CLS_W, name=f"bhead{k}") for k in range(LBD_BUCKETS))
        self.tail = Array(Signal(CLS_W, name=f"btail{k}") for k in range(LBD_BUCKETS))
        self.cnt = Array(Signal(CLS_W + 1, name=f"bcnt{k}") for k in range(LBD_BUCKETS))
        # query / pop (pruner)
        self.q = Signal(4)
        self.q_head = Signal(CLS_W)
        self.q_cnt = Signal(CLS_W + 1)
        self.pop_next = Signal(CLS_W)        # bucket_next[head[q]], valid the cycle after pop_req
        self.lbd_hist = [Signal(32, name=f"lbd{k}") for k in range(LBD_BUCKETS)]

    def client(self, owner):
        return self._new_client(f"{owner}_bk", [
            ("append", 1), ("append_b", 4), ("append_id", CLS_W),
            ("pop_req", 1), ("pop_commit", 1), ("sub_used", 1), ("sub_n", CLS_W + 1)])

    def elaborate(self, platform):
        m = Module()
        self.next.attach(m)
        g = self._merged
        append, b, cid = g("append", 1), g("append_b", 4), g("append_id", CLS_W)
        pop_req, pop_commit = g("pop_req", 1), g("pop_commit", 1)
        sub_used, sub_n = g("sub_used", 1), g("sub_n", CLS_W + 1)
        m.d.comb += [self.q_head.eq(self.head[self.q]), self.q_cnt.eq(self.cnt[self.q]),
                     self.pop_next.eq(self.next.rdata)]
        with m.If(append):
            with m.If(self.cnt[b] == 0):
                m.d.sync += self.head[b].eq(cid)
            with m.Else():
                self._np.write(m, self.tail[b], cid)
            m.d.sync += [self.tail[b].eq(cid), self.cnt[b].eq(self.cnt[b] + 1),
                         self.used_total.eq(self.used_total + 1)]
            with m.Switch(b):
                for k in range(LBD_BUCKETS):
                    with m.Case(k):
                        m.d.sync += self.lbd_hist[k].eq(self.lbd_hist[k] + 1)
        with m.If(pop_req):
            self._np.read(m, self.head[self.q])
        with m.If(pop_commit):
            m.d.sync += [self.head[self.q].eq(self.next.rdata),
                         self.cnt[self.q].eq(self.cnt[self.q] - 1)]
        with m.If(sub_used):
            m.d.sync += self.used_total.eq(self.used_total - sub_n)
        with m.If(self.reset):
            m.d.sync += [self.used_total.eq(0), *[self.cnt[k].eq(0) for k in range(LBD_BUCKETS)],
                         *[h.eq(0) for h in self.lbd_hist]]
        return m


class PriorityQueue(_Service):
    """pq_handler.cpp + priority_queue_functions.cpp: the VSIDS binary heap.

    Ops: INIT (loadPositioning), PEEK i (heap[i].var -> result), HIDE v
    (hideElement), UNHIDE v (unhideElement), BUMP v (decayEveryElement body,
    incl. the rescale ADJUST loop), DECAY (multiplier *= 1/decay).

    Requests enter a command FIFO and execute strictly in order (the HLS
    pqHandlerInput stream), so clients fire and forget: they only wait for
    `ready` (FIFO not full).  PEEK answers with a `result_valid` pulse;
    `idle` means every queued command has finished."""

    INIT, PEEK, HIDE, UNHIDE, BUMP, DECAY = 1, 2, 3, 4, 5, 6
    CMD_FIFO = 32

    def __init__(self, n_vars, inv_decay):
        super().__init__()
        self.n_vars = n_vars
        self.inv_decay = inv_decay
        self.heap_ram = _RAM("pq_heap", L_HEAP, N_MAX)
        self.pos_ram = _RAM("pq_pos", unsigned(VAR_W), N_MAX)
        self.heap = self.heap_ram.port("pq")
        self.pos = self.pos_ram.port("pq")
        self.done = Signal()             # pulses as each command completes
        self.result = Signal(VAR_W)
        self.result_valid = Signal()
        self.ready = Signal()
        self.idle = Signal()
        self.remaining = Signal(LVL_W)
        self.mult = Signal(SCORE_W)

    def client(self, owner):
        return self._new_client(f"{owner}_pq", [("req", 1), ("op", 3), ("arg", LVL_W)])

    @staticmethod
    def call(m, client, op, arg=0):
        m.d.comb += [client.req.eq(1), client.op.eq(op), client.arg.eq(arg)]

    def elaborate(self, platform):  # noqa: C901
        m = Module()
        self.heap_ram.attach(m)
        self.pos_ram.attach(m)
        heap, pos = self.heap, self.pos
        req, op_in, arg_in = (self._merged("req", 1), self._merged("op", 3),
                              self._merged("arg", LVL_W))
        remaining, mult = self.remaining, self.mult
        m.submodules.cmd_fifo = cf = SyncFIFO(width=3 + LVL_W, depth=self.CMD_FIFO)
        m.d.comb += [cf.w_en.eq(req), cf.w_data.eq(Cat(op_in, arg_in)), self.ready.eq(cf.w_rdy)]
        op = cf.r_data[:3]
        arg = cf.r_data[3:]

        a = Signal(LVL_W)          # op argument (index or 0-based variable)
        i = Signal(LVL_W + 1)
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
        bm_s = Signal(SCORE_W)

        with m.FSM() as fsm:
            m.d.comb += self.idle.eq(fsm.ongoing("IDLE") & ~cf.r_rdy)
            with m.State("IDLE"):
                with m.If(cf.r_rdy):
                    m.d.comb += cf.r_en.eq(1)
                    m.d.sync += a.eq(arg)
                    with m.Switch(op):
                        with m.Case(self.INIT):
                            m.d.sync += i.eq(0)
                            m.next = "INIT"
                        with m.Case(self.PEEK):
                            heap.read(m, arg)
                            m.next = "PEEK"
                        with m.Case(self.HIDE):
                            pos.read(m, arg)
                            m.next = "H1"
                        with m.Case(self.UNHIDE):
                            pos.read(m, arg)
                            m.next = "UH1"
                        with m.Case(self.BUMP):
                            pos.read(m, arg)
                            m.next = "BM1"
                        with m.Case(self.DECAY):
                            m.d.sync += mult.eq(fp_mul(mult, self.inv_decay))
                            m.next = "DONE"
            with m.State("DONE"):
                m.d.comb += self.done.eq(1)
                m.next = "IDLE"
            with m.State("INIT"):                    # loadPositioning
                with m.If(i == N_MAX):
                    m.d.sync += [remaining.eq(self.n_vars), mult.eq(SCORE_ONE)]
                    m.next = "DONE"
                with m.Else():
                    heap.write(m, i, pack(L_HEAP, s=0, var=i))
                    pos.write(m, i, i)
                    m.d.sync += i.eq(i + 1)
            with m.State("PEEK"):
                m.d.sync += self.result.eq(heap.rdata.var)
                m.next = "PEEK_OUT"
            with m.State("PEEK_OUT"):
                m.d.comb += [self.done.eq(1), self.result_valid.eq(1)]
                m.next = "IDLE"

            # hideElement + swapLower
            with m.State("H1"):
                m.d.sync += [pq_p.eq(pos.rdata), pq_r.eq(remaining - 1)]
                heap.read(m, pos.rdata)
                m.next = "H2"
            with m.State("H2"):
                m.d.sync += pq_swap.eq(heap.rdata)
                heap.read(m, pq_r)
                m.next = "H3"
            with m.State("H3"):
                m.d.sync += pq_last.eq(heap.rdata)
                heap.write(m, pq_p, heap.rdata)
                m.next = "H4"
            with m.State("H4"):
                pos.write(m, pq_last.var, pq_p)
                heap.write(m, pq_r, pq_swap)
                m.next = "H5"
            with m.State("H5"):
                pos.write(m, a, pq_r)
                m.d.sync += [remaining.eq(pq_r), sl_x.eq(pq_last), sl_p.eq(pq_p),
                             sl_rem.eq(pq_r)]
                m.next = "SL0"
            with m.State("SL0"):
                m.d.sync += sl_l.eq(2 * sl_p + 1)
                heap.read(m, 2 * sl_p + 1)
                m.next = "SL1"
            with m.State("SL1"):
                m.d.sync += sl_left.eq(heap.rdata)
                heap.read(m, sl_l + 1)
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
                    heap.write(m, sl_p, right)
                    m.d.sync += [sl_use_var.eq(right.var), sl_p.eq(sl_l + 1), sl_done.eq(0)]
                with m.Elif(~e1 & ~e3):
                    heap.write(m, sl_p, sl_x)
                    m.d.sync += [sl_use_var.eq(sl_x.var), sl_done.eq(1)]
                with m.Else():
                    heap.write(m, sl_p, sl_left)
                    m.d.sync += [sl_use_var.eq(sl_left.var), sl_p.eq(sl_l), sl_done.eq(0)]
                m.next = "SL3"
            with m.State("SL3"):
                pos.write(m, sl_use_var, sl_save)
                with m.If(sl_done):
                    m.next = "SL4"
                with m.Else():
                    m.next = "SL0"
            with m.State("SL4"):
                pos.write(m, sl_x.var, sl_save)
                heap.write(m, sl_save, sl_x)
                m.next = "DONE"

            # unhideElement
            with m.State("UH1"):
                with m.If(pos.rdata < remaining):
                    m.next = "DONE"
                with m.Else():
                    m.d.sync += [pq_p.eq(pos.rdata), pq_r.eq(remaining)]
                    heap.read(m, pos.rdata)
                    m.next = "UH2"
            with m.State("UH2"):
                m.d.sync += pq_swap.eq(heap.rdata)
                heap.read(m, pq_r)
                m.next = "UH3"
            with m.State("UH3"):
                heap.write(m, pq_p, heap.rdata)
                pos.write(m, heap.rdata.var, pq_p)
                m.next = "UH4"
            with m.State("UH4"):
                heap.write(m, pq_r, pq_swap)
                pos.write(m, a, pq_r)
                m.d.sync += [sh_x.eq(pq_swap), sh_p.eq(pq_r), sh_save.eq(pq_r),
                             remaining.eq(pq_r + 1)]
                m.next = "SH1"

            # decayEveryElement body: bump one activity (+ the 1e100 ADJUST loop)
            with m.State("BM1"):
                m.d.sync += pq_p.eq(pos.rdata)
                heap.read(m, pos.rdata)
                m.next = "BM2"
            with m.State("BM2"):
                m.d.sync += bm_s.eq(fp_add(heap.rdata.s, mult))
                m.next = "BM3"
            with m.State("BM3"):
                heap.write(m, pq_p, pack(L_HEAP, s=bm_s, var=a))
                with m.If(bm_s[FRAC_W:] >= EXP_BIAS + RESCALE_EXP):
                    m.d.sync += i.eq(0)
                    m.next = "RS0"
                with m.Else():
                    m.next = "BM4"
            with m.State("RS0"):
                with m.If(i == self.n_vars):
                    m.d.sync += [mult.eq(fp_rescale(mult)), bm_s.eq(fp_rescale(bm_s))]
                    m.next = "BM4"
                with m.Else():
                    heap.read(m, i)
                    m.next = "RS1"
            with m.State("RS1"):
                heap.write(m, i, pack(L_HEAP, s=fp_rescale(heap.rdata.s), var=heap.rdata.var))
                m.d.sync += i.eq(i + 1)
                m.next = "RS0"
            with m.State("BM4"):
                with m.If(pq_p < remaining):
                    m.d.sync += [sh_x.eq(pack(L_HEAP, s=bm_s, var=a)), sh_p.eq(pq_p),
                                 sh_save.eq(pq_p)]
                    m.next = "SH1"
                with m.Else():
                    m.next = "DONE"

            # swapHigher (shared by UNHIDE and BUMP)
            with m.State("SH1"):
                with m.If(sh_p == 0):
                    m.next = "SHE"
                with m.Else():
                    heap.read(m, (sh_p - 1) >> 1)
                    m.next = "SH2"
            with m.State("SH2"):
                np_ = ((sh_p - 1) >> 1)[:VAR_W]
                with m.If(heap.rdata.s < sh_x.s):
                    heap.write(m, sh_p, heap.rdata)
                    pos.write(m, heap.rdata.var, sh_p)
                    m.d.sync += [sh_save.eq(np_), sh_p.eq(np_)]
                    m.next = "SH1"
                with m.Else():
                    m.next = "SHE"
            with m.State("SHE"):
                pos.write(m, sh_x.var, sh_save)
                heap.write(m, sh_save, sh_x)
                m.next = "DONE"
        return m


class Restart(Elaboratable):
    """restart.cpp: Luby sequence x RESET_MULTIPLIER.  `tick` once per conflict;
    `fire` (valid the next cycle) says this conflict triggers a restart."""

    def __init__(self, reset_mult):
        self.reset_mult = reset_mult
        self.reset = Signal()
        self.tick = Signal()
        self.fire = Signal()

    def elaborate(self, platform):
        m = Module()
        limit = Signal(32)
        count = Signal(32)
        u = Signal(32)
        vlog = Signal(5)
        with m.If(self.tick):
            lc = (count + 1)[:32]
            with m.If(limit == lc):
                lowbit = (u & -u)[:32]
                m.d.sync += [count.eq(0), limit.eq(self.reset_mult << vlog), self.fire.eq(1)]
                with m.If(lowbit == (C(1, 32) << vlog)[:32]):
                    m.d.sync += [u.eq(u + 1), vlog.eq(0)]
                with m.Else():
                    m.d.sync += vlog.eq(vlog + 1)
            with m.Else():
                m.d.sync += [count.eq(lc), self.fire.eq(0)]
        with m.If(self.reset):
            m.d.sync += [limit.eq(self.reset_mult), count.eq(0), u.eq(2), vlog.eq(0)]
        return m


# ==========================================================================
# Kernels.
# ==========================================================================

class Propagator(_Engine):
    """decide.cpp checkUndecided + discover.cpp (discover, updateStatesForward,
    controlSink) + color.cpp (forward): assign the decision / flipped / fixed
    literals and propagate to a fixed point or the first conflict.

    Each trail literal's occurrence list streams through _OccPipe at one
    clause per cycle; unit literals it finds queue in a FIFO and are applied
    (discover) concurrently, in walk order, by a second FSM."""

    STATS = ("check_cnt",)
    UNIT_FIFO = 16

    def __init__(self, mems, cfg):
        super().__init__()
        self.cfg = cfg
        self.p = _Ports(mems, "bcp", "stk", "meta", "lmmd", "ubc", "occ", "ls", "cst", "cmd")
        self.pipe = _OccPipe(self.p, cfg.P, "bcp")
        # inputs (sampled at start)
        self.i_height = Signal(LVL_W)
        self.i_fixed = Signal(LVL_W)
        self.level = Signal(LVL_W)
        self.use_flipped = Signal()
        self.flipped = Signal(signed(LIT_W))
        self.top_var = Signal(VAR_W)
        self.given = Signal(CLS_W)
        # outputs
        self.height = Signal(LVL_W)
        self.fixed = Signal(LVL_W)
        self.conflict = Signal()
        self.commit = Signal(signed(LVL_W + 1))
        self.nunsat = Signal(2)
        self.unsat0 = Signal(CID_W)
        self.unsat1 = Signal(CID_W)

    def elaborate(self, platform):  # noqa: C901
        m = Module()
        self._clear_stats(m)
        p, pipe, cfg = self.p, self.pipe, self.cfg
        height, fixed, level = self.height, self.fixed, self.level
        conflict, nunsat = self.conflict, self.nunsat
        qh = Signal(LVL_W)
        bL = Signal(signed(LIT_W))
        flipped, top_var = self.flipped, self.top_var

        m.submodules.unit_fifo = fifo = SyncFIFO(width=LIT_W + CID_W, depth=self.UNIT_FIFO)
        m.d.comb += pipe.can_issue.eq(fifo.level < self.UNIT_FIFO - 3)

        def on_update(c1, st):                   # updateStatesForward
            ncomp = (st.comp.as_unsigned() ^ (-bL)[:LIT_W])[:LIT_W].as_signed()
            nrem = (st.rem - 1)[:REM_W]
            p.cst.write(m, c1 - 1, pack(L_CST, comp=ncomp, rem=nrem))
            with m.If((nrem == 1) & ~conflict):
                m.d.comb += [fifo.w_en.eq(1), fifo.w_data.eq(Cat(ncomp, c1))]
            with m.Elif(nrem == 0):
                with m.If(nunsat == 0):
                    m.d.sync += [self.unsat0.eq(c1), nunsat.eq(1)]
                with m.Elif(nunsat == 1):
                    m.d.sync += [self.unsat1.eq(c1), nunsat.eq(2)]
                m.d.sync += conflict.eq(1)
        pipe.elaborate_into(m, on_update)

        # ---- discover: apply queued unit literals in order -----------------
        u_lit = Signal(signed(LIT_W))
        u_c1 = Signal(CID_W)
        u_m = Signal(L_META)
        u_len = Signal(NUM_W)
        with m.FSM(name="discover") as dfsm:
            with m.State("IDLE"):
                with m.If(fifo.r_rdy):
                    m.d.comb += fifo.r_en.eq(1)
                    ul = fifo.r_data[:LIT_W].as_signed()
                    uc = fifo.r_data[LIT_W:]
                    m.d.sync += [u_lit.eq(ul), u_c1.eq(uc)]
                    p.cmd.read(m, uc - 1)
                    p.meta.read(m, var_of(ul))
                    m.next = "U1"
            with m.State("U1"):
                v = var_of(u_lit)
                mr = p.meta.rdata
                m.d.sync += [u_m.eq(mr), u_len.eq(p.cmd.rdata.num)]
                with m.If(~mr.stk):
                    p.meta.write(m, v, pack(L_META, ins=height, dec=level, stk=1, phase=mr.phase,
                                            ubl=bL, short=p.cmd.rdata.num))
                    p.ubc.write(m, v, u_c1)
                    p.stk.write(m, height, u_lit)
                    p.lmmd.read(m, v)
                    m.d.sync += height.eq(height + 1)
                    m.next = "U2"
                with m.Else():
                    p.stk.read(m, mr.ins)
                    m.next = "U3"
            with m.State("U2"):
                v = var_of(u_lit)
                p.lmmd.write(m, v, pack(L_LMMD, keep=p.lmmd.rdata.keep, decide=0,
                                        fix=p.lmmd.rdata.fix | (level == 0)))
                with m.If(level == 0):
                    m.d.sync += fixed.eq(fixed + 1)
                m.next = "IDLE"
            with m.State("U3"):                      # shorter reason for a duplicate
                v = var_of(u_lit)
                with m.If((p.stk.rdata == u_lit) & (u_m.ubl == bL) & (u_len < u_m.short)):
                    p.ubc.write(m, v, u_c1)
                    p.meta.write(m, v, pack(L_META, ins=u_m.ins, dec=u_m.dec, stk=u_m.stk,
                                            phase=u_m.phase, ubl=u_m.ubl, short=u_len))
                m.next = "IDLE"
        units_idle = dfsm.ongoing("IDLE") & ~fifo.r_rdy

        walked = Signal()
        with m.FSM():
            with m.State("IDLE"):
                with m.If(self.start):
                    m.d.sync += [height.eq(self.i_height), fixed.eq(self.i_fixed),
                                 qh.eq(self.i_height), conflict.eq(0), nunsat.eq(0)]
                    with m.If(self.use_flipped):
                        p.meta.read(m, var_of(flipped))
                        p.lmmd.read(m, var_of(flipped))
                        m.next = "FLIP"
                    with m.Elif(level == 0):
                        m.next = "FX_RD"
                    with m.Else():
                        p.meta.read(m, top_var)
                        p.lmmd.read(m, top_var)
                        m.next = "DECIDE"
            with m.State("FIN"):
                m.d.comb += self.done.eq(1)
                m.next = "IDLE"
            with m.State("FX_RD"):                   # WRITE_FIXED_DECISION
                with m.If(height >= fixed):
                    m.next = "Q"
                with m.Else():
                    p.stk.read(m, height)
                    m.next = "FX_M"
            with m.State("FX_M"):
                p.meta.read(m, var_of(p.stk.rdata))
                p.lmmd.read(m, var_of(p.stk.rdata))
                m.d.sync += bL.eq(p.stk.rdata)
                m.next = "FX_W"
            with m.State("FX_W"):
                v = var_of(bL)
                p.meta.write(m, v, pack(L_META, ins=height, dec=0, stk=1,
                                        phase=Mux(bL > 0, cfg.pos_phase, ~cfg.pos_phase),
                                        ubl=0, short=p.meta.rdata.short))
                p.lmmd.write(m, v, pack(L_LMMD, keep=p.lmmd.rdata.keep, decide=1, fix=1))
                m.d.sync += height.eq(height + 1)
                m.next = "FX_RD"
            with m.State("DECIDE"):
                g = Mux(p.meta.rdata.phase != cfg.pos_phase, -(top_var + 1), top_var + 1)
                p.meta.write(m, top_var, pack(L_META, ins=height, dec=level, stk=1,
                                              phase=p.meta.rdata.phase, ubl=0,
                                              short=p.meta.rdata.short))
                p.lmmd.write(m, top_var, pack(L_LMMD, keep=p.lmmd.rdata.keep, decide=1, fix=0))
                p.stk.write(m, height, g)
                m.d.sync += height.eq(height + 1)
                m.next = "Q"
            with m.State("FLIP"):
                v = var_of(flipped)
                p.meta.write(m, v, pack(L_META, ins=height, dec=level, stk=1,
                                        phase=Mux(flipped > 0, cfg.pos_phase, ~cfg.pos_phase),
                                        ubl=0, short=p.meta.rdata.short))
                p.lmmd.write(m, v, pack(L_LMMD, keep=p.lmmd.rdata.keep, decide=0,
                                        fix=p.lmmd.rdata.fix))
                p.ubc.write(m, v, self.given + 1)
                p.stk.write(m, height, flipped)
                m.d.sync += height.eq(height + 1)
                m.next = "Q"
            with m.State("Q"):                       # colorStream: next literal
                with m.If((qh < height) & ~conflict):
                    p.stk.read(m, qh)
                    m.next = "Q_L"
                with m.Else():
                    m.d.sync += self.commit.eq(qh - 1)
                    m.next = "FIN"
            with m.State("Q_L"):
                m.d.sync += [bL.eq(p.stk.rdata), qh.eq(qh + 1), walked.eq(0)]
                m.d.comb += [pipe.e.eq(Cat(p.stk.rdata > 0, var_of(p.stk.rdata))),
                             pipe.start.eq(1)]
                self._stat(m, "check_cnt")
                m.next = "WALK"
            with m.State("WALK"):                    # wait for the walk and its units
                with m.If(pipe.done):
                    m.d.sync += walked.eq(1)
                with m.If(walked & units_idle):
                    m.next = "Q"
        return m


class Learner(_Engine):
    """learn.cpp learnClause, up to the backtrack target: GET_SHORTEST_START,
    RESOLUTION (merge_resolution_sort + part_2 + findNextCls), ZERO_SEQ, and
    GET_BT_LEVEL / FIND_ABSOLUTE / GET_MINIMIZE."""

    STATS = ("learn_iter", "learn_merge", "longest")

    def __init__(self, mems, cfg, pq):
        super().__init__()
        self.cfg = cfg
        self.p = _Ports(mems, "ln", "cmd", "cs", "scr", "val", "meta", "lmmd", "rc", "stk",
                        "ubc", "tomin", "send")
        self.cw = _ClauseWalk(self.p, "ln")
        self.pq = pq.client("ln")
        self.pq_ready = pq.ready
        # inputs
        self.nunsat = Signal(2)
        self.unsat0 = Signal(CID_W)
        self.unsat1 = Signal(CID_W)
        self.level = Signal(LVL_W)
        self.reset_all = Signal()
        # outputs
        self.error = Signal()
        self.num_el = Signal(CNT_W)
        self.found_abs = Signal()
        self.level_before = Signal(signed(LVL_W + 1))
        self.uip = Signal(signed(LIT_W))
        self.ins0 = Signal(signed(LIT_W))
        self.non_rem = Signal(CNT_W)
        self.tomin_n = Signal(LRN_W)
        self.bt_target = Signal(LVL_W)

    def elaborate(self, platform):  # noqa: C901
        m = Module()
        self._clear_stats(m)
        p, cw, PQ = self.p, self.cw, PriorityQueue
        level, num_el, found_abs = self.level, self.num_el, self.found_abs
        level_before, non_rem, tomin_n = self.level_before, self.non_rem, self.tomin_n
        next_c = Signal(CID_W)
        best_len = Signal(NUM_W)
        stream = Signal(CNT_W)
        highest = Signal(LVL_W)
        fixed_cnt = Signal(signed(CNT_W))
        trail_end = Signal(signed(LVL_W + 1))
        set_once = Signal()
        is_uip = Signal()
        rh = Signal(2)
        res_pos = Signal(LRN_W)
        poss = Signal(signed(LIT_W))
        once = Signal()
        fn_i = Signal(signed(LVL_W + 1))
        fn_save = Signal(signed(LVL_W + 1))
        fn_v = Signal(VAR_W)
        vc_i = Signal(VAR_W)
        bl_i = Signal(CNT_W)
        bl_x = Signal(signed(LIT_W))
        mode_a = Signal()
        bv = Signal(VAR_W)

        with m.FSM():
            with m.State("IDLE"):
                with m.If(self.start):
                    p.cmd.read(m, self.unsat0 - 1)
                    m.d.sync += self.error.eq(0)
                    m.next = "S1"
            with m.State("FIN"):
                m.d.comb += self.done.eq(1)
                m.next = "IDLE"
            with m.State("S1"):                      # GET_SHORTEST_START
                m.d.sync += [next_c.eq(self.unsat0), best_len.eq(p.cmd.rdata.num)]
                with m.If(self.nunsat > 1):
                    p.cmd.read(m, self.unsat1 - 1)
                    m.next = "S2"
                with m.Else():
                    m.next = "INIT"
            with m.State("S2"):
                with m.If(p.cmd.rdata.num < best_len):
                    m.d.sync += next_c.eq(self.unsat1)
                m.next = "INIT"
            with m.State("INIT"):
                m.d.sync += [num_el.eq(0), stream.eq(0), highest.eq(0), fixed_cnt.eq(0),
                             trail_end.eq(0), set_once.eq(0), is_uip.eq(0), found_abs.eq(0)]
                m.next = "ITER"
            with m.State("ITER"):                    # RESOLUTION loop head
                with m.If(is_uip | found_abs):
                    m.d.sync += vc_i.eq(0)
                    m.next = "DECAY"
                with m.Else():
                    self._stat(m, "learn_iter")
                    m.d.sync += [rh.eq(0), res_pos.eq(0), once.eq(highest != 0),
                                 cw.c.eq(next_c - 1), poss.eq(0)]
                    with m.If(num_el > 0):
                        p.rc.read(m, num_el - 1)
                        m.next = "P"
                    with m.Else():
                        m.next = "MG_CW0"
            with m.State("P"):
                m.d.sync += poss.eq(p.rc.rdata)
                m.next = "MG_CW0"
            cw.states(m, "MG", "MG_A", "MG_END")
            with m.State("MG_A"):                    # merge_resolution_sort
                v = var_of(cw.lit)
                p.scr.read(m, v)
                p.val.read(m, v >> 5)
                p.meta.read(m, v)
                p.lmmd.read(m, v)
                self._stat(m, "learn_merge")
                m.next = "MG_B"
            with m.State("MG_B"):
                v = var_of(cw.lit)
                vbit = p.val.rdata.bit_select(v[:5], 1)
                st0 = Mux(vbit, p.scr.rdata.st, 0)
                pos0 = Mux(vbit, p.scr.rdata.pos, 0)
                p.val.write(m, v >> 5, p.val.rdata | (C(1, 32) << v[:5])[:32])
                bit = Mux(cw.lit > 0, 1, 2)
                ins = (st0 & bit) == 0
                st1 = (st0 | bit)[:2]
                mr, lr = p.meta.rdata, p.lmmd.rdata
                with m.If(st1 == 3):
                    with m.If(pos0 != (num_el - 1)[:CNT_W]):
                        p.rc.write(m, pos0, poss)
                        m.d.sync += [res_pos.eq(pos0), rh.eq(1)]
                    with m.Else():
                        m.d.sync += rh.eq(2)
                    m.d.sync += num_el.eq(num_el - 1)
                    p.scr.write(m, v, 0)
                    with m.If(lr.fix):
                        m.d.sync += fixed_cnt.eq(fixed_cnt - 1)
                    p.lmmd.write(m, v, pack(L_LMMD, keep=0, decide=lr.decide, fix=lr.fix))
                    m.next = "MG_CWN"
                with m.Elif(ins):                    # merge_resolution_sort_part_2
                    p.rc.write(m, num_el, cw.lit)
                    m.d.sync += num_el.eq(num_el + 1)
                    with m.If(rh == 0):
                        m.d.sync += poss.eq(cw.lit)
                    p.scr.write(m, v, pack(L_SCR, st=st1, pos=num_el))
                    with m.If(mr.dec == level):
                        with m.If((highest < mr.ins) & ~once):
                            m.d.sync += highest.eq(mr.ins)
                        m.d.sync += stream.eq(stream + 1)
                    with m.If(lr.fix):
                        m.d.sync += fixed_cnt.eq(fixed_cnt + 1)
                    p.lmmd.write(m, v, pack(L_LMMD, keep=1, decide=lr.decide, fix=lr.fix))
                    with m.If(mr.dec > 0):
                        m.d.sync += bv.eq(v)
                        m.next = "BUMP"
                    with m.Else():
                        m.next = "MG_CWN"
                with m.Else():
                    m.next = "MG_CWN"
            with m.State("BUMP"):                    # pqHandler UPDATE (queued)
                with m.If(self.pq_ready):
                    PQ.call(m, self.pq, PQ.BUMP, bv)
                    m.next = "MG_CWN"
            with m.State("MG_END"):
                with m.If(rh == 1):
                    p.scr.read(m, var_of(poss))
                    m.next = "MG_E2"
                with m.Else():
                    m.next = "AFTER"
            with m.State("MG_E2"):
                p.scr.write(m, var_of(poss), pack(L_SCR, st=p.scr.rdata.st, pos=res_pos))
                m.next = "AFTER"
            with m.State("AFTER"):
                te = Mux(set_once, trail_end, highest)
                m.d.sync += [trail_end.eq(te), set_once.eq(1)]
                with m.If(num_el > self.stats["longest"]):
                    m.d.sync += self.stats["longest"].eq(num_el)
                with m.If(num_el > MAX_LEARN):
                    m.d.sync += self.error.eq(1)
                    m.next = "FIN"
                with m.Else():
                    with m.If((num_el == 1) | ((num_el - fixed_cnt) == 1)):
                        m.d.sync += found_abs.eq(1)
                    with m.If(stream == 1):
                        m.d.sync += is_uip.eq(1)
                        m.next = "ITER"
                    with m.Else():
                        m.d.sync += [fn_i.eq(te), fn_save.eq(te)]
                        m.next = "FN_RD"
            with m.State("FN_RD"):                   # findNextCls
                with m.If(fn_i < 0):
                    m.next = "FN_DONE"
                with m.Else():
                    p.stk.read(m, fn_i)
                    m.next = "FN_V"
            with m.State("FN_V"):
                v = var_of(p.stk.rdata)
                p.val.read(m, v >> 5)
                p.scr.read(m, v)
                m.d.sync += [fn_v.eq(v), trail_end.eq(trail_end - 1)]
                self._stat(m, "learn_merge")
                m.next = "FN_C"
            with m.State("FN_C"):
                vbit = p.val.rdata.bit_select(fn_v[:5], 1)
                s_ = p.scr.rdata.st
                with m.If(vbit & ((s_ == 1) | (s_ == 2))):
                    p.ubc.read(m, fn_v)
                    m.d.sync += fn_save.eq(trail_end)
                    m.next = "FN_U"
                with m.Else():
                    m.d.sync += fn_i.eq(fn_i - 1)
                    m.next = "FN_RD"
            with m.State("FN_U"):
                m.d.sync += next_c.eq(p.ubc.rdata)
                m.next = "FN_DONE"
            with m.State("FN_DONE"):
                m.d.sync += [trail_end.eq(fn_save), stream.eq(stream - 1)]
                m.next = "ITER"
            with m.State("DECAY"):                   # pqHandler EXIT: multiplier decay (queued)
                with m.If(self.pq_ready):
                    PQ.call(m, self.pq, PQ.DECAY)
                    m.next = "CLR"
            with m.State("CLR"):                     # ZERO_SEQ (clearIterations words)
                with m.If(vc_i == ((self.cfg.n_vars + 31) >> 5)):
                    m.d.sync += [bl_i.eq(0), level_before.eq(-1), self.uip.eq(0),
                                 self.ins0.eq(0), non_rem.eq(0), tomin_n.eq(0),
                                 mode_a.eq(~self.reset_all & ~found_abs)]
                    m.next = "BL_RD"
                with m.Else():
                    p.val.write(m, vc_i, 0)
                    m.d.sync += vc_i.eq(vc_i + 1)
            with m.State("BL_RD"):                   # GET_BT_LEVEL / FIND_ABSOLUTE / GET_MINIMIZE
                with m.If(bl_i == num_el):
                    p.send.read(m, Mux(mode_a, level_before, 0)[:LVL_W + 1])
                    m.next = "BL_T"
                with m.Else():
                    p.rc.read(m, bl_i)
                    m.next = "BL_V"
            with m.State("BL_V"):
                p.meta.read(m, var_of(p.rc.rdata))
                p.lmmd.read(m, var_of(p.rc.rdata))
                m.d.sync += bl_x.eq(p.rc.rdata)
                m.next = "BL_C"
            with m.State("BL_C"):
                mr, lr = p.meta.rdata, p.lmmd.rdata
                with m.If(found_abs):
                    with m.If(~lr.fix):
                        m.d.sync += self.ins0.eq(bl_x)
                with m.Else():
                    with m.If(mode_a):
                        with m.If(~lr.fix & (mr.dec != 0) & (level_before < mr.dec)
                                  & (mr.dec != level)):
                            m.d.sync += level_before.eq(mr.dec)
                        with m.If(mr.dec == level):
                            m.d.sync += self.uip.eq(bl_x)
                    with m.If(lr.decide & ~lr.fix):
                        m.d.sync += non_rem.eq(non_rem + 1)
                    with m.If(~(lr.fix | lr.decide)):
                        p.tomin.write(m, tomin_n, bl_x)
                        m.d.sync += tomin_n.eq(tomin_n + 1)
                m.d.sync += bl_i.eq(bl_i + 1)
                m.next = "BL_RD"
            with m.State("BL_T"):
                m.d.sync += self.bt_target.eq(p.send.rdata)
                m.next = "FIN"
        return m


class Backtracker(_Engine):
    """backtrack.cpp undoStates + updateStatesBackward: pop `count` trail
    literals, re-increment their clause states (walked ones only, via the
    pipelined _OccPipe), unhide them in the VSIDS heap and save their phase."""

    def __init__(self, mems, cfg, pq):
        super().__init__()
        self.cfg = cfg
        self.p = _Ports(mems, "bt", "stk", "occ", "ls", "cst", "meta")
        self.pipe = _OccPipe(self.p, cfg.P, "bt")
        self.pq = pq.client("bt")
        self.pq_ready = pq.ready
        self.i_height = Signal(LVL_W)
        self.count = Signal(LVL_W)
        self.commit = Signal(signed(LVL_W + 1))
        self.height = Signal(LVL_W)

    def elaborate(self, platform):
        m = Module()
        p, pipe, cfg = self.p, self.pipe, self.cfg
        height = self.height
        left = Signal(LVL_W)
        bL = Signal(signed(LIT_W))
        ud_v = Signal(VAR_W)

        def on_update(c1, st):                   # updateStatesBackward
            p.cst.write(m, c1 - 1, pack(
                L_CST, comp=(st.comp.as_unsigned() ^ (-bL)[:LIT_W])[:LIT_W].as_signed(),
                rem=st.rem + 1))
        pipe.elaborate_into(m, on_update)

        with m.FSM():
            with m.State("IDLE"):
                with m.If(self.start):
                    m.d.sync += [height.eq(self.i_height), left.eq(self.count)]
                    m.next = "TOP"
            with m.State("FIN"):
                m.d.comb += self.done.eq(1)
                m.next = "IDLE"
            with m.State("TOP"):
                with m.If(left == 0):
                    m.next = "FIN"
                with m.Else():
                    p.stk.read(m, height - 1)
                    m.d.sync += [height.eq(height - 1), left.eq(left - 1)]
                    m.next = "L"
            with m.State("L"):
                m.d.sync += [bL.eq(p.stk.rdata), ud_v.eq(var_of(p.stk.rdata))]
                with m.If(height <= self.commit):
                    m.d.comb += [pipe.e.eq(Cat(p.stk.rdata > 0, var_of(p.stk.rdata))),
                                 pipe.start.eq(1)]
                    m.next = "WALK"
                with m.Else():
                    m.next = "META"
            with m.State("WALK"):
                with m.If(pipe.done):
                    m.next = "UNHIDE"
            with m.State("UNHIDE"):                  # unhideElement (queued)
                with m.If(self.pq_ready):
                    PriorityQueue.call(m, self.pq, PriorityQueue.UNHIDE, ud_v)
                    m.next = "META"
            with m.State("META"):
                p.meta.read(m, ud_v)
                m.next = "MW"
            with m.State("MW"):
                mr = p.meta.rdata
                p.meta.write(m, ud_v, pack(L_META, ins=mr.ins, dec=mr.dec, stk=0,
                                           phase=Mux(bL > 0, cfg.pos_phase, ~cfg.pos_phase),
                                           ubl=mr.ubl, short=mr.short))
                m.next = "TOP"
        return m


class Minimizer(_Engine):
    """minimize.cpp: recursive resolution-based minimization of the learned
    clause; marks removable literals MIN_KEEP=2 and counts the kept ones."""

    STATS = ("min_iter", "min_merge")

    def __init__(self, mems, cfg):
        super().__init__()
        self.cfg = cfg
        self.p = _Ports(mems, "mn", "tomin", "lmmd", "ubc", "cmd", "cs", "scm", "vam", "mq")
        self.cw = _ClauseWalk(self.p, "mn")
        self.n = Signal(LRN_W)           # input: entries in tomin
        self.extra = Signal(CNT_W)       # output: non-removable literals found
        self.did = Signal()              # output: something was removed

    def elaborate(self, platform):  # noqa: C901
        m = Module()
        self._clear_stats(m)
        p, cw = self.p, self.cw
        j = Signal(LRN_W)
        g_v = Signal(VAR_W)
        g_mm = Signal(L_LMMD)
        next_c = Signal(CID_W)
        mq_h = Signal(LRN_W)
        mq_t = Signal(LRN_W)
        num_e = Signal(signed(CNT_W))
        cmk = Signal(CNT_W)
        hit = Signal()
        vc_i = Signal(VAR_W)
        with m.FSM():
            with m.State("IDLE"):
                with m.If(self.start):
                    m.d.sync += [j.eq(0), self.extra.eq(0), self.did.eq(0)]
                    m.next = "RD"
            with m.State("FIN"):
                m.d.comb += self.done.eq(1)
                m.next = "IDLE"
            with m.State("RD"):
                with m.If(j == self.n):
                    m.next = "FIN"
                with m.Else():
                    p.tomin.read(m, j)
                    m.next = "G"
            with m.State("G"):
                v = var_of(p.tomin.rdata)
                p.lmmd.read(m, v)
                p.ubc.read(m, v)
                m.d.sync += g_v.eq(v)
                m.next = "G2"
            with m.State("G2"):
                m.d.sync += [g_mm.eq(p.lmmd.rdata), next_c.eq(p.ubc.rdata)]
                with m.If(p.lmmd.rdata.fix | p.lmmd.rdata.decide):
                    m.d.sync += j.eq(j + 1)
                    m.next = "RD"
                with m.Else():
                    m.d.sync += [mq_h.eq(0), mq_t.eq(0), num_e.eq(0), cmk.eq(0)]
                    m.next = "ITER"
            with m.State("ITER"):
                self._stat(m, "min_iter")
                m.d.sync += [hit.eq(0), cw.c.eq(next_c - 1)]
                m.next = "MM_CW0"
            cw.states(m, "MM", "MM_A", "MM_END")
            with m.State("MM_A"):                    # minimize_resolution_sort
                u = var_of(cw.lit)
                p.scm.read(m, u)
                p.vam.read(m, u >> 5)
                p.lmmd.read(m, u)
                self._stat(m, "min_merge")
                m.next = "MM_B"
            with m.State("MM_B"):                    # ... + part_2
                u = var_of(cw.lit)
                vbit = p.vam.rdata.bit_select(u[:5], 1)
                st0 = Mux(vbit, p.scm.rdata, 0)
                p.vam.write(m, u >> 5, p.vam.rdata | (C(1, 32) << u[:5])[:32])
                bit = Mux(cw.lit > 0, 1, 2)
                ins = (st0 & bit) == 0
                st1 = (st0 | bit)[:2]
                lr = p.lmmd.rdata
                cmp_ = (lr.keep > 0) | lr.fix
                with m.If(st1 == 3):
                    m.d.sync += num_e.eq(num_e - 1)
                with m.Elif(ins):
                    m.d.sync += num_e.eq(num_e + 1)
                    p.scm.write(m, u, st1)
                    with m.If(~hit):
                        with m.If(cmp_):
                            m.d.sync += cmk.eq(cmk + 1)
                        with m.Else():
                            p.mq.write(m, mq_h, u)
                            m.d.sync += mq_h.eq(mq_h + 1)
                            with m.If(lr.decide):
                                m.d.sync += hit.eq(1)
                m.next = "MM_CWN"
            with m.State("MM_END"):
                with m.If(~hit & (cmk == num_e)):
                    p.lmmd.write(m, g_v, pack(L_LMMD, keep=2, decide=g_mm.decide, fix=g_mm.fix))
                    m.d.sync += [self.did.eq(1), vc_i.eq(0)]
                    m.next = "CLR"
                with m.Elif(hit | (mq_t >= mq_h)):
                    m.d.sync += [self.extra.eq(self.extra + 1), vc_i.eq(0)]
                    m.next = "CLR"
                with m.Else():
                    p.mq.read(m, mq_t)
                    m.d.sync += mq_t.eq(mq_t + 1)
                    m.next = "Q"
            with m.State("Q"):
                p.ubc.read(m, p.mq.rdata)
                m.next = "Q2"
            with m.State("Q2"):
                m.d.sync += next_c.eq(p.ubc.rdata)
                m.next = "ITER"
            with m.State("CLR"):                     # ZERO_SEQ_2 (clearIterations words)
                with m.If(vc_i == ((self.cfg.n_vars + 31) >> 5)):
                    m.d.sync += j.eq(j + 1)
                    m.next = "RD"
                with m.Else():
                    p.vam.write(m, vc_i, 0)
                    m.d.sync += vc_i.eq(vc_i + 1)
        return m


class ClauseSaver(_Engine):
    """clause_store_handler SAVE/saveData/BUCKET + learn.cpp writeClauseStream/
    saveClause + manage.cpp allocatePage (+ location_handler SAVE)."""

    STATS = ("simplified", "longest_simplified")

    def __init__(self, mems, cfg, fcp, fid, flp, buckets):
        super().__init__()
        self.cfg = cfg
        self.p = _Ports(mems, "sv", "rc", "meta", "lmmd", "occ", "ls", "cs", "cmd", "c2l",
                        "l2c", "cst")
        self.fcp, self.fid, self.flp, self.bk = fcp, fid, flp, buckets
        self.c_fcp = fcp.client("sv")
        self.c_fid = fid.client("sv")
        self.c_flp = flp.client("sv")
        self.c_bk = buckets.client("sv")
        # inputs
        self.num_el = Signal(CNT_W)
        self.non_rem = Signal(CNT_W)
        self.reset_all = Signal()
        self.uip = Signal(signed(LIT_W))
        self.did = Signal()
        # outputs
        self.error = Signal(signed(4))   # 0, -4 (no clause space), -5 (no literal page)
        self.cid = Signal(CLS_W)

    def elaborate(self, platform):  # noqa: C901
        m = Module()
        self._clear_stats(m)
        p, cfg, P = self.p, self.cfg, self.cfg.P
        fcp, fid, flp = self.fcp, self.fid, self.flp
        non_rem, cid = self.non_rem, self.cid
        addr = Signal(CA_W)
        i = Signal(CNT_W)
        sx = Signal(signed(LIT_W))
        sv = Signal(VAR_W)
        entry = Signal(VAR_W + 1)
        comp = Signal(signed(LIT_W))
        udl = [Signal(LVL_W, name=f"udl{k}") for k in range(LBD_BUCKETS + 1)]
        udl_n = Signal(4)
        sub = Signal(2)
        kept = Signal(CNT_W)
        o_start = Signal(LA_W)
        o_latest = Signal(LA_W)
        o_num = Signal(LA_W + 1)
        o_free = Signal(FREE_W)

        with m.FSM():
            with m.State("IDLE"):
                with m.If(self.start):
                    m.d.sync += self.error.eq(0)
                    m.next = "CHECK"
            with m.State("FIN"):
                m.d.comb += self.done.eq(1)
                m.next = "IDLE"
            with m.State("CHECK"):
                cls_size = ((CE_MAX - fcp.nxt) >> 2)[:CA_W + 1] + fcp.cnt
                with m.If((cls_size * (CLS_PAGE - 1) < non_rem) | fid.empty):
                    m.d.sync += self.error.eq(-4)
                    m.next = "FIN"
                with m.Else():
                    m.d.comb += self.c_fcp.pop.eq(1)
                    m.next = "A"
            with m.State("A"):
                with m.If(fcp.ack):
                    m.d.sync += addr.eq(fcp.val)
                    m.d.comb += self.c_fid.pop.eq(1)
                    m.next = "B"
            with m.State("B"):
                with m.If(fid.ack):
                    m.d.sync += [cid.eq(fid.val), comp.eq(0), udl_n.eq(0), sub.eq(0),
                                 kept.eq(0), i.eq(0)]
                    p.cmd.write(m, fid.val, pack(L_CMD, start=addr, num=non_rem))
                    m.next = "RD"
            with m.State("RD"):
                with m.If(i == self.num_el):
                    m.next = "END"
                with m.Else():
                    p.rc.read(m, i)
                    m.next = "V"
            with m.State("V"):
                v = var_of(p.rc.rdata)
                p.meta.read(m, v)
                p.lmmd.read(m, v)
                m.d.sync += [sx.eq(p.rc.rdata), sv.eq(v)]
                m.next = "K"
            with m.State("K"):
                mr, lr = p.meta.rdata, p.lmmd.rdata
                keep = ~lr.fix & (lr.keep == 1)
                p.lmmd.write(m, sv, pack(L_LMMD, keep=0, decide=lr.decide, fix=lr.fix))
                with m.If(keep):
                    m.d.sync += comp.eq(comp ^ sx)
                    found = Cat(*[(udl[k] == mr.dec) & (k < udl_n)
                                  for k in range(LBD_BUCKETS + 1)]).any()
                    with m.If(~found & (udl_n < LBD_BUCKETS + 1)):
                        with m.Switch(udl_n):
                            for k in range(LBD_BUCKETS + 1):
                                with m.Case(k):
                                    m.d.sync += udl[k].eq(mr.dec)
                        m.d.sync += udl_n.eq(udl_n + 1)
                    e = Cat(sx < 0, sv)
                    p.occ.read(m, e)
                    m.d.sync += entry.eq(e)
                    m.next = "O"
                with m.Else():
                    m.d.sync += i.eq(i + 1)
                    m.next = "RD"
            with m.State("O"):                       # saveClause + saveData + location SAVE
                o = p.occ.rdata
                la = (o.latest + P - o.free - 2)[:LA_W]
                ca = (addr + sub)[:CA_W]
                p.ls.write(m, la, cid + 1)
                p.occ.write(m, entry, pack(L_OCC, start=o.start, latest=o.latest,
                                           num=o.num + 1, free=o.free - 1))
                p.cs.write(m, ca, fit(sx, CS_W))
                p.c2l.write(m, ca, la)
                p.l2c.write(m, la, ca)
                m.d.sync += [o_start.eq(o.start), o_latest.eq(o.latest), o_num.eq(o.num + 1),
                             o_free.eq(o.free - 1), kept.eq(kept + 1)]
                with m.If(sub == CLS_PAGE - 2):
                    m.d.sync += sub.eq(0)
                    with m.If(kept + 1 != non_rem):
                        m.d.comb += self.c_fcp.pop.eq(1)
                        m.next = "NP"
                    with m.Else():
                        m.next = "Z"
                with m.Else():
                    m.d.sync += sub.eq(sub + 1)
                    m.next = "AC"
            with m.State("NP"):
                with m.If(fcp.ack):
                    p.cs.write(m, addr + CLS_PAGE - 1, fcp.val)
                    m.d.sync += addr.eq(fcp.val)
                    m.next = "AC"
            with m.State("Z"):
                p.cs.write(m, addr + CLS_PAGE - 1, 0)
                m.next = "AC"
            with m.State("AC"):                      # allocatePage when the page filled
                with m.If(o_free == 0):
                    with m.If(flp.empty):
                        m.d.sync += self.error.eq(-5)
                        m.next = "FIN"
                    with m.Else():
                        m.d.comb += self.c_flp.pop.eq(1)
                        m.next = "AL0"
                with m.Else():
                    m.d.sync += i.eq(i + 1)
                    m.next = "RD"
            with m.State("AL0"):
                with m.If(flp.ack):
                    p.ls.write(m, o_latest + P - 1, flp.val)
                    m.next = "AL1"
            with m.State("AL1"):
                p.ls.write(m, flp.val + P - 2, o_latest)
                p.occ.write(m, entry, pack(L_OCC, start=o_start, latest=flp.val, num=o_num,
                                           free=P - 2))
                m.d.sync += i.eq(i + 1)
                m.next = "RD"
            with m.State("END"):                     # csh BUCKET + new clause state
                b = Mux(udl_n < 2, LBD_BUCKETS - 1, udl_n - 2)[:4]
                m.d.comb += [self.c_bk.append.eq(1), self.c_bk.append_b.eq(b),
                             self.c_bk.append_id.eq(cid)]
                p.cst.write(m, cid, Mux(self.reset_all, pack(L_CST, comp=comp, rem=non_rem),
                                        pack(L_CST, comp=self.uip, rem=1)))
                with m.If(self.did):
                    self._stat(m, "simplified")
                    with m.If(non_rem > self.stats["longest_simplified"]):
                        m.d.sync += self.stats["longest_simplified"].eq(non_rem)
                m.next = "FIN"
        return m


class Pruner(_Engine):
    """clause_store_handler DELETE (getDeletedClsID, deleteClauses) + manage.cpp
    deleteTransposedClauses + location_handler SEND/UPDATE: on a restart, delete
    the prune fraction of learned clauses, highest LBD bucket and oldest first."""

    STATS = ("deleted",)

    def __init__(self, mems, cfg, fcp, fid, flp, buckets):
        super().__init__()
        self.cfg = cfg
        self.p = _Ports(mems, "pr", "cmd", "cs", "c2l", "occ", "ls", "l2c")
        self.cw = _ClauseWalk(self.p, "pr")
        self.bk = buckets
        self.c_fcp = fcp.client("pr")
        self.c_fid = fid.client("pr")
        self.c_flp = flp.client("pr")
        self.c_bk = buckets.client("pr")
        self.last_ins = Signal(CLS_W + 1)    # input: the clause just learned (exempt)

    def elaborate(self, platform):  # noqa: C901
        m = Module()
        self._clear_stats(m)
        p, cw, bk, P = self.p, self.cw, self.bk, self.cfg.P
        remove_total = Signal(CLS_W + 1)
        removed = Signal(CLS_W + 1)
        pb = Signal(signed(5))
        d_r = Signal(CLS_W)
        entry = Signal(VAR_W + 1)
        la = Signal(LA_W)
        latest = Signal(LA_W)
        free = Signal(FREE_W)
        swap = Signal(LA_W)
        moved = Signal(LS_W)
        ca = Signal(CA_W)
        o_start = Signal(LA_W)
        o_num = Signal(LA_W + 1)
        m.d.comb += bk.q.eq(pb[:4])

        with m.FSM():
            with m.State("IDLE"):
                with m.If(self.start):
                    rt = ((bk.used_total * self.cfg.prune_q16) >> 16)[:CLS_W + 1]
                    m.d.sync += [remove_total.eq(rt), pb.eq(LBD_BUCKETS - 1), removed.eq(0)]
                    m.d.comb += [self.c_bk.sub_used.eq(1), self.c_bk.sub_n.eq(rt)]
                    m.next = "SEL"
            with m.State("FIN"):
                m.d.comb += self.done.eq(1)
                m.next = "IDLE"
            with m.State("SEL"):                     # getDeletedClsID
                with m.If((removed == remove_total) | (pb < 0)):
                    m.next = "FIN"
                with m.Elif((bk.q_cnt == 0) | (bk.q_head == self.last_ins)):
                    m.d.sync += pb.eq(pb - 1)
                with m.Else():
                    m.d.sync += d_r.eq(bk.q_head)
                    m.d.comb += self.c_bk.pop_req.eq(1)
                    m.next = "POP"
            with m.State("POP"):
                m.d.comb += self.c_bk.pop_commit.eq(1)
                m.d.sync += [removed.eq(removed + 1), cw.c.eq(d_r)]
                self._stat(m, "deleted")
                m.d.comb += [self.c_fid.push.eq(1), self.c_fid.push_v.eq(d_r)]
                m.next = "DL_CW0"
            cw.states(m, "DL", "DX0", "SEL")
            with m.State("DX0"):                     # deleteClauses
                with m.If(cw.cursub == 0):
                    m.d.comb += [self.c_fcp.push.eq(1), self.c_fcp.push_v.eq(cw.cur)]
                p.c2l.read(m, cw.cur)
                e = Cat(cw.lit < 0, var_of(cw.lit))
                p.occ.read(m, e)
                m.d.sync += entry.eq(e)
                m.next = "DX1"
            with m.State("DX1"):                     # deleteTransposedClauses
                o = p.occ.rdata
                m.d.sync += [la.eq(p.c2l.rdata), o_start.eq(o.start), o_num.eq(o.num)]
                with m.If(o.free == P - 2):
                    m.d.comb += [self.c_flp.push.eq(1), self.c_flp.push_v.eq(o.latest)]
                    p.ls.read(m, o.latest + P - 2)
                    m.next = "DX2"
                with m.Else():
                    m.d.sync += [latest.eq(o.latest), free.eq(o.free + 1),
                                 swap.eq(o.latest + P - o.free - 3)]
                    m.next = "DX3"
            with m.State("DX2"):
                m.d.sync += [latest.eq(p.ls.rdata), free.eq(1), swap.eq(p.ls.rdata + P - 3)]
                m.next = "DX3"
            with m.State("DX3"):
                p.ls.read(m, swap)
                p.l2c.read(m, swap)
                m.next = "DX4"
            with m.State("DX4"):
                m.d.sync += [moved.eq(p.ls.rdata), ca.eq(p.l2c.rdata)]
                p.ls.write(m, la, p.ls.rdata)
                m.next = "DX5"
            with m.State("DX5"):                     # location_handler UPDATE
                p.ls.write(m, swap, 0)
                p.occ.write(m, entry, pack(L_OCC, start=o_start, latest=latest,
                                           num=o_num - 1, free=free))
                with m.If(moved - 1 != d_r):
                    p.l2c.write(m, la, ca)
                    p.c2l.write(m, ca, la)
                m.next = "DL_CWN"
        return m


# ==========================================================================
# Host interface (message.cpp / the XRT host link) and the top level.
# ==========================================================================

class Config:
    """Host-written solver configuration (the HLS miscCounters / kernel args)."""

    def __init__(self):
        self.n_vars = Signal(LVL_W)
        self.n_cls = Signal(CID_W)
        self.lit_elems = Signal(LA_W + 1)
        self.cls_elems = Signal(CA_W + 1)
        self.fixed_init = Signal(LVL_W)
        self.pos_phase = Signal()
        self.P = Signal(7, init=8)
        self.reset_mult = Signal(32, init=100)
        self.prune_q16 = Signal(17, init=6553)
        self.inv_decay = Signal(SCORE_W, init=0x7F0D79)


class HostInterface(Elaboratable):
    """Wishbone register file: configuration, memory load/readback port,
    status, statistics and per-phase cycle counters."""

    def __init__(self, bus, cfg, mems, status):
        self.bus = bus                   # the top's wb_* signals
        self.cfg = cfg
        self.status = status             # dict of readable values
        self.p = _Ports(mems, "host", "ls", "cs", "cmd", "cst", "occ", "stk")
        self.idle = Signal()             # input: solver idle (memory access allowed)
        self.go = Signal()               # output: CTRL.start written

    def elaborate(self, platform):  # noqa: C901
        m = Module()
        b, cfg, p, st = self.bus, self.cfg, self.p, self.status
        ack = b.wb_ack
        stb = b.wb_cyc & b.wb_stb & ~ack
        wb_w = stb & b.wb_we
        wb_r = stb & ~b.wb_we
        adr, dw = b.wb_adr, b.wb_dat_w
        mem_sel = Signal(4)
        mem_ptr = Signal(18)
        occ_stage = Signal(32)
        rd_odd = Signal()
        is_mem = (adr == R_MEMDATA) | adr[8]

        with m.If(stb):
            m.d.sync += ack.eq(1)
        with m.Else():
            m.d.sync += ack.eq(0)

        with m.If(self.idle):
            with m.If(wb_w & is_mem):
                with m.Switch(mem_sel):
                    with m.Case(M_LS):
                        p.ls.write(m, mem_ptr, dw)
                    with m.Case(M_CS):
                        p.cs.write(m, mem_ptr, dw)
                    with m.Case(M_CMD):
                        p.cmd.write(m, mem_ptr, pack(L_CMD, start=dw[:16], num=dw[16:]))
                    with m.Case(M_CST):
                        p.cst.write(m, mem_ptr, pack(L_CST, comp=dw[:16].as_signed(), rem=dw[16:]))
                    with m.Case(M_OCC):
                        with m.If(mem_ptr[0] == 0):
                            m.d.sync += occ_stage.eq(dw)
                        with m.Else():
                            p.occ.write(m, mem_ptr >> 1, pack(
                                L_OCC, start=occ_stage[:16], latest=occ_stage[16:],
                                num=dw[:16], free=dw[16:24]))
                    with m.Case(M_STK):
                        p.stk.write(m, mem_ptr, dw.as_signed())
            with m.If(wb_r & is_mem):
                with m.Switch(mem_sel):
                    for sel, port in [(M_LS, p.ls), (M_CS, p.cs), (M_CMD, p.cmd),
                                      (M_CST, p.cst), (M_STK, p.stk)]:
                        with m.Case(sel):
                            port.read(m, mem_ptr)
                    with m.Case(M_OCC):
                        p.occ.read(m, mem_ptr >> 1)
            with m.If(wb_w):
                with m.Switch(adr):
                    for a, s in [(R_NVARS, cfg.n_vars), (R_NCLS, cfg.n_cls),
                                 (R_LITELEMS, cfg.lit_elems), (R_CLSELEMS, cfg.cls_elems),
                                 (R_FIXED, cfg.fixed_init), (R_LITPAGE, cfg.P),
                                 (R_RESETMULT, cfg.reset_mult), (R_PRUNE, cfg.prune_q16),
                                 (R_INVDECAY, cfg.inv_decay), (R_MEMSEL, mem_sel),
                                 (R_MEMPTR, mem_ptr)]:
                        with m.Case(a):
                            m.d.sync += s.eq(dw)
                    with m.Case(R_POSPHASE):
                        m.d.sync += cfg.pos_phase.eq(dw[0])
                    with m.Case(R_CTRL):
                        m.d.comb += self.go.eq(dw[0])
            with m.If((wb_w | wb_r) & is_mem):
                m.d.sync += [mem_ptr.eq(mem_ptr + 1), rd_odd.eq(mem_ptr[0])]

        caps = [N_MAX, C_MAX, LE_MAX, CE_MAX, MAX_LEARN, FRAC_W, MAGIC, VERSION]
        dr = b.wb_dat_r
        with m.Switch(adr):
            with m.Case(R_CTRL):
                m.d.comb += dr.eq(Cat(~self.idle, st["done"]))
            with m.Case(R_RESULT):
                m.d.comb += dr.eq(st["result"])
            for a, s in [(R_NVARS, cfg.n_vars), (R_NCLS, cfg.n_cls), (R_LITELEMS, cfg.lit_elems),
                         (R_CLSELEMS, cfg.cls_elems), (R_FIXED, cfg.fixed_init),
                         (R_POSPHASE, cfg.pos_phase), (R_LITPAGE, cfg.P),
                         (R_RESETMULT, cfg.reset_mult), (R_PRUNE, cfg.prune_q16),
                         (R_INVDECAY, cfg.inv_decay), (R_MEMSEL, mem_sel), (R_MEMPTR, mem_ptr)]:
                with m.Case(a):
                    m.d.comb += dr.eq(s)
            with m.Case(R_MEMDATA, "1--------"):
                with m.Switch(mem_sel):
                    with m.Case(M_LS):
                        m.d.comb += dr.eq(p.ls.rdata)
                    with m.Case(M_CS):
                        m.d.comb += dr.eq(p.cs.rdata)
                    with m.Case(M_CMD):
                        m.d.comb += dr.eq(Cat(fit(p.cmd.rdata.start, 16), p.cmd.rdata.num))
                    with m.Case(M_CST):
                        m.d.comb += dr.eq(Cat(fit(p.cst.rdata.comp, 16), p.cst.rdata.rem))
                    with m.Case(M_OCC):
                        o = p.occ.rdata
                        m.d.comb += dr.eq(Mux(rd_odd, Cat(fit(o.num, 16), fit(o.free, 16)),
                                              Cat(fit(o.start, 16), fit(o.latest, 16))))
                    with m.Case(M_STK):
                        m.d.comb += dr.eq(fit(p.stk.rdata, 32))
            for i, c in enumerate(caps):
                with m.Case(R_CAPS + i):
                    m.d.comb += dr.eq(c)
            for i, n in enumerate(STAT_NAMES):
                if n in st:
                    with m.Case(R_STATS + i):
                        m.d.comb += dr.eq(st[n])
            for k in range(N_PHASES):
                with m.Case(R_CYCLES + 2 * k):
                    m.d.comb += dr.eq(st["cyc"][k][:32])
                with m.Case(R_CYCLES + 2 * k + 1):
                    m.d.comb += dr.eq(st["cyc"][k][32:])
        return m


class SATAccel(Elaboratable):
    """CDCL SAT solver behind the Cloud-FPGA Wishbone B4 slave contract.

    The top level is solver.cpp's SOLVE_ITERATION loop: it owns the trail
    height, decision level and fixed-decision count, and schedules the
    kernels (Propagator, Learner, Backtracker, Minimizer, ClauseSaver, Pruner)
    and services (PriorityQueue, FreeLists, LbdBuckets, Restart)."""

    def __init__(self):
        self.wb_cyc = Signal()
        self.wb_stb = Signal()
        self.wb_we = Signal()
        self.wb_adr = Signal(9)
        self.wb_dat_w = Signal(32)
        self.wb_sel = Signal(4)
        self.wb_dat_r = Signal(32)
        self.wb_ack = Signal()

    def elaborate(self, platform):  # noqa: C901
        m = Module()
        cfg = Config()
        mems = Memories()

        # ------------------------------------------------ services --------
        pq = PriorityQueue(cfg.n_vars, cfg.inv_decay)
        flp = FreeList("free_lit_pages", LA_W, LE_MAX // 4, LE_MAX, cfg.P)
        fcp = FreeList("free_cls_pages", CA_W, CE_MAX // 4, CE_MAX, CLS_PAGE)
        fid = FreeList("free_cls_id", CLS_W, C_MAX, C_MAX, 1)
        bk = LbdBuckets()
        rst = Restart(cfg.reset_mult)

        # ------------------------------------------------ kernels ---------
        prop = Propagator(mems, cfg)
        learn = Learner(mems, cfg, pq)
        back = Backtracker(mems, cfg, pq)
        mini = Minimizer(mems, cfg)
        save = ClauseSaver(mems, cfg, fcp, fid, flp, bk)
        prune = Pruner(mems, cfg, fcp, fid, flp, bk)
        engines = [prop, learn, back, mini, save, prune]

        tp = _Ports(mems, "top", "meta", "lmmd", "val", "vam", "send", "tomin", "stk")
        tpq = pq.client("top")

        # ------------------------------------------------ solver state ----
        height = Signal(LVL_W)
        fixed = Signal(LVL_W)
        level = Signal(LVL_W)
        do_bt = Signal()
        use_flipped = Signal()
        flipped = Signal(signed(LIT_W))
        commit = Signal(signed(LVL_W + 1))
        top_var = Signal(VAR_W)
        reset_all = Signal()
        found_abs = Signal()
        back_busy = Signal()
        mini_busy = Signal()
        non_rem = Signal(CNT_W)
        did = Signal()
        level_before = Signal(signed(LVL_W + 1))
        uip = Signal(signed(LIT_W))
        ins0 = Signal(signed(LIT_W))
        given = Signal(CLS_W)
        last_ins = Signal(CLS_W + 1)
        done = Signal()
        result = Signal(signed(32))
        init_i = Signal(LVL_W + 1)
        ft_inc = Signal(LVL_W)
        ft_cnt = Signal(LRN_W)
        ft_found = Signal()
        hide_i = Signal(LRN_W)
        phase = Signal(4)
        cyc = [Signal(64, name=f"cyc{k}") for k in range(N_PHASES)]
        cycles = Signal(64)
        tstat = {n: Signal(32, name=f"stat_{n}") for n in ("total", "decide", "retry",
                                                          "backtrack", "reset")}

        def inc(name):
            m.d.sync += tstat[name].eq(tstat[name] + 1)

        # kernel inputs follow the solver state
        m.d.comb += [
            prop.i_height.eq(height), prop.i_fixed.eq(fixed), prop.level.eq(level),
            prop.use_flipped.eq(use_flipped), prop.flipped.eq(flipped),
            prop.top_var.eq(top_var), prop.given.eq(given),
            learn.nunsat.eq(prop.nunsat), learn.unsat0.eq(prop.unsat0),
            learn.unsat1.eq(prop.unsat1), learn.level.eq(level), learn.reset_all.eq(reset_all),
            back.i_height.eq(height), back.count.eq(height - learn.bt_target),
            back.commit.eq(commit),
            mini.n.eq(learn.tomin_n),
            save.num_el.eq(learn.num_el), save.non_rem.eq(non_rem),
            save.reset_all.eq(reset_all), save.uip.eq(uip), save.did.eq(did),
            prune.last_ins.eq(last_ins),
            flp.start.eq(cfg.lit_elems), fcp.start.eq(cfg.cls_elems), fid.start.eq(cfg.n_cls),
        ]

        host_status = {"done": done, "result": result, "height": height, "level": level,
                       "fixed": fixed, "cycles_lo": cycles[:32], "cycles_hi": cycles[32:],
                       "cyc": cyc, **tstat}
        for e in engines:
            host_status.update(e.stats)
        host = HostInterface(self, cfg, mems, host_status)

        with m.FSM(name="solver") as fsm:
            m.d.comb += host.idle.eq(fsm.ongoing("IDLE"))

            with m.State("IDLE"):
                with m.If(host.go):
                    m.d.sync += [done.eq(0), result.eq(0), init_i.eq(0), phase.eq(PH_INIT),
                                 cycles.eq(0), *[c.eq(0) for c in cyc]]
                    m.next = "INIT"

            # ===================================== INIT (copy_in) =========
            with m.State("INIT"):
                with m.If(init_i == N_MAX):
                    PriorityQueue.call(m, tpq, PriorityQueue.INIT)
                    m.d.comb += [flp.reset.eq(1), fcp.reset.eq(1), fid.reset.eq(1),
                                 bk.reset.eq(1), rst.reset.eq(1),
                                 *[e.clear.eq(1) for e in engines]]
                    m.d.sync += [height.eq(0), fixed.eq(cfg.fixed_init),
                                 level.eq(Mux(cfg.fixed_init == 0, 1, 0)),
                                 do_bt.eq(0), use_flipped.eq(0), commit.eq(-1),
                                 last_ins.eq(C_MAX), *[s.eq(0) for s in tstat.values()]]
                    m.next = "INIT_PQ"
                with m.Else():
                    tp.lmmd.write(m, init_i, 0)
                    tp.meta.write(m, init_i, pack(L_META, ins=0, dec=0, stk=0,
                                                  phase=~cfg.pos_phase, ubl=0, short=0))
                    tp.send.write(m, init_i, 0)
                    with m.If(init_i < N_MAX // 32):
                        tp.val.write(m, init_i, 0)
                        tp.vam.write(m, init_i, 0)
                    m.d.sync += init_i.eq(init_i + 1)
            with m.State("INIT_PQ"):
                m.next = "INIT_PQ_W"
            with m.State("INIT_PQ_W"):
                with m.If(pq.idle):
                    m.next = "MAIN"

            # ================================ SOLVE_ITERATION ============
            with m.State("MAIN"):
                inc("total")
                with m.If(~do_bt):
                    with m.If(use_flipped):
                        inc("retry")
                    with m.Else():
                        inc("decide")
                    with m.If(~use_flipped & (level != 0)):
                        m.d.sync += [ft_inc.eq(0), ft_cnt.eq(0), phase.eq(PH_FIND)]
                        m.next = "FT_PEEK"
                    with m.Else():
                        m.next = "BCP"
                with m.Else():
                    inc("backtrack")
                    m.d.comb += rst.tick.eq(1)
                    m.d.sync += phase.eq(PH_LEARN)
                    m.next = "RST"

            # FIND_TOP: pqHandler GET_UNDECIDED (scan heap slots in index order)
            with m.State("FT_PEEK"):
                with m.If(pq.ready):
                    PriorityQueue.call(m, tpq, PriorityQueue.PEEK, ft_inc)
                    m.next = "FT_W"
            with m.State("FT_W"):
                with m.If(pq.result_valid):
                    tp.meta.read(m, pq.result)
                    tp.tomin.write(m, ft_cnt, pq.result)
                    m.d.sync += [top_var.eq(pq.result), ft_cnt.eq(ft_cnt + 1),
                                 ft_inc.eq(ft_inc + 1)]
                    m.next = "FT_C"
            with m.State("FT_C"):
                with m.If(~tp.meta.rdata.stk):
                    m.d.sync += [ft_found.eq(1), hide_i.eq(0)]
                    m.next = "HD_0"
                with m.Elif(ft_cnt == SCAN_BATCH):
                    m.d.sync += [ft_found.eq(0), hide_i.eq(0)]
                    m.next = "HD_0"
                with m.Else():
                    m.next = "FT_PEEK"
            with m.State("HD_0"):
                with m.If(hide_i == ft_cnt):
                    with m.If(ft_found):
                        m.next = "BCP"
                    with m.Else():
                        m.d.sync += [ft_inc.eq(0), ft_cnt.eq(0)]
                        m.next = "FT_PEEK"
                with m.Else():
                    tp.tomin.read(m, hide_i)
                    m.next = "HD_1"
            with m.State("HD_1"):                    # hideElement (queued)
                with m.If(pq.ready):
                    PriorityQueue.call(m, tpq, PriorityQueue.HIDE, tp.tomin.rdata[:VAR_W])
                    m.d.sync += hide_i.eq(hide_i + 1)
                    m.next = "HD_0"
                with m.Else():
                    tp.tomin.read(m, hide_i)         # hold the entry while the queue is full

            # discover / propagate
            with m.State("BCP"):
                m.d.comb += prop.start.eq(1)
                m.d.sync += phase.eq(PH_BCP)
                m.next = "BCP_W"
            with m.State("BCP_W"):
                with m.If(prop.done):
                    new_level = Mux(prop.conflict, level, level + 1)[:LVL_W]
                    tp.send.write(m, level, prop.height)
                    m.d.sync += [height.eq(prop.height), fixed.eq(prop.fixed),
                                 commit.eq(prop.commit), do_bt.eq(prop.conflict),
                                 level.eq(new_level), use_flipped.eq(0)]
                    with m.If(new_level == 0):
                        m.d.sync += result.eq(0)
                        m.next = "FINISH"
                    with m.Elif((prop.height == cfg.n_vars) & ~prop.conflict):
                        m.d.sync += result.eq(1)
                        m.next = "FINISH"
                    with m.Else():
                        m.next = "MAIN"

            # learnClause: resolution -> undo (backtrack) -> minimize -> save
            with m.State("RST"):
                m.d.sync += reset_all.eq(rst.fire)
                with m.If(rst.fire):
                    inc("reset")
                m.next = "LEARN"
            with m.State("LEARN"):
                m.d.comb += learn.start.eq(1)
                m.next = "LEARN_W"
            with m.State("LEARN_W"):
                with m.If(learn.done):
                    with m.If(learn.error):
                        m.d.sync += result.eq(-2)
                        m.next = "FINISH"
                    with m.Else():
                        m.d.sync += [found_abs.eq(learn.found_abs), non_rem.eq(learn.non_rem),
                                     level_before.eq(learn.level_before), uip.eq(learn.uip),
                                     ins0.eq(learn.ins0), phase.eq(PH_MIN),
                                     back_busy.eq(1), mini_busy.eq(~learn.found_abs)]
                        # undo_states_and_minimize_task_parallel_wrapper: the two
                        # kernels touch disjoint memories, so they run together.
                        m.d.comb += [back.start.eq(1), mini.start.eq(~learn.found_abs)]
                        m.next = "BACK_MIN_W"
            with m.State("BACK_MIN_W"):
                with m.If(back.done):
                    m.d.sync += [height.eq(back.height), back_busy.eq(0)]
                with m.If(mini.done):
                    m.d.sync += [non_rem.eq(non_rem + mini.extra), did.eq(mini.did),
                                 mini_busy.eq(0)]
                with m.If(~back_busy & ~mini_busy):
                    with m.If(found_abs):
                        m.d.sync += level.eq(0)
                        m.next = "LEARN_DONE"
                    with m.Else():
                        m.d.sync += phase.eq(PH_SAVE)
                        m.next = "SAVE"
            with m.State("SAVE"):
                m.d.comb += save.start.eq(1)
                m.next = "SAVE_W"
            with m.State("SAVE_W"):
                with m.If(save.done):
                    with m.If(save.error != 0):
                        m.d.sync += result.eq(save.error)
                        m.next = "FINISH"
                    with m.Else():
                        m.d.sync += [given.eq(save.cid), last_ins.eq(save.cid),
                                     level.eq(level_before)]
                        m.next = "LEARN_DONE"
            with m.State("LEARN_DONE"):
                with m.If(reset_all):
                    m.d.sync += [level.eq(0), phase.eq(PH_DELETE)]
                    m.d.comb += prune.start.eq(1)
                    m.next = "PRUNE_W"
                with m.Else():
                    m.next = "POST"
            with m.State("PRUNE_W"):
                with m.If(prune.done):
                    m.next = "POST"

            # solver.cpp tail: fixed literal / flipped UIP for the next iteration
            with m.State("POST"):
                m.d.sync += do_bt.eq(0)
                with m.If(level == 0):
                    nf = Mux(ins0 != 0, fixed + 1, fixed)[:LVL_W]
                    with m.If(ins0 != 0):
                        tp.stk.write(m, height, ins0)
                    m.d.sync += [fixed.eq(nf), use_flipped.eq(0),
                                 level.eq(Mux((ins0 == 0) | (nf == 0), 1, 0))]
                with m.Else():
                    m.d.sync += [use_flipped.eq(1), flipped.eq(uip)]
                m.next = "MAIN"

            with m.State("FINISH"):
                m.d.sync += done.eq(1)
                m.next = "IDLE"

            busy = ~fsm.ongoing("IDLE")

        # ------------------------------------------------ timer.cpp ------
        with m.If(busy):
            m.d.sync += cycles.eq(cycles + 1)
            with m.Switch(phase):
                for k in range(N_PHASES):
                    with m.Case(k):
                        m.d.sync += cyc[k].eq(cyc[k] + 1)

        # ------------------------------------------------ wiring ---------
        mems.attach(m)
        for name, sub in [("host", host), ("pq", pq), ("free_lit_pages", flp),
                          ("free_cls_pages", fcp), ("free_cls_id", fid), ("lbd_buckets", bk),
                          ("restart", rst), ("propagator", prop), ("learner", learn),
                          ("backtracker", back), ("minimizer", mini), ("saver", save),
                          ("pruner", prune)]:
            m.submodules[name] = sub

        # simulation-only handles (unused by the bitstream)
        self._dbg = dict(height=height, level=level, do_bt=do_bt, use_flipped=use_flipped,
                         flipped=flipped, fixed=fixed, total=tstat["total"], top_var=top_var,
                         commit=commit, remaining=pq.remaining)
        self._mem = {n: getattr(mems, n).mem for n, _, _ in Memories.SPEC}
        return m
