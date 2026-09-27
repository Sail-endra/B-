"""Independent verification of generated problems with sympy.

A model that writes a problem *and* its own answer key is unreliable, so a
generated numeric/symbolic problem is only kept if sympy, solving the same
parameters from scratch, reproduces the model's claimed answer. Anything sympy
cannot check (proofs, essays, short answer) is marked `unverified` and shown as
such -- never presented as if it were checked.

Structure: the generator emits a `template` name and a `params` dict. Each
template here has a solver that returns the canonical answer as {label: value}.
`verify(template, params, claimed)` re-solves and compares numerically. Micro is
highly tractable this way; these five templates cover the methods the syllabus
leans on (utility maximisation, budget line, elasticity, cost minimisation,
profit maximisation).
"""

from __future__ import annotations

import math
from typing import Any, Callable, Optional

import sympy as sp

# Relative tolerance for comparing a model's number to sympy's truth.
_RTOL = 1e-3


def _num(x: Any) -> Optional[float]:
    try:
        return float(sp.nsimplify(x)) if isinstance(x, str) else float(x)
    except Exception:  # noqa: BLE001
        try:
            return float(sp.sympify(x))
        except Exception:  # noqa: BLE001
            return None


# -- solvers: params -> {answer label: numeric value} ------------------------


def _cobb_douglas_utility_max(p: dict) -> dict[str, float]:
    """U(x1,x2)=x1^a x2^b, income m, prices p1,p2. Interior optimum."""
    a, b, m, p1, p2 = (sp.Rational(str(p[k])) for k in ("a", "b", "m", "p1", "p2"))
    x1, x2, lam = sp.symbols("x1 x2 lam", positive=True)
    root = sp.solve([a / x1 - lam * p1, b / x2 - lam * p2,
                     p1 * x1 + p2 * x2 - m], [x1, x2, lam], dict=True)[0]
    return {"x1": float(root[x1]), "x2": float(root[x2])}


def _budget_line(p: dict) -> dict[str, float]:
    """Prices p1,p2 and income m -> intercepts and slope of the budget line."""
    m, p1, p2 = (sp.Rational(str(p[k])) for k in ("m", "p1", "p2"))
    x1, x2, slope = sp.symbols("x1 x2 slope")
    root = sp.solve([p1 * x1 - m, p2 * x2 - m, slope + p1 / p2],
                    [x1, x2, slope], dict=True)[0]
    return {"x1_intercept": float(root[x1]), "x2_intercept": float(root[x2]),
            "slope": float(root[slope])}


def _price_elasticity(p: dict) -> dict[str, float]:
    """Linear demand Q = A - B*P; point elasticity at price P0."""
    A, B, P0 = (sp.Rational(str(p[k])) for k in ("A", "B", "P0"))
    price = sp.symbols("price")
    demand = A - B * price
    quantity = demand.subs(price, P0)
    elasticity = sp.diff(demand, price).subs(price, P0) * P0 / quantity
    return {"quantity": float(quantity), "elasticity": float(elasticity)}


def _cost_minimization(p: dict) -> dict[str, float]:
    """Cobb-Douglas production q = L^a K^b, wage w, rental r, target output Q.
    Conditional factor demands at the cost-minimising interior point."""
    a, b, w, r, Q = (sp.Rational(str(p[k])) for k in ("a", "b", "w", "r", "Q"))
    L, K, lam = sp.symbols("L K lam", positive=True)
    root = sp.solve([w - lam * a / L, r - lam * b / K,
                     a * sp.log(L) + b * sp.log(K) - sp.log(Q)],
                    [L, K, lam], dict=True)[0]
    lv, kv = float(root[L]), float(root[K])
    return {"L": lv, "K": kv, "cost": float(w) * lv + float(r) * kv}


def _profit_max_monopoly(p: dict) -> dict[str, float]:
    """Linear inverse demand P = A - B*q, constant marginal cost c. Monopoly."""
    A, B, c = (sp.Rational(str(p[k])) for k in ("A", "B", "c"))
    q = sp.symbols("q", real=True)
    profit = (A - B * q - c) * q
    quantity = sp.solve(sp.diff(profit, q), q)[0]
    price = A - B * quantity
    return {"quantity": float(quantity), "price": float(price),
            "profit": float(profit.subs(q, quantity))}


SOLVERS: dict[str, Callable[[dict], dict[str, float]]] = {
    "cobb_douglas_utility_max": _cobb_douglas_utility_max,
    "budget_line": _budget_line,
    "price_elasticity": _price_elasticity,
    "cost_minimization": _cost_minimization,
    "profit_max_monopoly": _profit_max_monopoly,
}

VERIFIABLE_TEMPLATES = tuple(SOLVERS)


def solve(template: str, params: dict) -> Optional[dict[str, float]]:
    solver = SOLVERS.get(template)
    if solver is None:
        return None
    try:
        result = solver(params)
        if not result or any(not math.isfinite(value) for value in result.values()):
            return None
        return result
    except Exception:  # noqa: BLE001 - bad params from the model = not verifiable
        return None


def _close(a: float, b: float) -> bool:
    if a is None or b is None:
        return False
    if math.isclose(a, b, rel_tol=_RTOL, abs_tol=1e-6):
        return True
    return abs(a - b) <= _RTOL * max(1.0, abs(a), abs(b))


def verify(template: str, params: dict, claimed: dict) -> tuple[bool, dict[str, float]]:
    """Return (ok, truth). ok is True only when sympy's answer reproduces every
    value the model claimed (that both sides provide)."""
    truth = solve(template, params)
    if not truth:
        return False, {}
    checked = 0
    for label, tv in truth.items():
        cv = _num(claimed.get(label)) if isinstance(claimed, dict) else None
        if cv is None:
            return False, truth
        checked += 1
        if not _close(cv, float(tv)):
            return False, truth
    # Require the model to have committed to at least one checkable value.
    return (checked > 0), truth
