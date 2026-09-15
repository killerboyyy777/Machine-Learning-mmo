"""Shared-policy multi-agent Torch trainer for Text MMO.

Unlike launching several copies of dqn_agent.py, this process owns one DQN
and one checkpoint writer. Four (or more) WebSocket environments run as
asyncio tasks; each completed environment step is applied to the same replay
buffer and policy in the event loop, so no model updates are lost to
last-writer-wins checkpoint races.

Run from the repository root:
    python torch_agents/torch_farm.py --agents 4 --steps 1000000
"""

import argparse
import asyncio
import os
import signal
import sys
import time
from pathlib import Path

# Make sibling imports work however this file is launched: `python
# torch_agents/torch_farm.py` from the repo root, or `python -m
# torch_agents.torch_farm`.
sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "ml"))

from dqn_agent import TorchDQNAgent
from ml_env import ACTIONS, N_ACTIONS, TextMMOEnv, flatten_obs


class TorchFarm:
    def __init__(self, args):
        self.args = args
        self.stop = asyncio.Event()
        self.steps = 0
        self.last_save_step = -1
        self.last_best_score = float("-inf")
        self.agent = TorchDQNAgent(name=args.name_prefix + "Shared", url=args.url)
        if args.weights:
            loaded = self.agent.load_weights(args.weights)
        else:
            loaded = self.agent.load_weights()
        if not loaded and args.best_weights and os.path.exists(args.best_weights):
            self.agent.load_weights(args.best_weights)
        self.runners = []

    def should_stop(self):
        return self.stop.is_set() or (self.args.steps > 0 and self.steps >= self.args.steps)

    async def checkpoint(self, force=False):
        if not force and self.steps < self.args.save_every:
            return
        if not force and self.steps - self.last_save_step < self.args.save_every:
            return
        self.agent.save_weights(self.args.weights)
        self.last_save_step = self.steps
        if self.last_best_score > self.agent.best_score:
            self.agent.best_score = self.last_best_score
            self.agent.save_weights(self.args.best_weights)


class Runner:
    def __init__(self, index, farm):
        self.farm = farm
        self.name = f"{farm.args.name_prefix}{index}"
        self.env = TextMMOEnv(self.name, url=farm.args.url)
        self.features = None
        self.obs = None
        self.prev_gold = 0.0
        self.prev_inventory = set()
        self.prev_orders = []
        self.score = 0.0
        self.steps = 0

    async def reset(self):
        self.obs = await self.env.reset()
        self.features = flatten_obs(self.obs)
        self.prev_gold = float(self.obs.get("gold_raw", 0.0))
        self.prev_inventory = set(self.obs.get("inv_names", []) or [])
        self.prev_orders = []

    def transition_targets(self, next_obs, info, action_name):
        gold = float(next_obs.get("gold_raw", self.prev_gold))
        gold_delta = gold - self.prev_gold
        self.prev_gold = gold

        inventory = set(next_obs.get("inv_names", []) or [])
        loot_delta = float(len(inventory - self.prev_inventory))
        self.prev_inventory = inventory

        fill = (info or {}).get("market_fill") or {}
        market_pnl = -float(fill.get("cost", 0.0)) if action_name == "market_buy" else 0.0

        quest = (info or {}).get("quest") or {}
        by_quest = quest.get("by_quest") or {}
        quest_reward = 0.0
        if (by_quest.get("guard_charm") or {}).get("turned_in"):
            quest_reward += 15.0
        if (by_quest.get("delver") or {}).get("turned_in"):
            quest_reward += 10.0

        return gold_delta, loot_delta, market_pnl, quest_reward

    async def run(self):
        await self.reset()
        try:
            while not self.farm.should_stop():
                epsilon = self.farm.agent._epsilon()
                action_index = self.farm.agent.act(self.features, epsilon, self.env.valid_action_mask())
                action_name = ACTIONS[action_index]
                next_obs, reward, done, info = await self.env.step(action_index)
                next_features = flatten_obs(next_obs)
                gold_delta, loot_delta, market_pnl, quest_reward = self.transition_targets(
                    next_obs, info, action_name
                )

                self.farm.agent.store({
                    "state": self.features,
                    "action": action_index,
                    "reward": reward,
                    "next_state": next_features,
                    "done": done,
                    "gold_delta": gold_delta,
                    "loot_delta": loot_delta,
                    "market_pnl": market_pnl,
                    "quest_reward": quest_reward,
                    "quest_intrinsic": 0.0,
                })
                self.farm.agent.t_step += 1
                if self.farm.agent.t_step >= self.farm.agent.replay_size:
                    self.farm.agent.learn()

                self.features = next_features
                self.obs = next_obs
                self.score = next_obs["score_raw"]
                self.steps += 1
                self.farm.steps += 1
                if self.score > self.farm.last_best_score:
                    self.farm.last_best_score = self.score
                await self.farm.checkpoint()

                if done:
                    await self.reset()
        finally:
            await self.env.close()


async def main_async(args):
    farm = TorchFarm(args)
    farm.runners = [Runner(i, farm) for i in range(args.agents)]
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, farm.stop.set)
        except (NotImplementedError, ValueError):
            pass

    print(f"[torch-farm] {args.agents} agents sharing one DQN, {N_ACTIONS} actions, "
          f"{args.steps or 'unlimited'} total steps")
    tasks = [asyncio.create_task(r.run(), name=r.name) for r in farm.runners]
    try:
        await asyncio.gather(*tasks)
    except KeyboardInterrupt:
        farm.stop.set()
    finally:
        farm.stop.set()
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await farm.checkpoint(force=True)
        print("[torch-farm] finished: total_steps=" + str(farm.steps))
        for runner in farm.runners:
            print(f"  {runner.name}: steps={runner.steps} score={runner.score:.2f}")


def parse_args():
    parser = argparse.ArgumentParser(description="Shared-policy multi-agent Torch trainer.")
    parser.add_argument("--agents", type=int, default=4, help="concurrent agents (default 4)")
    parser.add_argument("--name-prefix", default="TorchFarm", help="fresh character name prefix")
    parser.add_argument("--url", default="ws://localhost:8765")
    parser.add_argument("--steps", type=int, default=0, help="total shared steps; 0 runs until stopped")
    parser.add_argument("--save-every", type=int, default=500, help="shared checkpoint interval in steps")
    parser.add_argument("--weights", default=None, help="shared current checkpoint path")
    parser.add_argument("--best-weights", default=None, help="best checkpoint path")
    args = parser.parse_args()
    if args.agents < 1:
        parser.error("--agents must be >= 1")
    if args.steps < 0 or args.save_every < 1:
        parser.error("--steps must be >= 0 and --save-every must be >= 1")
    here = os.path.dirname(os.path.abspath(__file__))
    args.weights = os.path.abspath(args.weights) if args.weights else os.path.join(here, "ml_farm_weights.json")
    args.best_weights = os.path.abspath(args.best_weights) if args.best_weights else os.path.join(here, "ml_farm_best.json")
    return args


if __name__ == "__main__":
    asyncio.run(main_async(parse_args()))
