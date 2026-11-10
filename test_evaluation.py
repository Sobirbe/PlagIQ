"""Small run of the ground-truth evaluation; guards matcher accuracy in CI."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / 'evaluation'))
import run_eval  # noqa: E402


def test_ground_truth_properties():
    rows, parity, fp = run_eval.evaluate(trials=1, seed=7)
    for row in rows:
        recall = row['tp'] / row['truth']
        if row['transform'] in run_eval.EXPECTED_FULL:
            assert recall == (1.0 if row['length'] >= 7 else 0.0), row
            assert row['aligned'] == row['n'] or row['length'] < 7, row
        if row['transform'] == 'exact copy' and row['length'] >= 7:
            assert row['exact_bounds'] == row['n'], row
        assert row['fp'] == 0 and row['bad_input'] == 0, row
    assert parity['mismatches'] == 0 and parity['cases'] > 0
    assert fp['pairs_with_match'] == 0
    assert run_eval.fold_check() == []


def test_citation_ground_truth():
    import eval_citations
    confusion, ok = eval_citations.evaluate(trials=3, seed=11)
    for matrix in confusion.values():
        for truth, row in matrix.items():
            assert all(v == 0 for k, v in row.items() if k != truth), (truth, row)
    assert ok['none'] == ok['quotes'] == ok['bibliography'] == ok['both'] == ok['cases']
