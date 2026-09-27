"""Generate practice variants of an uploaded problem, then let sympy verify them.

The model is asked to produce a variant that tests the *same method* with new
specifics, and -- crucially -- to express it as one of the verifiable templates
(`verify.VERIFIABLE_TEMPLATES`) with the parameters it chose and the answer it
computed. `verify.solve` then re-derives the answer from those parameters and the
variant is discarded unless the two agree. That is what makes "the model wrote
its own answer key" safe.

When the problem's method is not one sympy can check (a proof, an essay), the
model may still write a variant, but it is returned `unverified` and must be shown
as such -- never presented as if it were checked.
"""

from __future__ import annotations

from typing import Any

from . import verify

_PROMPT = """You are writing PRACTICE VARIANTS of a problem from a student's
course, so they can rehearse the same method on fresh numbers. Produce {n}
variants that test the SAME method as the original, with different specifics
(changed parameters, a different but equivalent context).

The original problem (topic: {topic}):
{original}
Provided solution, if the assignment includes one (use its method and level of
detail as a guide; do not copy its specific answer):
{given_solution}

If -- and only if -- the method is one of these solvable templates, express each
variant with that template so it can be machine-checked:

{templates}

For a template variant, return:
  "template": one of the names above,
  "params": the exact numeric parameters (all the keys that template needs),
  "prompt": the natural-language problem statement you built from those params,
  "answer": your computed final answer as a JSON object mapping each answer label
            to its numeric value (e.g. {{"x1": 25.0, "x2": 8.0}}),
  "solution_steps": the worked method, steps without skipping the reasoning.

If the method is NOT one of those templates (a proof, an essay, a short-answer
concept question), return instead:
  "template": "none",
  "prompt": the variant problem statement,
  "solution_steps": a model solution,
  "answer": {{}}.

Return JSON only: {{"variants": [ ... ]}}."""


def _template_menu() -> str:
    lines = {
        "cobb_douglas_utility_max": "U=x1^a x2^b, income m, prices p1,p2 -> params a,b,m,p1,p2; answer x1,x2",
        "budget_line": "prices p1,p2 income m -> params m,p1,p2; answer x1_intercept,x2_intercept,slope",
        "price_elasticity": "linear demand Q=A-B*P at price P0 -> params A,B,P0; answer quantity,elasticity",
        "cost_minimization": "produce q=L^a K^b, wage w, rental r, output Q -> params a,b,w,r,Q; answer L,K,cost",
        "profit_max_monopoly": "inverse demand P=A-B*q, marginal cost c -> params A,B,c; answer quantity,price,profit",
    }
    return "\n".join(f"- {name}: {desc}" for name, desc in lines.items())


def generate_variants(problem: dict, llm: Any, n: int = 2) -> list[dict]:
    """Return variant dicts. Each carries `verified` (sympy-confirmed) and
    `verify_method`. Unverifiable-method variants come back verified=False,
    verify_method='unverified'. A verifiable variant whose sympy check FAILS is
    dropped here and counted by the caller as a discard."""
    if not getattr(llm, "available", False):
        return []
    prompt = _PROMPT.format(
        n=n, topic=problem.get("topic", ""), original=problem.get("prompt", "")[:1500],
        given_solution=problem.get("given_solution", "")[:1200] or "(none provided)",
        templates=_template_menu(),
    )
    try:
        data = llm.json(prompt, max_tokens=3000)
    except Exception:  # noqa: BLE001
        return []

    out: list[dict] = []
    for raw in data.get("variants", []) or []:
        text = str(raw.get("prompt", "")).strip()
        if not text:
            continue
        template = str(raw.get("template", "none")).strip()
        steps = str(raw.get("solution_steps", "")).strip()

        if template in verify.VERIFIABLE_TEMPLATES:
            params = raw.get("params") or {}
            claimed = raw.get("answer") or {}
            ok, truth = verify.verify(template, params, claimed)
            if not ok:
                # Model's answer disagrees with sympy (or params were unusable):
                # discard. The caller counts this toward the discard rate.
                out.append({"_discarded": True})
                continue
            # Use sympy's answer as the canonical one -- it is the ground truth.
            answer_txt = ", ".join(f"{k} = {v:.4g}" for k, v in truth.items())
            out.append({
                "prompt": text, "type": "numeric", "answer": answer_txt,
                "solution_steps": steps, "verified": True,
                "verify_method": f"sympy:{template}", "_discarded": False,
            })
        else:
            if str(problem.get("type", "")).lower() in {
                "numeric", "symbolic", "symbolic_derivation",
            }:
                # Numeric and symbolic work is never quietly downgraded to
                # unverified: it must fit an independent solver or be discarded.
                out.append({"_discarded": True})
                continue
            out.append({
                "prompt": text, "type": str(raw.get("type", "short_answer")),
                "answer": "", "solution_steps": steps, "verified": False,
                "verify_method": "unverified", "_discarded": False,
            })
    return out
