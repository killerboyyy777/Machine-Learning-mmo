"""Deterministic evaluation harness (#54): fixed seeds, no exploration,
mean/std reports over N seeds. Answers "is agent A better than B?" by
running both checkpoints on identical seeds.

Live (needs the server)::

    python server.py                                  # terminal 1
    python -m torch_agents.eval --checkpoint torch_agents/ml_best.json --seeds 10
    python -m torch_agents.eval --checkpoint A.pt --baseline B.pt --seeds 10

Seeds control agent-side RNG only (action sampling is off at epsilon=0,
but torch/linear tie-breaks and env timing still vary); the world itself
is the shared persistent server, so treat scores as comparative, not
absolute. Each seed runs a fresh character for --steps env steps.
"""

import argparse
import asyncio
import json
import math
import random
import statistics
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import torch

from ml.ml_env import TextMMOEnv, flatten_obs


def _load_agent(policy, checkpoint):
    if policy == "linear":
        from ml.ml_client import LinearQAgent
        from ml.ml_env import N_ACTIONS, OBS_SIZE

        agent = LinearQAgent(OBS_SIZE, N_ACTIONS)
        agent.load(checkpoint)
        return lambda feats, mask: agent.act(feats, 0.0, mask)
    from torch_agents.dqn_agent import TorchDQNAgent

    agent = TorchDQNAgent()
    agent.load_weights(checkpoint)
    return lambda feats, mask: agent.act(feats, 0.0, mask)


async def eval_seed(act_fn, url, seed, steps, reward_mode):
    random.seed(seed)
    torch.manual_seed(seed)
    env = TextMMOEnv(f"Eval{seed}", url=url, max_steps=steps, reward_mode=reward_mode)
    obs = await env.reset()
    feats = flatten_obs(obs)
    done = False
    while not done:
        action = act_fn(feats, env.valid_action_mask())
        obs, _reward, done, _info = await env.step(action)
        feats = flatten_obs(obs)
    score = obs["score_raw"]
    await env.close()
    return score


async def evaluate(checkpoint, policy, url, seeds, steps, reward_mode):
    act_fn = _load_agent(policy, checkpoint)
    scores = []
    for s in seeds:
        score = await eval_seed(act_fn, url, s, steps, reward_mode)
        scores.append(score)
        print(f"  seed {s}: score={score:.2f}")
    return scores


def report(name, scores):
    mean = statistics.fmean(scores)
    std = statistics.pstdev(scores) if len(scores) > 1 else 0.0
    print(
        f"{name}: n={len(scores)} mean={mean:.2f} std={std:.2f} "
        f"min={min(scores):.2f} max={max(scores):.2f}"
    )
    return mean, std


# ---------------------------------------------------------------------------
# Paired comparison statistics (#69). Same seeds => paired differences, so
# a one-sample t-test on challenger-minus-baseline is the right tool.
# Implemented exactly (regularized incomplete beta + bisection for the
# critical value) so no scipy dependency is needed.
# ---------------------------------------------------------------------------

def _betacf(a, b, x):
    """Continued fraction for the incomplete beta function."""
    qab, qap, qam = a + b, a + 1.0, a - 1.0
    c, d = 1.0, 1.0 - qab * x / qap
    if abs(d) < 1e-300:
        d = 1e-300
    d = 1.0 / d
    h = d
    for m in range(1, 200):
        m2 = 2 * m
        aa = m * (b - m) * x / ((qam + m2) * (a + m2))
        d = 1.0 + aa * d
        if abs(d) < 1e-300:
            d = 1e-300
        c = 1.0 + aa / c
        if abs(c) < 1e-300:
            c = 1e-300
        d = 1.0 / d
        h *= d * c
        aa = -(a + m) * (qab + m) * x / ((a + m2) * (qap + m2))
        d = 1.0 + aa * d
        if abs(d) < 1e-300:
            d = 1e-300
        c = 1.0 + aa / c
        if abs(c) < 1e-300:
            c = 1e-300
        d = 1.0 / d
        delt = d * c
        h *= delt
        if abs(delt - 1.0) < 1e-12:
            break
    return h


def _beta_reg(a, b, x):
    """Regularized incomplete beta I_x(a, b)."""
    if x <= 0.0:
        return 0.0
    if x >= 1.0:
        return 1.0
    lbeta = math.lgamma(a + b) - math.lgamma(a) - math.lgamma(b)
    front = math.exp(a * math.log(x) + b * math.log(1.0 - x) + lbeta)
    if x < (a + 1.0) / (a + b + 2.0):
        return front * _betacf(a, b, x) / a
    return 1.0 - front * _betacf(b, a, 1.0 - x) / b


def _t_sf(t, df):
    """Two-sided Student-t survival: P(|T| >= |t|) with df degrees."""
    if df <= 0:
        return 1.0
    x = df / (df + t * t)
    return _beta_reg(df / 2.0, 0.5, x)


