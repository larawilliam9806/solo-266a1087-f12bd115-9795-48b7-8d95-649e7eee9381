"""Unit tests for the two-stage LP solver."""

import math

import pytest

from app.weighting import solve


def _res(households, targets):
    return solve(households, [(t[0], t[1], t[2]) for t in targets])


def test_exactly_balanceable_returns_zero_gap_and_minimal_change():
    households = [
        {"household_id": "H1", "district": "D1", "size_band": "S1", "init_weight": 1.0},
        {"household_id": "H2", "district": "D1", "size_band": "S2", "init_weight": 1.0},
        {"household_id": "H3", "district": "D2", "size_band": "S1", "init_weight": 1.0},
        {"household_id": "H4", "district": "D2", "size_band": "S2", "init_weight": 1.0},
    ]
    # Initial weights already hit every target => stage 2 must keep them.
    targets = [
        ("district", "D1", 2.0),
        ("district", "D2", 2.0),
        ("size_band", "S1", 2.0),
        ("size_band", "S2", 2.0),
    ]
    res = _res(households, targets)
    assert res.failure_reason is None
    assert res.balanced is True
    assert res.gap < 1e-6
    assert all(abs(w - 1.0) < 1e-6 for w in res.weights)


def test_factor_bounds_respected_when_unbalanced():
    households = [
        {"household_id": "H1", "district": "D1", "size_band": "S1", "init_weight": 10.0},
    ]
    targets = [
        ("district", "D1", 1000.0),
        ("size_band", "S1", 1000.0),
    ]
    res = _res(households, targets)
    assert res.failure_reason is None
    assert res.balanced is False
    # Closest feasible point is the upper bound 2x initial weight.
    assert abs(res.weights[0] - 20.0) < 1e-6
    assert res.group_diffs[0]["difference"] == pytest.approx(20.0 - 1000.0)


def test_stage2_minimises_movement_among_optimal_solutions():
    # Two households in the same group; target equals total initial weight,
    # so any w1+w2 = const pair is stage-1 optimal. Minimum L1 movement is
    # zero change for both.
    households = [
        {"household_id": "H1", "district": "D1", "size_band": "S1", "init_weight": 3.0},
        {"household_id": "H2", "district": "D1", "size_band": "S1", "init_weight": 5.0},
    ]
    targets = [
        ("district", "D1", 8.0),
        ("size_band", "S1", 8.0),
    ]
    res = _res(households, targets)
    assert res.balanced is True
    assert abs(res.weights[0] - 3.0) < 1e-5
    assert abs(res.weights[1] - 5.0) < 1e-5


def test_lower_factor_bound_clamps():
    households = [
        {"household_id": "H1", "district": "D1", "size_band": "S1", "init_weight": 10.0},
    ]
    targets = [
        ("district", "D1", 0.0),
        ("size_band", "S1", 0.0),
    ]
    res = _res(households, targets)
    assert res.balanced is False
    # Closest feasible point to zero is the lower bound 0.5x initial.
    assert abs(res.weights[0] - 5.0) < 1e-6
    assert math.isfinite(res.gap)
