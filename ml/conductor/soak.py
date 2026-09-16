#!/usr/bin/env python3
"""Conductor soak test: runs N agents for a duration, verifies health.

Usage:
    python ml/conductor/soak.py --agents 50 --duration 3600 --min-agents 40
"""
import argparse
import asyncio
import sys
import os

# Add parent dir to path for imports
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from conductor import Conductor


async def main():
    parser = argparse.ArgumentParser(description="Conductor soak test")
    parser.add_argument("--agents", type=int, default=50, help="Target concurrent agents")
    parser.add_argument("--duration", type=int, default=3600, help="Run duration in seconds")
    parser.add_argument("--min-agents", type=int, default=40, help="Minimum alive agents to pass")
    parser.add_argument("--arrivals", type=float, default=2.0, help="Arrivals per minute")
    parser.add_argument("--lifetime", type=int, default=100, help="Mean lifetime episodes")
    parser.add_argument("--wave-size", type=int, default=10, help="Agents per startup wave")
    parser.add_argument("--wave-delay", type=float, default=5.0, help="Seconds between waves")
    parser.add_argument("--base-dir", default="soak_run", help="Base directory for conductor data")
    args = parser.parse_args()

    conductor = Conductor(
        base_dir=args.base_dir,
        max_agents=args.agents,
        arrivals_per_minute=args.arrivals,
        mean_lifetime_episodes=args.lifetime,
    )

    try:
        await conductor.run(
            duration_seconds=args.duration,
            wave_size=args.wave_size,
            wave_delay=args.wave_delay,
        )
    except KeyboardInterrupt:
        print("[soak] Interrupted, shutting down...")
        conductor.stop()
        await asyncio.sleep(1)

    # Final check
    alive = conductor.registry.snapshot()["alive"]
    print(f"[soak] Final alive agents: {alive}/{args.agents}")
    if alive < args.min_agents:
        print(f"[soak] FAIL: alive {alive} < min {args.min_agents}")
        sys.exit(1)
    else:
        print(f"[soak] PASS: alive {alive} >= min {args.min_agents}")
        sys.exit(0)


if __name__ == "__main__":
    asyncio.run(main())