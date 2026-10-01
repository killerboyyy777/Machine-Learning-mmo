"""Shared-policy multi-agent Torch trainer for Text MMO.

Unlike launching several copies of dqn_agent.py, this process owns one DQN
and one checkpoint writer. Four (or more) WebSocket environments run as
asyncio tasks; each completed environment step is applied to the same replay
buffer and policy in the event loop, so no model updates are lost to
last-writer-wins checkpoint races.

Resume state: the weights file carries the model, and a sidecar JSON file
next to it (`<weights>.farm.json`) carries the farm step counters. --steps
is a LIFETIME total across restarts, not a per-run budget: resuming with
the same --steps value after a completed run performs zero further steps;
pass a larger total to continue training.

Run from the repository root:
    python torch_agents/torch_farm.py --agents 4 --steps 1000000
"""

import argparse
import asyncio
import json
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
    CONNECTION_ERRORS,
    N_ACTIONS,
    QUEST_DELVER_REWARD_POINTS,
    QUEST_REWARD_POINTS,
    QUESTS,
    TextMMOEnv,
    flatten_obs,
    market_net,
    quest_charm_net,
    reset_with_retry,
    safe_spread_rooms,
)
from reward_dsl import RewardFormulaError, compile_formula, reference


class TorchFarm:
    def __init__(self, args):
        self.args = args
        self.stop = asyncio.Event()
        self.steps = 0
        self.last_save_step = -1
        self.last_best_score = float("-inf")
        self.agent = TorchDQNAgent(
            name=args.name_prefix + "Shared",
            url=args.url,
            td_norm=args.td_norm,
            td_clip=args.td_clip,
        )
        loaded = self.agent.load_weights(args.weights)
        if not loaded and args.best_weights and os.path.exists(args.best_weights):
            loaded = self.agent.load_weights(args.best_weights)
        if loaded:
            # Seed from the restored peak: score > -inf is always true on
            # the first step, which used to clobber the best file with an
            # early worse score (#378).
            self.last_best_score = self.agent.best_score
        self._farm_state_path = args.weights + ".farm.json"
        try:
            with open(self._farm_state_path) as f:
                _st = json.load(f)
            if not isinstance(_st, dict):
                raise TypeError("sidecar root must be an object")
            # Persisted counters survive restarts: --steps is a lifetime
            # total, so resume continues the count instead of restarting it.
            self.steps = int(_st.get("steps", 0))
            self.last_save_step = int(_st.get("last_save_step", -1))
        except (OSError, ValueError, TypeError, AttributeError):
            pass
        # Spawn spread (#417): round-robin over the safe room pool so the
        # farm does not train 16 identical first observations. Computed once
        # (world.json is static for a process) and reported in the banner.
        self.spawn_rooms = safe_spread_rooms() if args.spawn_spread else []
        self.runners = []

    def spawn_room_for(self, index):
        """Stable per-runner spread target. Index-based, not random: a
        relaunched farm spreads the same way, so a trainee that wanders off
        its starting room and comes back has not lost its own identity."""
        if not self.spawn_rooms:
            return None
        return self.spawn_rooms[index % len(self.spawn_rooms)]

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
        # Sidecar write leaves the event loop: blocking open() in async
        # context trips ASYNC230 and stalls runners on slow disks.

        def _write_state():
            try:
                with open(self._farm_state_path, "w") as f:
                    json.dump(
                        {"steps": self.steps, "last_save_step": self.last_save_step}, f
                    )
            except OSError:
                pass

        await asyncio.to_thread(_write_state)


class Runner:
    def __init__(self, index, farm):
        self.farm = farm
        self.index = index
        self.name = f"{farm.args.name_prefix}{index}"
        self.spawn_room = farm.spawn_room_for(index)
        self.env = TextMMOEnv(
            self.name,
            url=farm.args.url,
            spawn_room=self.spawn_room,
            reward_formula=farm.args.reward_formula,
        )
        self.features = None
        self.obs = None
        self.prev_gold = 0.0
        self.prev_inventory = set()
        self.prev_orders = []
        self.score = 0.0
        self.steps = 0
        # Training health (#418): a runner that wedges must say so. The
        # pilot lost 4 of 16 trainees with no log line and no entry in the
        # summary, which silently deflated the shared step rate.
        self.reset_retries = 0
        self.last_error = None

    def log_retry(self, attempt, delay, error):
        self.reset_retries += 1
        self.last_error = f"reset attempt {attempt}: {error}"
        print(
            f"[torch-farm] {self.name}: reset failed (attempt {attempt}): "
            f"{error}; retrying in {delay:.1f}s",
            flush=True,
        )

    async def reset(self):
        # Jitter spreads a fleet-wide reconnect: without it all 16 sockets
        # retry on the same second and hammer a server that is still
        # coming up.
        self.obs = await reset_with_retry(
            self.env,
            name=self.name,
            on_retry=self.log_retry,
            jitter=min(0.5, 0.05 * self.index),
            should_stop=self.farm.stop.is_set,
        )
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
        # No per-material loop (unlike an earlier farm revision): the
        # single-agent bonus is deliberately NOT farmable per pickup
        # (dqn_agent train(): accept + progress transitions only), and the
        # farm docstring promises that same objective (#378).

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
        try:
            await self.reset()
            while not self.farm.should_stop():
                epsilon = self.farm.agent._epsilon()
                action_index = self.farm.agent.act(
                    self.features, epsilon, self.env.valid_action_mask()
                )
                action_name = ACTIONS[action_index]
                try:
                    next_obs, reward, done, info = await self.env.step(action_index)
                except CONNECTION_ERRORS as e:
                    # Reached when the socket dies between actions in a way
                    # step() did not absorb: a refused write, a connect that
                    # timed out, or a send on the None socket a failed
                    # reconnect left behind. A clean server restart or kick
                    # arrives as ConnectionClosed, which step() turns into
                    # done=True, so the reset below handles that case.
                    # Store nothing either way: a dead connection has no
                    # honest next_state.
                    self.last_error = f"step lost the connection: {e}"
                    print(
                        f"[torch-farm] {self.name}: {self.last_error}; "
                        f"reconnecting",
                        flush=True,
                    )
                    await self.reset()
                    continue
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
        except asyncio.CancelledError:
            raise
        except Exception as e:
            # Log before giving up, so the runner's exit is never the silent
            # one #418 was about -- the gather() summary cannot report a
            # failure it never sees.
            self.last_error = f"{type(e).__name__}: {e}"
            print(
                f"[torch-farm] {self.name}: runner stopped after " f"{self.last_error}",
                flush=True,
            )
            raise
        finally:
            await self.env.close()


