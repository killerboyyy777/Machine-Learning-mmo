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
from pathlib import Path

# Make sibling imports work however this file is launched: `python
# torch_agents/torch_farm.py` from the repo root, or `python -m
# torch_agents.torch_farm`.
sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "ml"))

from dqn_agent import TorchDQNAgent
from ml_env import (
    ACTIONS,
    N_ACTIONS,
    QUEST_DELVER_REWARD_POINTS,
    QUEST_REWARD_POINTS,
    QUESTS,
    TextMMOEnv,
    flatten_obs,
    market_net,
    quest_charm_net,
)


class TorchFarm:
    def __init__(self, args):
        self.args = args
        self.stop = asyncio.Event()
        self.steps = 0
        self.last_save_step = -1
        self.last_best_score = float("-inf")
        self.agent = TorchDQNAgent(name=args.name_prefix + "Shared", url=args.url)
        loaded = self.agent.load_weights(args.weights)
        if not loaded and args.best_weights and os.path.exists(args.best_weights):
            self.agent.load_weights(args.best_weights)
        self.runners = []

    def should_stop(self):
        return self.stop.is_set() or (
            self.args.steps > 0 and self.steps >= self.args.steps
        )

    async def checkpoint(self, force=False):
        if not force and self.steps < self.args.save_every:
            return
        if not force and self.steps - self.last_save_step < self.args.save_every:
            return
        self.agent.save_weights(self.args.weights)
        self.last_save_step = self.steps


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

    def transition_targets(self, next_obs, next_features, info, action_name):
        """Auxiliary targets mirroring single-agent train() (#234): the
        farm used to store count-loot, buy-only P&L, zero intrinsic and no
        curiosity, silently training a dumber objective."""
        agent = self.farm.agent
        gold = float(next_obs.get("gold_raw", self.prev_gold))
        gold_delta = gold - self.prev_gold
        self.prev_gold = gold

        cur_inv = set(next_obs.get("inv_names", []) or [])
        new_items = cur_inv - self.prev_inventory
        qinfo = (info or {}).get("quest") or {}
        if qinfo.get("crafted_charm"):
            loot_delta = float(quest_charm_net())
        else:
            loot_delta = 0.0
            if new_items:
                loot_delta = min(1.0, gold) / max(1.0, len(new_items))
        self.prev_inventory = cur_inv

        fill = (info or {}).get("market_fill") or {}
        if action_name == "market_buy" and fill.get("cost", 0.0) > 0:
            market_pnl = -float(fill["cost"])
        else:
            prev_ids = {o["id"]: o for o in (self.prev_orders or [])}
            cur_ids = {o["id"]: o for o in (info.get("own_orders") or [])}
            market_pnl = sum(
                market_net(o["price"]) for i, o in prev_ids.items() if i not in cur_ids
            )
        self.prev_orders = info.get("own_orders") or []

        by_quest = qinfo.get("by_quest") or {}
        quest_reward = 0.0
        if (by_quest.get("guard_charm") or {}).get("turned_in"):
            quest_reward += float(QUEST_REWARD_POINTS)
        if (by_quest.get("delver") or {}).get("turned_in"):
            quest_reward += float(QUEST_DELVER_REWARD_POINTS)
        if (by_quest.get("remedy") or {}).get("turned_in"):
            quest_reward += float(QUESTS["remedy"]["reward_points"])
        if (by_quest.get("tonic") or {}).get("turned_in"):
            quest_reward += float(QUESTS["tonic"]["reward_points"])
        quest_intrinsic = 0.0
        if any(
            (by_quest.get(q) or {}).get("accepted")
            for q in ("guard_charm", "delver", "remedy", "tonic")
        ):
            quest_intrinsic += agent.intrinsic_accept
        if qinfo.get("crafted_charm") or qinfo.get("delver_became_ready"):
            quest_intrinsic += agent.intrinsic_progress
        for mat_key in ("quest_mat_bark", "quest_mat_hide", "quest_mat_ecto"):
            if float(next_obs.get(mat_key, 0.0)) > float(
                (self.obs or {}).get(mat_key, 0.0)
            ):
                quest_intrinsic += agent.intrinsic_progress

        rnd_bonus = agent.rnd_bonus(next_features) if agent.rnd_lambda else 0.0

        return (
            gold_delta,
            loot_delta,
            market_pnl,
            quest_reward,
            quest_intrinsic,
            rnd_bonus,
        )

    async def run(self):
        await self.reset()
        try:
            while not self.farm.should_stop():
                epsilon = self.farm.agent._epsilon()
                action_index = self.farm.agent.act(
                    self.features, epsilon, self.env.valid_action_mask()
                )
                action_name = ACTIONS[action_index]
                next_obs, reward, done, info = await self.env.step(action_index)
                next_features = flatten_obs(next_obs)
                (
                    gold_delta,
                    loot_delta,
                    market_pnl,
                    quest_reward,
                    quest_intrinsic,
                    rnd_bonus,
                ) = self.transition_targets(next_obs, next_features, info, action_name)

                self.farm.agent.store(
                    {
                        "state": self.features,
                        "action": action_index,
                        "reward": reward,
                        "next_state": next_features,
                        "done": done,
                        "gold_delta": gold_delta,
                        "loot_delta": loot_delta,
                        "market_pnl": market_pnl,
                        "quest_reward": quest_reward,
                        "quest_intrinsic": quest_intrinsic,
                        "rnd_bonus": rnd_bonus,
                    }
                )
                self.farm.agent.t_step += 1
                # learn() self-gates on minibatch fill (#222).
                self.farm.agent.learn()

                self.features = next_features
                self.obs = next_obs
                self.score = next_obs["score_raw"]
                self.steps += 1
                self.farm.steps += 1
                if self.score > self.farm.last_best_score:
                    self.farm.last_best_score = self.score
                    # Snapshot immediately so the saved best never lags the
                    # achieving step (#327): the boundary checkpoint would
                    # otherwise write (possibly degraded) later weights under
                    # this peak's score metadata.
                    self.farm.agent.best_score = self.score
                    self.farm.agent.save_weights(self.farm.args.best_weights)
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

    print(
        f"[torch-farm] {args.agents} agents sharing one DQN, {N_ACTIONS} actions, "
        f"{args.steps or 'unlimited'} total steps"
    )
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
    parser = argparse.ArgumentParser(
        description="Shared-policy multi-agent Torch trainer."
    )
    parser.add_argument(
        "--agents", type=int, default=4, help="concurrent agents (default 4)"
    )
    parser.add_argument(
        "--name-prefix", default="TorchFarm", help="fresh character name prefix"
    )
    parser.add_argument("--url", default="ws://localhost:8765")
    parser.add_argument(
        "--steps", type=int, default=0, help="total shared steps; 0 runs until stopped"
    )
    parser.add_argument(
        "--save-every",
        type=int,
        default=500,
        help="shared checkpoint interval in steps",
    )
    parser.add_argument(
        "--weights", default=None, help="shared current checkpoint path"
    )
    parser.add_argument("--best-weights", default=None, help="best checkpoint path")
    args = parser.parse_args()
    if args.agents < 1:
        parser.error("--agents must be >= 1")
    if args.steps < 0 or args.save_every < 1:
        parser.error("--steps must be >= 0 and --save-every must be >= 1")
    here = os.path.dirname(os.path.abspath(__file__))
    args.weights = (
        os.path.abspath(args.weights)
        if args.weights
        else os.path.join(here, "ml_farm_weights.json")
    )
    args.best_weights = (
        os.path.abspath(args.best_weights)
        if args.best_weights
        else os.path.join(here, "ml_farm_best.json")
    )
    return args


if __name__ == "__main__":
    asyncio.run(main_async(parse_args()))
