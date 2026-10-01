"""Specialist reward-mode unit tests (no live server needed).

Run from the repo root:  python tests/test_reward_modes.py
Covers #36 (econ mode) and #37 (xp mode): mode validation, inventory
pricing, and per-mode reward math via the pure _compute_reward helper.
Also #68 (reward formula DSL): the tokenizer, parser, signal plumbing, and
the fail-soft contract for a formula or hook that goes wrong mid-run.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import server as srv
from ml.ml_env import (
    ECON_INV_LAMBDA,
    REWARD_MODES,
    XP_LEVEL_BONUS,
    TextMMOEnv,
    inventory_value,
    merchant_value,
)
from ml.reward_dsl import (
    FUNCTIONS,
    SIGNALS,
    RewardFormulaError,
    compile_formula,
    reference,
)

# --- mode validation ---
assert set(REWARD_MODES) == {"score", "xp", "econ"}
env_default = TextMMOEnv("ModeDefault")
assert env_default.reward_mode == "score"
try:
    TextMMOEnv("ModeBad", reward_mode="fame")
    raise SystemExit("FAIL: invalid reward_mode accepted")
except ValueError:
    pass
print("MODE_VALIDATION_OK")

# --- inventory_value pricing ---
assert inventory_value([]) == 0
assert inventory_value(None) == 0
assert inventory_value(["No Such Item XYZ"]) == 0
iid, idef = next(iter(srv.ITEM_DEFS.items()))
name, value = idef["name"], merchant_value(iid)
assert inventory_value([name]) == value, (name, value)
assert inventory_value([name, "No Such Item XYZ"]) == value
print(f"INVENTORY_VALUE_OK ({iid}={value}g)")

# --- xp mode: pure XP, ignores score/gold/inv/social ---
env_xp = TextMMOEnv("ModeXP", reward_mode="xp")
r = env_xp._compute_reward(
    score_gain=100.0,
    xp_gain=8.0,
    levels=1,
    gold_delta=50.0,
    inv_delta=30.0,
    party_before=1,
)
assert r == 8.0 + XP_LEVEL_BONUS * 1, r
r0 = env_xp._compute_reward(0.0, 0.0, 0, 0.0, 0.0, 1)
assert r0 == 0.0, r0
print("XP_MODE_OK")

# --- econ mode: gold + lambda * inv delta, ignores score/xp ---
env_econ = TextMMOEnv("ModeEcon", reward_mode="econ")
r = env_econ._compute_reward(
    score_gain=100.0,
    xp_gain=40.0,
    levels=2,
    gold_delta=7.0,
    inv_delta=-3.0,
    party_before=1,
)
assert r == 7.0 + ECON_INV_LAMBDA * -3.0, r
print("ECON_MODE_OK")

# --- score mode unchanged: solo passes score through, social still applies ---
env_score = TextMMOEnv("ModeScore")
r = env_score._compute_reward(2.5, 99.0, 3, 99.0, 99.0, 1)
assert r == 2.5, r
env_score._state["other_players"] = 1
env_score._state["party_size"] = 2
env_score._state["score"] = 0.0
r = env_score._compute_reward(2.5, 0.0, 0, 0.0, 0.0, 2)
assert abs(r - 2.55) < 1e-9, r  # social only (already grouped)
r = env_score._compute_reward(2.5, 0.0, 0, 0.0, 0.0, 1)
assert abs(r - 3.05) < 1e-9, r  # social + formation bonus on 1->2
print("SCORE_MODE_OK")

# =====================================================================
# #68: custom reward formulas. Everything below is offline -- env
# construction is pure, and _reward_signals/_compute_reward never touch
# a socket.
# =====================================================================

# --- the issue's own formula, evaluated as written ---
_ISSUE = "0.5*xp_delta + 1.2*profit - 0.1*deaths"


def _full(**over):
    """A complete signal dict: evaluate() rejects a formula naming a
    signal the caller did not supply, so tests must pass the whole
    vocabulary rather than just the terms under test."""
    sig = {name: 0.0 for name in SIGNALS}
    sig["level"] = 1.0
    sig["party_size"] = 1.0
    sig.update(over)
    return sig


_f = compile_formula(_ISSUE)
assert _f.signals_used == {"xp_delta", "profit", "deaths"}, _f.signals_used
# xp 2, gold_delta 10, inv_delta 2 -> profit 12, 2 deaths
assert (
    abs(
        _f.evaluate(
            _full(xp_delta=2.0, gold_delta=10.0, inv_delta=2.0, deaths=2.0, profit=12.0)
        )
        - (0.5 * 2 + 1.2 * 12 - 0.1 * 2)
    )
    < 1e-9
)

# --- every documented signal is nameable, and nothing else is ---
assert "xp_delta" in SIGNALS and "profit" in SIGNALS and "deaths" in SIGNALS
assert "min" in FUNCTIONS and "clamp" in FUNCTIONS
assert reference()  # CLI help / error text must not be empty
for bad in ("xp_dlta", "killz", "reward", "len", "True", "__import__"):
    try:
        compile_formula(bad)
        raise SystemExit(f"FAIL: unknown signal accepted: {bad}")
    except RewardFormulaError:
        pass

# --- a typo names the near-miss rather than just failing ---
try:
    compile_formula("0.5*xp_dlta + 1")
    raise SystemExit("FAIL: typo accepted")
except RewardFormulaError as e:
    assert "xp_delta" in str(e), e  # the did-you-mean hint

# --- no code execution: the tokenizer has no attribute/call escape ---
for evil in (
    "__import__('os').system('echo pwned')",
    "eval('1')",
    "open('/etc/passwd')",
    "xp_delta.__class__",
    "xp_delta.real",
    "1 if xp_delta else 2",
    "[x for x in (1,2)]",
    "xp_delta; xp_delta",
    "xp_delta ** 2",
    "xp_delta // 2",
    "xp_delta % 2",
    "profit & 1",
    "lambda: 1",
):
    try:
        compile_formula(evil)
        raise SystemExit(f"FAIL: unsafe/unsupported expression accepted: {evil!r}")
    except RewardFormulaError:
        pass

# --- arithmetic and functions, incl. precedence and parens ---
_cases = [
    ("1 + 2 * 3", {}, 7.0),
    ("(1 + 2) * 3", {}, 9.0),
    ("-2 * 3", {}, -6.0),
    ("--2", {}, 2.0),
    ("-(1 + 1)", {}, -2.0),
    ("abs(0 - 3.5)", {}, 3.5),
    ("sign(0 - 7)", {}, -1.0),
    ("floor(7 / 2)", {}, 3.0),
    ("ceil(7 / 2)", {}, 4.0),
    ("min(3, 1, 2)", {}, 1.0),
    ("max(3, 1, 2)", {}, 3.0),
    ("clamp(9, 0, 4)", {}, 4.0),
    ("clamp(0 - 9, 0, 4)", {}, 0.0),
    ("clamp(2, 0, 4)", {}, 2.0),
    ("2 * clamp(0.5 * xp_delta, 0, 1)", {"xp_delta": 5.0}, 2.0),
    ("0.5e1", {}, 5.0),
    ("profit", {"profit": 1.25}, 1.25),
]
for expr, sig, want in _cases:
    got = compile_formula(expr).evaluate(_full(**sig))
    assert abs(got - want) < 1e-9, f"{expr}: got {got}, want {want}"

print("FORMULA_PARSE_OK")

# --- a bad formula fails at construction, not 3 hours into a run ---
for bad in (
    "0.5*",
    "profit +",
    "profit)",
    "profit profit",
    "(1 + 2",
    "min(1)",
    "clamp(1, 2)",
    "foo(1)",
    "1 2",
    "()",
):
    try:
        TextMMOEnv("DslBad", reward_formula=bad)
        raise SystemExit(f"FAIL: bad formula accepted at construction: {bad!r}")
    except RewardFormulaError:
        pass
# empty means "not set" (falsy), same as omitting the kwarg -- so an
# unset CLI default does not need special-casing
assert TextMMOEnv("DslEmpty", reward_formula="")._formula is None
print("FORMULA_REJECT_OK")

# --- division by zero is counted, not raised: 0.0 keeps the step alive ---
_z = compile_formula("xp_delta / (deaths - deaths)")
assert _z.evaluate(_full(xp_delta=5.0, deaths=3.0)) == 0.0
assert _z.zero_divisions == 1, _z.zero_divisions
assert compile_formula("xp_delta / 2").zero_divisions == 0
print("FORMULA_DIVZERO_OK")

# --- signals: the formula sees real per-step quantities ---
_dsl = TextMMOEnv("DslSignals")
_dsl._state["other_players"] = 1
_dsl._state["party_size"] = 2
_dsl._state["score"] = 0.0
_sig = _dsl._reward_signals(
    score_gain=2.0,
    xp_gain=7.0,
    levels=1,
    gold_delta=10.0,
    inv_delta=2.0,
    deaths=0,
    gold_lost=0.0,
    gold_dropped=0.0,
    xp_lost=0.0,
    steps_since_death=3,
)
assert set(_sig) == set(SIGNALS), sorted(set(SIGNALS) ^ set(_sig))
assert _sig["xp_delta"] == 7.0 and _sig["level_delta"] == 1.0
assert _sig["score_delta"] == 2.0 and _sig["gold_delta"] == 10.0
assert _sig["inv_delta"] == 2.0 and _sig["profit"] == 12.0  # gold + inventory
assert _sig["steps_since_death"] == 3.0 and _sig["steps"] == 0.0
assert _sig["party_size"] == 2.0 and _sig["level"] == 1.0
# social/formation are the same terms score mode would have paid, so a
# formula can opt back in instead of silently losing group shaping
assert abs(_sig["social"] - 0.05) < 1e-9, _sig["social"]
# profit must be gold + inv only -- not the score, which decays with
# difficulty; that is the whole reason a formula names profit at all
assert _sig["gold_lost"] == 0.0 and _sig["xp_lost"] == 0.0
print("FORMULA_SIGNALS_OK")

# --- the formula replaces the mode reward outright ---
_env = TextMMOEnv("DslRun", reward_formula=_ISSUE)
r = _env._compute_reward(
    score_gain=2.0,
    xp_gain=2.0,
    levels=1,
    gold_delta=10.0,
    inv_delta=2.0,
    party_before=1,
    signals=_env._reward_signals(
        score_gain=2.0,
        xp_gain=2.0,
        levels=1,
        gold_delta=10.0,
        inv_delta=2.0,
        deaths=2,
        gold_lost=0.0,
        gold_dropped=0.0,
        xp_lost=0.0,
        steps_since_death=0,
    ),
)
assert abs(r - (0.5 * 2 + 1.2 * 12 - 0.1 * 2)) < 1e-9, r
# score mode would have said 2.0 here: the formula really did take over
assert abs(r - 2.0) > 1.0, r
# without a custom reward the mode path is untouched (default runs are
# byte-identical to pre-#68)
_plain = TextMMOEnv("DslPlain")
assert _plain._compute_reward(2.5, 99.0, 3, 99.0, 99.0, 1, None) == 2.5
assert _plain.custom_reward_errors == 0
print("FORMULA_REPLACES_MODE_OK")

# --- formula + reward_mode: the formula wins, and says so once ---
_warn = TextMMOEnv("DslWarn", reward_formula="score_delta", reward_mode="xp")
assert _warn._formula is not None and _warn.reward_mode == "xp"
r = _warn._compute_reward(
    score_gain=4.0,
    xp_gain=9.0,
    levels=1,
    gold_delta=0.0,
    inv_delta=0.0,
    party_before=1,
    signals=_warn._reward_signals(
        score_gain=4.0,
        xp_gain=9.0,
        levels=1,
        gold_delta=0.0,
        inv_delta=0.0,
        deaths=0,
        gold_lost=0.0,
        gold_dropped=0.0,
        xp_lost=0.0,
        steps_since_death=0,
    ),
)
assert r == 4.0, r  # formula (score_delta), not xp mode (9 + 5)
print("FORMULA_BEATS_MODE_OK")

# --- the fail-soft contract: a broken hook degrades, never crashes ---
_seen = []


def _boom(signals):
    _seen.append(signals)
    raise RuntimeError("research hook exploded")


_hard = TextMMOEnv("DslHook", reward_formula="score_delta", reward_fn=_boom)
r = _hard._compute_reward(
    score_gain=6.0,
    xp_gain=0.0,
    levels=0,
    gold_delta=0.0,
    inv_delta=0.0,
    party_before=1,
    signals=_hard._reward_signals(
        score_gain=6.0,
        xp_gain=0.0,
        levels=0,
        gold_delta=0.0,
        inv_delta=0.0,
        deaths=0,
        gold_lost=0.0,
        gold_dropped=0.0,
        xp_lost=0.0,
        steps_since_death=0,
    ),
)
assert r == 6.0, r  # hook raised -> formula answered
assert _hard.custom_reward_errors == 1, _hard.custom_reward_errors
assert _seen and "xp_delta" in _seen[0]  # hook got the real signals

# non-finite from a hook is refused the same way
_nan = TextMMOEnv(
    "DslNan", reward_formula="score_delta", reward_fn=lambda s: float("nan")
)
r = _nan._compute_reward(
    score_gain=8.0,
    xp_gain=0.0,
    levels=0,
    gold_delta=0.0,
    inv_delta=0.0,
    party_before=1,
    signals=_nan._reward_signals(
        score_gain=8.0,
        xp_gain=0.0,
        levels=0,
        gold_delta=0.0,
        inv_delta=0.0,
        deaths=0,
        gold_lost=0.0,
        gold_dropped=0.0,
        xp_lost=0.0,
        steps_since_death=0,
    ),
)
assert r == 8.0, r
assert _nan.custom_reward_errors == 1, _nan.custom_reward_errors

# hook outranks the formula when it has an opinion
_win = TextMMOEnv("DslWin", reward_formula="score_delta", reward_fn=lambda s: 42.0)
r = _win._compute_reward(
    score_gain=8.0,
    xp_gain=0.0,
    levels=0,
    gold_delta=0.0,
    inv_delta=0.0,
    party_before=1,
    signals=_win._reward_signals(
        score_gain=8.0,
        xp_gain=0.0,
        levels=0,
        gold_delta=0.0,
        inv_delta=0.0,
        deaths=0,
        gold_lost=0.0,
        gold_dropped=0.0,
        xp_lost=0.0,
        steps_since_death=0,
    ),
)
assert r == 42.0, r
assert _win.custom_reward_errors == 0

# a hook returning None abstains, so the formula answers -- this is the
# default AgentPlugin.reward contract
_abstain = TextMMOEnv(
    "DslAbstain", reward_formula="score_delta", reward_fn=lambda s: None
)
r = _abstain._compute_reward(
    score_gain=8.0,
    xp_gain=0.0,
    levels=0,
    gold_delta=0.0,
    inv_delta=0.0,
    party_before=1,
    signals=_abstain._reward_signals(
        score_gain=8.0,
        xp_gain=0.0,
        levels=0,
        gold_delta=0.0,
        inv_delta=0.0,
        deaths=0,
        gold_lost=0.0,
        gold_dropped=0.0,
        xp_lost=0.0,
        steps_since_death=0,
    ),
)
assert r == 8.0, r
assert _abstain.custom_reward_errors == 0, _abstain.custom_reward_errors

# no hook and no formula: pure mode, and the error counter stays at zero
_none = TextMMOEnv("DslNone")
assert _none._call_reward_hook({"score_delta": 1.0}) is None
assert _none.custom_reward_errors == 0
print("FORMULA_FAILSOFT_OK")

print("ALL_REWARD_MODES_OK")
