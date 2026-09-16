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
import faulthandler
import json
import os
import sys
import time
import traceback

sys.path.insert(
    0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
)

from ml.conductor.conductor import Conductor


def setup_file_logging(base_dir):
    """Mirror all output into <base-dir>/soak.log and arm crash diagnostics.

    faulthandler covers fatal crashes (segfault/abort); sys.excepthook and
    the asyncio handler cover Python-level deaths; a periodic traceback
    dump covers silent event-loop wedges. Returns the log file object."""
    os.makedirs(base_dir, exist_ok=True)
    # Long-lived handle by design (the whole run logs here); not a leak.
    logf = open(os.path.join(base_dir, "soak.log"), "a", buffering=1)  # noqa: SIM115
    faulthandler.enable(file=logf)
    # Hang watchdog: dump every 30min even when healthy -- the only trace
    # a wedged loop leaves behind.
    faulthandler.dump_traceback_later(1800, repeat=True, file=logf)

    def _excepthook(t, v, tb):
        logf.write("".join(traceback.format_exception(t, v, tb)))
        logf.flush()

    sys.excepthook = _excepthook
    sys.stdout = logf
    sys.stderr = logf
    return logf


def parse_args():
    p = argparse.ArgumentParser(description="Conductor soak test.")
    p.add_argument("--agents", type=int, default=50)
    p.add_argument("--duration", type=float, default=3600.0, help="seconds")
    p.add_argument("--url", default="ws://localhost:8765")
    p.add_argument("--base-dir", default="soak_run")
    p.add_argument(
        "--min-agents",
        type=int,
        default=40,
        help="FAIL when fewer agents are alive at the end",
    )
    p.add_argument("--reward-mode", default="score", choices=("score", "xp", "econ"))
    p.add_argument(
        "--max-steps",
        type=int,
        default=500,
        help="env steps per episode (episodes drive mixer/PBT/status)",
    )
    p.add_argument("--step-timeout", type=float, default=30.0)
    p.add_argument("--arrivals", type=float, default=2.0, help="arrivals per minute")
    p.add_argument("--lifetime", type=int, default=15,
                   help="mean lifetime in completed episodes (episode-based "
                        "churn: ~15 episodes sustains turnover in an hour)")
    p.add_argument("--wave-size", type=int, default=10, help="agents per startup wave")
    p.add_argument(
        "--wave-delay", type=float, default=2.0, help="seconds between waves"
    )
    p.add_argument(
        "--checkpoint",
        default=None,
        help="linear weights (default: ml/ml_best.json when present)",
    )
    p.add_argument(
        "--slot",
        action="append",
        default=None,
        metavar="SPEC",
        help="agent slot: NAME[:key=val,...] with env_* keys routed "
        "to the env (repeat a slot to raise its weight). "
        "Default: one linear slot. "
        "Example: --slot gather --slot torch:checkpoint=X,epsilon=0.1",
    )
    p.add_argument(
        "--plugin-dir", default=None, help="extra directory of AgentPlugin *.py files"
    )
    p.add_argument(
        "--status-every",
        type=float,
        default=300.0,
        help="seconds between status snapshots (0 disables)",
    )
    p.add_argument(
        "--status-file",
        default=None,
        help="JSONL status log (default: <base-dir>/soak_status.jsonl)",
    )
    return p.parse_args()


def summarize(cond):
    """Lean status snapshot for the overnight log (aggregates only -- the
    full per-agent registry is saved by the conductor at the end)."""
    st = cond.status()
    agents = st["registry"]["agents"]
    episodes = sum(a["episodes"] for a in agents)
    mean_r = sum(a["mean_reward"] * a["episodes"] for a in agents) / max(1, episodes)
    return {
        "ts": time.time(),
        "uptime": round(st["uptime"], 1),
        "alive": st["registry"]["alive"],
        "total": st["registry"]["total"],
        "episodes": episodes,
        "mean_reward": round(mean_r, 4),
        "supervisor_running": st["supervisor"]["running"],
        "mixer": st["mixer"],
    }


def _append_jsonl(path, obj):
    """Append one JSON line (sync helper: keeps blocking file IO out of
    async bodies)."""
    with open(path, "a") as f:
        f.write(json.dumps(obj) + "\n")


async def status_logger(cond, path, interval):
    """Append summarize() lines until the run ends, plus a final snapshot."""
    await asyncio.sleep(min(interval, 5.0))
    while cond.status()["running"]:
        _append_jsonl(path, summarize(cond))
        await asyncio.sleep(interval)
    _append_jsonl(path, summarize(cond))


async def main():
    args = parse_args()
    logf = setup_file_logging(args.base_dir)
    loop = asyncio.get_running_loop()

    def _async_handler(loop, context):
        logf.write(
            f"asyncio: {context.get('message')} " f"{context.get('exception', '')}\n"
        )
        logf.flush()

    loop.set_exception_handler(_async_handler)
    ckpt = args.checkpoint
    if ckpt is None:
        here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        cand = os.path.join(here, "ml_best.json")
        ckpt = cand if os.path.isfile(cand) else None
    from ml.plugins import discover, parse_slot

    discover(args.plugin_dir)
    if args.slot:
        slots = [parse_slot(spec) for spec in args.slot]
        # soak-level env/step defaults apply unless the slot overrides them
        for slot in slots:
            slot["env"].setdefault("reward_mode", args.reward_mode)
            slot["env"].setdefault("max_steps", args.max_steps)
            slot.setdefault("step_timeout", args.step_timeout)
    else:
        slots = [
            {
                "plugin": "linear",
                "config": {"checkpoint": ckpt, "epsilon": 0.05},
                "env": {"reward_mode": args.reward_mode, "max_steps": args.max_steps},
                "weight": 1,
                "step_timeout": args.step_timeout,
            }
        ]
    cond = Conductor(
        args.base_dir,
        max_agents=args.agents,
        arrivals_per_minute=args.arrivals,
        mean_lifetime_episodes=args.lifetime,
        runners=slots,
        url=args.url,
    )
    status_file = args.status_file or os.path.join(args.base_dir, "soak_status.jsonl")
    print(
        f"[soak] {args.agents} agents for {args.duration:.0f}s "
        f"(checkpoint={ckpt or 'fresh'})",
        flush=True,
    )
    print(f"[soak] settings: {vars(args)}", flush=True)
    tasks = [
        cond.run(
            duration_seconds=args.duration,
            wave_size=args.wave_size,
            wave_delay=args.wave_delay,
        )
    ]
    if args.status_every > 0:
        tasks.append(status_logger(cond, status_file, args.status_every))
    try:
        await asyncio.gather(*tasks)
    except KeyboardInterrupt:
        print("[soak] Interrupted, shutting down...")
        cond.stop()
        await asyncio.sleep(1)
    st = cond.status()
    alive = st["registry"]["alive"]
    running = st["supervisor"]["running"]
    print(f"[soak] end: alive={alive}/{args.agents} running={running}")
    for ptype, cell in sorted(st.get("by_type", {}).items()):
        print(
            f"[soak]   {ptype}: alive={cell['alive']} "
            f"episodes={cell['episodes']} mean_reward={cell['mean_reward']}"
        )
    if alive < args.min_agents:
        print(f"[soak] FAIL: alive {alive} < min {args.min_agents}")
        return 1
    print("[soak] PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