def _t_crit(alpha_two_sided, df):
    """Two-sided critical value via bisection (monotone in t)."""
    lo, hi = 0.0, 1.0
    while _t_sf(hi, df) > alpha_two_sided:
        hi *= 2.0
        if hi > 1e12:
            break
    for _ in range(100):
        mid = (lo + hi) / 2.0
        if _t_sf(mid, df) > alpha_two_sided:
            lo = mid
        else:
            hi = mid
    return (lo + hi) / 2.0


def compare(champ_scores, base_scores, alpha=0.05, min_n=5):
    """Paired challenger-vs-baseline comparison on identical seeds.

    Returns a JSON-serializable dict with mean_diff, paired t statistic,
    two-sided p-value, Cohen's d (paired), 95% CI on the mean diff, and a
    SIGNIFICANT/INCONCLUSIVE verdict. Fewer than min_n seeds (or a
    length mismatch) yields INCONCLUSIVE without statistics."""
    n = len(champ_scores)
    out = {"n": n, "verdict": "INCONCLUSIVE", "reason": None}
    if n != len(base_scores):
        out["reason"] = "seed count mismatch"
        return out
    if n < min_n:
        out["reason"] = f"n too small (need >={min_n})"
        return out
    diffs = [c - b for c, b in zip(champ_scores, base_scores)]
    mean_d = statistics.fmean(diffs)
    sd = statistics.stdev(diffs) if n > 1 else 0.0
    out["mean_diff"] = mean_d
    if sd == 0.0:
        # Identical diffs: difference is exact (or exactly zero).
        out.update(t=float("inf") if mean_d != 0.0 else 0.0,
                   p_value=0.0 if mean_d != 0.0 else 1.0,
                   cohen_d=float("inf") if mean_d > 0 else
                   (float("-inf") if mean_d < 0 else 0.0),
                   ci95=[mean_d, mean_d])
        out["verdict"] = "SIGNIFICANT" if mean_d != 0.0 else "INCONCLUSIVE"
        return out
    se = sd / math.sqrt(n)
    t = mean_d / se
    p = _t_sf(t, n - 1)
    crit = _t_crit(alpha, n - 1)
    out.update(t=t, p_value=p, cohen_d=mean_d / sd,
               ci95=[mean_d - crit * se, mean_d + crit * se])
    out["verdict"] = "SIGNIFICANT" if p < alpha else "INCONCLUSIVE"
    return out


def main():
    p = argparse.ArgumentParser(description="Deterministic agent evaluation.")
    p.add_argument("--checkpoint", required=True, help="weights to evaluate")
    p.add_argument("--baseline", default=None, help="second weights, same seeds")
    p.add_argument("--policy", default="torch", choices=("torch", "linear"))
    p.add_argument("--url", default="ws://localhost:8765")
    p.add_argument("--seeds", type=int, default=10)
    p.add_argument("--steps", type=int, default=500, help="env steps per seed")
    p.add_argument("--reward-mode", default="score", choices=("score", "xp", "econ"))
    p.add_argument("--test", default="ttest", choices=("none", "ttest"),
                   help="paired significance test on identical seeds")
    p.add_argument("--out", default=None,
                   help="write run record JSON (scores + comparison for #65)")
    args = p.parse_args()

    seeds = list(range(args.seeds))
    print(
        f"[eval] {args.policy} {args.checkpoint} seeds={args.seeds} steps={args.steps}"
    )
    champ = asyncio.run(
        evaluate(
            args.checkpoint, args.policy, args.url, seeds, args.steps, args.reward_mode
        )
    )
    m, _s = report("challenger", champ)
    record = {"checkpoint": args.checkpoint, "baseline": args.baseline,
              "policy": args.policy, "seeds": seeds, "steps": args.steps,
              "reward_mode": args.reward_mode, "challenger": champ,
              "comparison": None}
    if args.baseline:
        print(f"[eval] baseline {args.baseline}")
        base = asyncio.run(
            evaluate(
                args.baseline,
                args.policy,
                args.url,
                seeds,
                args.steps,
                args.reward_mode,
            )
        )
        mb, _sb = report("baseline  ", base)
        print(f"delta (challenger - baseline): {m - mb:+.2f}")
        record["baseline_scores"] = base
        if args.test == "ttest":
            comp = compare(champ, base)
            record["comparison"] = comp
            if comp.get("reason"):
                print(f"test: INCONCLUSIVE ({comp['reason']})")
            else:
                lo, hi = comp["ci95"]
                print(f"test: {comp['verdict']} "
                      f"(paired t={comp['t']:.3f}, p={comp['p_value']:.4f}, "
                      f"d={comp['cohen_d']:.3f}, "
                      f"95% CI [{lo:+.2f}, {hi:+.2f}])")
    if args.out:
        with open(args.out, "w") as f:
            json.dump(record, f, indent=2)
        print(f"[eval] run record -> {args.out}")


if __name__ == "__main__":
    try:
        main()
    except (OSError, ConnectionError) as e:
        print(f"Could not reach the server: {e}")
        print("Start the engine first: python server.py")