def _is_stop(result):
    """True when a runner ended because the farm was asked to stop, not
    because it broke. reset_with_retry re-raises the connection error it was
    retrying when should_stop fires, and task.cancel() raises CancelledError,
    so both land in the gather results as exceptions (#420)."""
    return isinstance(result, asyncio.CancelledError) or (
        isinstance(result, CONNECTION_ERRORS) and result is not None
    )


def _is_crash(result):
    return isinstance(result, BaseException) and not _is_stop(result)


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
    if farm.spawn_rooms:
        print(
            f"[torch-farm] spawn spread over {len(farm.spawn_rooms)} safe "
            f"rooms, first {len(farm.runners)}: "
            + ", ".join(r.spawn_room for r in farm.runners)
        )
    else:
        print(
            "[torch-farm] spawn spread off: every agent starts in the "
            "server's start room"
        )
    tasks = [asyncio.create_task(r.run(), name=r.name) for r in farm.runners]
    try:
        # return_exceptions: gather() otherwise reports only the first
        # failure and drops the rest, which is how four of sixteen pilot
        # runners disappeared with no trace (#418).
        await asyncio.gather(*tasks, return_exceptions=True)
    except KeyboardInterrupt:
        farm.stop.set()
    finally:
        farm.stop.set()
        for task in tasks:
            if not task.done():
                task.cancel()
        results = await asyncio.gather(*tasks, return_exceptions=True)
        await farm.checkpoint(force=True)
        # A stop is not a crash: task.cancel() and the should_stop re-raise
        # both surface as exceptions here, and counting them would deflate
        # runners_alive on every clean Ctrl-C or step-limit exit (#420).
        crashed = [r for r in results if _is_crash(r)]
        stopped = sum(1 for r in results if _is_stop(r))
        print(
            f"[torch-farm] finished: total_steps={farm.steps} "
            f"runners_alive={len(farm.runners) - len(crashed)} "
            f"crashed={len(crashed)} stopped={stopped}"
        )
        for runner, result in zip(farm.runners, results):
            line = (
                f"  {runner.name}: steps={runner.steps} "
                f"score={runner.score:.2f} "
                f"reset_retries={runner.reset_retries}"
            )
            if _is_crash(result):
                line += f" CRASHED {type(result).__name__}: {result}"
            elif runner.last_error:
                line += f" (last issue: {runner.last_error})"
            print(line)


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
    parser.add_argument(
        "--spawn-spread",
        action="store_true",
        default=True,
        help="spread trainees across safe rooms on login/respawn (default on)",
    )
    parser.add_argument(
        "--no-spawn-spread",
        dest="spawn_spread",
        action="store_false",
        help="every trainee starts in the server's start room (pre-#417 behavior)",
    )
    parser.add_argument(
        "--no-td-norm",
        dest="td_norm",
        action="store_false",
        default=True,
        help="skip per-batch advantage normalization + clipping (pre-#416b)",
    )
    parser.add_argument(
        "--td-clip",
        type=float,
        default=5.0,
        help="clip bound on the normalized advantage (#416b)",
    )
    parser.add_argument(
        "--reward-formula",
        default=None,
        metavar="EXPR",
        help="custom reward expression (#68), e.g. "
        "'0.5*xp_delta + 1.2*profit - 0.1*deaths'. Replaces the score "
        "reward entirely, so a formula that omits a term omits its "
        "shaping too. Signals:\n" + reference(),
    )
    args = parser.parse_args()
    if args.agents < 1:
        parser.error("--agents must be >= 1")
    if args.steps < 0 or args.save_every < 1:
        parser.error("--steps must be >= 0 and --save-every must be >= 1")
    if args.td_clip <= 0:
        parser.error("--td-clip must be > 0")
    if args.reward_formula:
        try:
            compile_formula(args.reward_formula)
        except RewardFormulaError as e:
            parser.error(f"--reward-formula: {e}")
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
