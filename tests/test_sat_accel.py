"""pytest -q tests/   (RTL cases simulate the full design cycle-accurately)"""

import os
import sys

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

from satlat import golden as G  # noqa: E402
from satlat import host as H  # noqa: E402
from satlat import rtlsim  # noqa: E402

T = os.path.join(ROOT, "SAT_test_cases")

GOLDEN_CASES = [
    ("sat/aalto.dimacs", 1), ("sat/aim50.dimacs", 1), ("sat/nqueens_8.dimacs", 1),
    ("sat/sudoku_6_2_3.dimacs", 1), ("sat/uf20.dimacs", 1),
    ("unsat/4_4_1.txt", 0), ("unsat/aim100.dimacs", 0), ("unsat/dubois20.dimacs", 0),
    ("unsat/pret150_75.dimacs", 0), ("unsat/ssa0432-003.dimacs", 0),
]
RTL_CASES = ["sat/aalto.dimacs", "sat/quinn.dimacs", "sat/uf20.dimacs",
             "sat/nqueens_4.dimacs", "unsat/aalto_2.dimacs", "unsat/nqueens_3.dimacs"]


def test_float_ops():
    one = G.ONE
    assert H.fp_decode(G.fp_add(one, one)) == 2.0
    assert abs(H.fp_decode(G.fp_mul(H.fp_encode(1.5), H.fp_encode(3.0))) - 4.5) < 1e-4
    assert G.fp_rescale(H.fp_encode(2.0 ** 101)) == H.fp_encode(2.0)


def test_luby_sequence():
    g = G.Golden(H.build_images(1, [[1]]))
    assert [g._luby() for _ in range(15)] == [1, 2, 1, 1, 2, 4, 1, 1, 2, 1, 1, 2, 4, 8, 1]


@pytest.mark.parametrize("case,expect", GOLDEN_CASES)
def test_golden(case, expect):
    img = H.load_cnf(os.path.join(T, case))
    res, g = G.solve(img, 2_000_000)
    assert res == expect
    if res == 1:
        assert not H.check_model(img.clauses, g.model())


@pytest.mark.parametrize("case", RTL_CASES)
def test_rtl_matches_golden(case):
    assert rtlsim.check(os.path.join(T, case), verbose=False)


def test_rtl_back_to_back_solves():
    """BRAM keeps the previous solve's contents on hardware; every solve must still match golden."""
    seq = ["sat/nqueens_8.dimacs", "sat/aim50.dimacs", "unsat/aalto_2.dimacs", "sat/uf20.dimacs"]
    assert rtlsim.check_seq([os.path.join(T, c) for c in seq], verbose=False)


# ------------------------------------------------------------ heuristics --
# Settings that fire every heuristic far more often than real use would.
STRESS = [
    dict(low_cls_pages=4000, glue_buckets=1, used_bit=True, min_abstract=True, rephase=10),
    dict(low_lit_pages=2000, glue_buckets=3, used_bit=True, min_abstract=True, rephase=25),
]
SEARCH = ("total", "decide", "retry", "backtrack", "reset", "learn_iter", "learn_merge",
          "simplified", "longest_simplified", "check_cnt", "deleted")


@pytest.mark.parametrize("case,expect", GOLDEN_CASES)
def test_min_abstract_keeps_the_search(case, expect):
    """The level filter and early stop only skip work whose outcome is already a failure."""
    _, g0 = G.solve(H.load_cnf(os.path.join(T, case)), 2_000_000)
    _, g1 = G.solve(H.load_cnf(os.path.join(T, case), H.Config(min_abstract=True)), 2_000_000)
    for n in SEARCH:
        assert getattr(g0.stats, n) == getattr(g1.stats, n), n
    assert g1.stats.min_merge <= g0.stats.min_merge


@pytest.mark.parametrize("case,expect", GOLDEN_CASES + [("../benchmarks/satlib/table3/hole7.cnf", 0)])
def test_golden_heuristics(case, expect):
    img = H.load_cnf(os.path.join(T, case), H.pigweed(H.Config(lit_page=16)))
    res, g = G.solve(img, 2_000_000)
    assert res == expect
    if res == 1:
        assert not H.check_model(img.clauses, g.model())


@pytest.mark.parametrize("case,stress", [("unsat/4_4_1.txt", 0), ("unsat/4_4_1.txt", 1),
                                         ("sat/aim50.dimacs", 0), ("sat/aim50.dimacs", 1)])
def test_rtl_heuristics_match_golden(case, stress):
    assert rtlsim.check(os.path.join(T, case), H.Config(**STRESS[stress]), verbose=False)


def test_rtl_heuristics_back_to_back():
    seq = ["sat/nqueens_8.dimacs", "unsat/aim100.dimacs", "sat/aim50.dimacs", "sat/uf20.dimacs"]
    assert rtlsim.check_seq([os.path.join(T, c) for c in seq], H.Config(**STRESS[0]), verbose=False)
