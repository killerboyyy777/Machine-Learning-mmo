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


def main():
    p = argparse.ArgumentParser(description="Deterministic agent evaluation.")
    p.add_argument("--checkpoint", required=True, help="weights to evaluate")
    p.add_argument("--baseline", default=None, help="second weights, same seeds")
    p.add_argument("--policy", default="torch", choices=("torch", "linear"))
    p.add_argument("--url", default="ws://localhost:8765")
    p.add_argument("--seeds", type=int, default=10)
    p.add_argument("--steps", type=int, default=500, help="env steps per seed")
    p.add_argument("--reward-mode", default="score", choices=("score", "xp", "econ"))
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


if __name__ == "__main__":
    try:
        main()
    except (OSError, ConnectionError) as e:
        print(f"Could not reach the server: {e}")
        print("Start the engine first: python server.py")
