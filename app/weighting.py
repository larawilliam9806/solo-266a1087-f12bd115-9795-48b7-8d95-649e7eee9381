"""Calibration solver backed by the native HiGHS LP engine.

Lexicographic optimisation, two linear programmes:

Stage 1 minimises the sum of absolute group deviations
        sum_g |weighted_count_g - target_g| across both dimensions,
        subject to 0.5 * init_weight <= w_i <= 2 * init_weight.

Stage 2 keeps that optimum deviation fixed (gap <= optimum + tolerance) and
        minimises sum_i |w_i - init_weight_i|.

Absolute values are modelled with non-negative slack pairs:
  * group g:  A_g w + d+_g - d-_g = target_g,  objective picks up d+ + d-;
  * household i movement:  w_i - v_i <= init_i and -w_i - v_i <= -init_i.

The factor bounds make the feasible region a non-empty compact box, so both
LPs always have an optimum; any non-optimal solver status is reported as a
computation failure instead of returning partial numbers.
"""

from __future__ import annotations

import math
import os
from contextlib import contextmanager
from dataclasses import dataclass

import highspy
import numpy as np

LOWER_FACTOR = 0.5
UPPER_FACTOR = 2.0
# Extra slack (in household units) allowed when freezing the stage-1 optimum.
GAP_TOLERANCE = 1e-6
BALANCED_TOLERANCE = 1e-5


@contextmanager
def _silence_stdout():
    """Redirect the OS-level stdout (the HiGHS banner is emitted by native
    code during Highs() construction, before any Python option takes effect).
    """
    devnull = os.open(os.devnull, os.O_WRONLY)
    saved = os.dup(1)
    try:
        os.dup2(devnull, 1)
        yield
    finally:
        os.dup2(saved, 1)
        os.close(devnull)
        os.close(saved)


@dataclass
class Group:
    dimension: str
    name: str
    target: float


@dataclass
class SolveResult:
    weights: list[float]
    balanced: bool
    gap: float
    group_diffs: list[dict]
    failure_reason: str | None = None


def _new_highs() -> highspy.Highs:
    with _silence_stdout():
        h = highspy.Highs()
    h.setOptionValue("output_flag", False)
    h.setOptionValue("log_to_console", False)
    # Both LPs are small in row count; simplex handles the bounds-heavy form.
    h.setOptionValue("solver", "simplex")
    return h


def _group_row(members: np.ndarray, slack_plus: int, slack_minus: int):
    """Indices/values for sum(member weights) - d+ + d- = target.

    With that convention d+ - d- = weighted_count - target, i.e. the
    reported difference.  Both slacks are non-negative and the objective
    minimises their sum, so at optimum one of each pair is zero.
    """
    idx = np.concatenate(
        [members.astype(np.int32), np.array([slack_plus, slack_minus], np.int32)]
    )
    val = np.concatenate(
        [np.ones(len(members)), np.array([-1.0, 1.0])]
    )
    return idx, val


def _check_status(h: highspy.Highs, stage: str) -> str | None:
    status = h.getModelStatus()
    if status == highspy.HighsModelStatus.kOptimal:
        return None
    return f"{stage}线性规划未取得最优解: {h.modelStatusToString(status)}"


def solve(households: list[dict], target_rows: list[dict]) -> SolveResult:
    n = len(households)
    init = np.array([h["init_weight"] for h in households], dtype=float)
    lower = LOWER_FACTOR * init
    upper = UPPER_FACTOR * init

    groups = [Group(d, g, float(t)) for d, g, t in target_rows]
    m = len(groups)
    targets = np.array([g.target for g in groups], dtype=float)

    membership: list[np.ndarray] = []
    for group in groups:
        members = [
            i for i, hh in enumerate(households)
            if hh[group.dimension] == group.name
        ]
        membership.append(np.asarray(members, dtype=np.int32))

    # ------------------------------------------------------------------
    # Stage 1: variables [w (n), d+ (m), d- (m)], min sum(d+ + d-)
    # equality per group: sum_{i in g} w_i + d+_g - d-_g = target_g
    # ------------------------------------------------------------------
    h1 = _new_highs()
    h1.addVars(
        n + 2 * m,
        np.concatenate([lower, np.zeros(2 * m)]),
        np.concatenate([upper, np.full(2 * m, highspy.kHighsInf)]),
    )
    h1.changeColsCost(
        2 * m,
        np.arange(n, n + 2 * m, dtype=np.int32),
        np.ones(2 * m),
    )
    for gi, members in enumerate(membership):
        idx, val = _group_row(members, n + gi, n + m + gi)
        t = float(targets[gi])
        h1.addRow(t, t, len(idx), idx, val)

    h1.run()
    reason = _check_status(h1, "第一阶段")
    if reason is not None:
        return SolveResult([], False, math.inf, [], failure_reason=reason)

    opt_gap = float(h1.getObjectiveValue())

    # ------------------------------------------------------------------
    # Stage 2: variables [w (n), v (n), d+ (m), d- (m)], min sum(v)
    # group equalities as stage 1 (slack columns start at 2n)
    # movement: w - v <= init ;  -w - v <= -init
    # freeze:   sum(d+ + d-) <= opt_gap + GAP_TOLERANCE
    # ------------------------------------------------------------------
    h2 = _new_highs()
    h2.addVars(
        2 * n + 2 * m,
        np.concatenate([lower, np.zeros(n + 2 * m)]),
        np.concatenate([upper, np.full(n + 2 * m, highspy.kHighsInf)]),
    )
    h2.changeColsCost(
        n, np.arange(n, 2 * n, dtype=np.int32), np.ones(n)
    )
    for gi, members in enumerate(membership):
        idx, val = _group_row(members, 2 * n + gi, 2 * n + m + gi)
        t = float(targets[gi])
        h2.addRow(t, t, len(idx), idx, val)

    for i in range(n):
        pair = np.array([i, n + i], dtype=np.int32)
        h2.addRow(
            -highspy.kHighsInf, float(init[i]), 2, pair,
            np.array([1.0, -1.0]),
        )
        h2.addRow(
            -highspy.kHighsInf, float(-init[i]), 2, pair,
            np.array([-1.0, -1.0]),
        )

    h2.addRow(
        -highspy.kHighsInf,
        opt_gap + GAP_TOLERANCE,
        2 * m,
        np.arange(2 * n, 2 * n + 2 * m, dtype=np.int32),
        np.ones(2 * m),
    )

    h2.run()
    reason = _check_status(h2, "第二阶段")
    if reason is not None:
        return SolveResult([], False, opt_gap, [], failure_reason=reason)

    solution = np.asarray(h2.getSolution().col_value, dtype=float)
    weights = solution[:n]
    d_plus = solution[2 * n:2 * n + m]
    d_minus = solution[2 * n + m:2 * n + 2 * m]

    # Clamp tiny numerical drift outside the legal factor range.
    weights = np.minimum(np.maximum(weights, lower), upper)

    diffs = d_plus - d_minus  # weighted_count - target per group
    gap = float(np.abs(diffs).sum())
    balanced = gap <= max(BALANCED_TOLERANCE, BALANCED_TOLERANCE * m)

    group_diffs = [
        {
            "dimension": groups[i].dimension,
            "group_name": groups[i].name,
            "weighted_count": float(targets[i] + diffs[i]),
            "target_count": float(targets[i]),
            "difference": float(diffs[i]),
        }
        for i in range(m)
    ]
    return SolveResult(
        weights=[float(w) for w in weights],
        balanced=balanced,
        gap=gap,
        group_diffs=group_diffs,
    )
