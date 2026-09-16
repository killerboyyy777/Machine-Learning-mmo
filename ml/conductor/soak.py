"""Nightly soak driver (#60): run the conductor with N agents for T seconds.

Builds a Conductor with a live env_factory + linear policy (ml_best.json
when present, fresh weights otherwise), runs it, then applies the
pass/fail gates: alive >= --min-agents at the end. Prints a verdict line
for CI logs and exits nonzero on failure.

Smoke test (needs the server)::

    python ml/conductor/soak.py --agents 2 --duration 20 --min-agents 1

Nightly (CI):: see .github/workflows/soak.yml (50 agents, 1 hour).
"""
import argparse
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))

from ml.conductor.conductor import Conductor
from ml.conductor.runners import make_env_factory, make_linear_policy


def parse_args():
    p = argparse.ArgumentParser(description="Conductor soak test.")
    p.add_argument("--agents", type=int, default=50)
    p.add_argument("--duration", type=float, default=3600.0, help="seconds")
    p.add_argument("--url", default="ws://localhost:8765")
    p.add_argument("--base-dir", default="soak_run")
    p.add_argument("--min-agents", type=int, default=40,
                   help="FAIL when fewer agents are alive at the end")
    p.add_argument("--reward-mode", default="score", choices=("score", "xp", "econ"))
    p.add_argument("--max-steps", type=int, default=500,
                   help="env steps per episode (episodes drive mixer/PBT/status)")
    p.add_argument("--step-timeout", type=float, default=30.0)
    p.add_argument("--arrivals", type=float, default=2.0, help="arrivals per minute")
    p.add_argument("--lifetime", type=int, default=100, help="mean lifetime episodes")
    p.add_argument("--wave-size", type=int, default=10, help="agents per startup wave")
    p.add_argument("--wave-delay", type=float, default=2.0, help="seconds between waves")
    p.add_argument("--checkpoint", default=None,
                   help="linear weights (default: ml/ml_best.json when present)")
    return p.parse_args()


async def main():
    args = parse_args()
    ckpt = args.checkpoint
    if ckpt is None:
        here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        cand = os.path.join(here, "ml_best.json")
        ckpt = cand if os.path.isfile(cand) else None
    runner = {
        "env_factory": make_env_factory(args.url, reward_mode=args.reward_mode,
                                        max_steps=args.max_steps),
        "policy_fn": make_linear_policy(ckpt, epsilon=0.05),
        "step_timeout": args.step_timeout,
    }
    cond = Conductor(args.base_dir, max_agents=args.agents,
                     arrivals_per_minute=args.arrivals,
                     mean_lifetime_episodes=args.lifetime, runner=runner)
    print(f"[soak] {args.agents} agents for {args.duration:.0f}s "
          f"(checkpoint={ckpt or 'fresh'})")
    try:
        await cond.run(duration_seconds=args.duration, wave_size=args.wave_size,
                       wave_delay=args.wave_delay)
    except KeyboardInterrupt:
        print("[soak] Interrupted, shutting down...")
        cond.stop()
        await asyncio.sleep(1)
    st = cond.status()
    alive = st["registry"]["alive"]
    running = st["supervisor"]["running"]
    print(f"[soak] end: alive={alive}/{args.agents} running={running}")
    if alive < args.min_agents:
        print(f"[soak] FAIL: alive {alive} < min {args.min_agents}")
        return 1
    print("[soak] PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
