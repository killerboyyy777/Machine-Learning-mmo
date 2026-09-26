"""
Long-running, multi-bot ML training farm.

N agents (the "amount of concurrent bots playing" is configurable) share one
linear-Q policy and train it against the live server, every agent as its own
WebSocket connection / character. Training keeps going until you stop it
(Ctrl+C / SIGTERM).

The best version is always kept:
  - ml_weights.json  -- whatever the farm is currently training (its "latest"),
  - ml_best.json     -- an immutable-in-practice snapshot of the weights the
                        moment the farm achieved its best performance so far.
                        Re-promotion only happens when a later checkpoint is
                        strictly better, so a bad run or crash can never
                        destroy your best policy. Restarting the farm with the
                        same bot names continues the same persistent characters.

Fitness is each bot's rolling average reward-per-step (the same metric the
single-run ml_client prints). The evaluation task periodically looks at the
most-fit bot; if it beats the stored best, the current weights are promoted.

Run:
    python3 server.py                      # terminal 1
    python3 ml_botfarm.py --bots 4         # terminal 2, until Ctrl+C

Options:
    --bots N            how many concurrent bot characters to play (default 4)
    --name-prefix P     names become P0, P1, ... (default "FarmBot"); reuse the
                        same prefix to keep training the same persistent characters
    --steps N           stop after N total env-steps across all bots (0 = forever,
                        the default)
    --checkpoint-every N   env-steps between best-model evaluations (default 200)
    --eval-every S      seconds between evaluations (default 5)
"""

import argparse
import asyncio
import json
import os
import random
import signal
import time

import websockets

try:
    from .ml_env import TextMMOEnv, OBS_SIZE, N_ACTIONS, ACTIONS, flatten_obs
    from .ml_client import LinearQAgent
    # Scripted baselines live in ml/plugins now; imported here so the
    # farm CLI, --scripted roles, and existing import sites keep working.
    from .plugins.scripted import (
        SCRIPTED_POLICIES, SCRIPTED_NAMES, ScriptedPolicy,
        GatherSellPolicy, DungeonClearerPolicy, MarketFlipperPolicy,
        CommissionerPolicy,
    )
except ImportError:
    # Running as a script (python ml/ml_botfarm.py): no parent package.
    from ml_env import TextMMOEnv, OBS_SIZE, N_ACTIONS, ACTIONS, flatten_obs
    from ml_client import LinearQAgent
    from plugins.scripted import (
        SCRIPTED_POLICIES, SCRIPTED_NAMES, ScriptedPolicy,
        GatherSellPolicy, DungeonClearerPolicy, MarketFlipperPolicy,
        CommissionerPolicy,
    )

_HERE = os.path.dirname(os.path.abspath(__file__))
WEIGHTS_FILE = os.path.join(_HERE, "ml_weights.json")
BEST_FILE = os.path.join(_HERE, "ml_best.json")

# Reconnect backoff for a down/restarting server (#330): capped exponential
# so the farm survives a brief outage instead of dying on the first error.
RESET_BACKOFF_BASE = 1.0
RESET_BACKOFF_MAX = 30.0


def load_json(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def save_json(path, data):
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(data, f, indent=2)
    os.replace(tmp, path)


def parse_roles(spec, bots):
    """Parse a --roles SPEC into a role list of length `bots`.

    SPEC is comma-separated ``role:count`` entries (e.g.
    ``gather:8,dungeon:4,market:7,commissioner:2``) plus an optional
    ``flex:role`` that fills any bots not covered by explicit counts. Without
    ``flex``, leftover bots round-robin over SCRIPTED_NAMES (the current
    default). Raises ValueError on unknown/duplicate roles, bad counts, or
    counts exceeding --bots.
    """
    counts = []
    seen = set()
    flex = None
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        if ":" not in part:
            raise ValueError(f"--roles: expected role:count, got {part!r}")
        name, _, cstr = part.partition(":")
        name = name.strip()
        cstr = cstr.strip()
        if name == "flex":
            if flex is not None:
                raise ValueError("--roles: only one 'flex' entry is allowed")
            if cstr not in SCRIPTED_POLICIES:
                raise ValueError(f"--roles: unknown flex role {cstr!r}")
            flex = cstr
            continue
        if name not in SCRIPTED_POLICIES:
            raise ValueError(
                f"--roles: unknown role {name!r} "
                f"(valid: {', '.join(SCRIPTED_NAMES)})"
            )
        if name in seen:
            raise ValueError(f"--roles: duplicate role {name!r}")
        seen.add(name)
        try:
            n = int(cstr)
        except ValueError:
            raise ValueError(f"--roles: invalid count {cstr!r} for {name!r}") from None
        if n < 0:
            raise ValueError(f"--roles: negative count for {name!r}")
        counts.append((name, n))
    total = sum(n for _, n in counts)
    if total > bots:
        raise ValueError(
            f"--roles: explicit counts sum to {total}, exceeds --bots {bots}"
        )
    roles = [name for name, n in counts for _ in range(n)]
    remaining = bots - total
    if remaining:
        if flex is not None:
            roles.extend([flex] * remaining)
        else:
            roles.extend(
                SCRIPTED_NAMES[i % len(SCRIPTED_NAMES)] for i in range(total, bots)
            )
    return roles


def build_role_list(args):
    """Resolve the per-bot role list from --roles or the legacy --scripted
    flag. Returns a list of length args.bots (None entries = RL training)."""
    if getattr(args, "roles", None):
        return parse_roles(args.roles, args.bots)
    mode = getattr(args, "scripted", "none")
    if not mode or mode == "none":
        return [None] * args.bots
    if mode in SCRIPTED_POLICIES:
        return [mode] * args.bots
    return [SCRIPTED_NAMES[i % len(SCRIPTED_NAMES)] for i in range(args.bots)]


class Farm:
    """Shared state + policy. All bots update the same agent; only one event
    loop ever touches it, so no locking is needed (coordinated stepping)."""

    def __init__(self, args):
        self.args = args
        self.stop = asyncio.Event()
        self.steps = 0
        self.epsilon = args.epsilon_start
        self.agent = LinearQAgent(OBS_SIZE, N_ACTIONS)
        self.bots = []
        self.role_list = build_role_list(args)

        self.best_fitness = float("-inf")
        self.best_score = float("-inf")
        loaded = None
        if os.path.exists(args.weights):
            if self.agent.load(args.weights):
                loaded = args.weights
        if loaded is None and os.path.exists(BEST_FILE):
            if self.agent.load(BEST_FILE):
                loaded = BEST_FILE
        if loaded:
            print(f"Resumed policy from {loaded}")
            meta = load_json(BEST_FILE, None)
            if meta:
                self.best_fitness = meta.get("fitness", float("-inf"))
                self.best_score = meta.get("best_score", float("-inf"))
        else:
            print("Starting from fresh (zeroed) weights")
        self.next_checkpoint = max(1, args.checkpoint_every)

    def epsilon_now(self):
        """Decay shared exploration with total farm steps (0.15s of delay per
        env-step keeps a handful of bots far apart even at low eps)."""
        progress = min(1.0, self.agent.training_steps / max(1, self.args.epsilon_decay_steps))
        return self.args.epsilon_start + (self.args.epsilon_end - self.args.epsilon_start) * progress

    def promote(self, fitness, best_score):
        self.best_fitness = fitness
        self.best_score = best_score
        save_json(BEST_FILE, {
            "weights": self.agent.weights,
            "bias": self.agent.bias,
            "fitness": round(fitness, 4),
            "best_score": round(best_score, 2),
            "steps": self.steps,
            "training_steps": self.agent.training_steps,
            "bots": self.args.bots,
            "saved": time.strftime("%Y-%m-%d %H:%M:%S"),
        })
        print(f"[best] NEW BEST fitness={fitness:.4f} score={best_score:.2f} "
              f"at step {self.steps} -> {BEST_FILE}")

    def save_weights(self):
        # Full LinearQAgent format (weights, bias, training_steps, dims,
        # version) so restarts resume the epsilon schedule (#231: the old
        # weights+bias-only blob reset training_steps to 0 on every load).
        self.agent.save(WEIGHTS_FILE)
        print(f"Saved current weights to {WEIGHTS_FILE}")


class BotRunner:
    """One character: one WebSocket env, one continuous training loop."""

    def __init__(self, index, farm):
        self.env = TextMMOEnv(f"{farm.args.name_prefix}{index}", url=farm.args.url)
        self.name = f"{farm.args.name_prefix}{index}"
        self.farm = farm
        self.features = None
        self.recent_rewards = []
        self.score = 0.0
        self.total_steps = 0
        role = farm.role_list[index]
        self.policy = SCRIPTED_POLICIES[role]() if role else None

    def fitness(self):
        r = self.recent_rewards
        if len(r) < self.farm.args.reward_window // 2:
            return None  # not enough recent data to judge yet
        return sum(r) / len(r)

    async def reset(self):
        obs = await self.env.reset()
        self.features = flatten_obs(obs)
        self.score = obs["score_raw"]
        self.recent_rewards = []

    async def _reset_with_retry(self):
        """Connect with backoff + re-login so a down/restarting server never
        kills the whole farm (#330). reset() raises ConnectionError when the
        server is unreachable; retry with capped exponential backoff until
        the farm is stopped (Ctrl+C still interrupts)."""
        attempt = 0
        while not self.farm.stop.is_set():
            try:
                await self.reset()
                return
            except (ConnectionError, OSError, websockets.ConnectionClosed) as e:
                attempt += 1
                delay = min(RESET_BACKOFF_MAX,
                            RESET_BACKOFF_BASE * (2 ** min(attempt - 1, 5)))
                print(f"[farm] {self.name}: server unreachable "
                      f"({e.__class__.__name__}); reconnecting in {delay:.1f}s")
                await asyncio.sleep(delay)

    async def run(self):
        await self._reset_with_retry()
        if self.policy is not None:
            await self.run_scripted()
            return
        while not self.farm.stop.is_set():
            if self.farm.args.steps and self.farm.steps >= self.farm.args.steps:
                self.farm.stop.set()
                break
            action = self.farm.agent.act(self.features, self.farm.epsilon_now(), self.env.valid_action_mask())
            try:
                next_obs, reward, done, info = await self.env.step(action)
            except (ConnectionError, OSError, websockets.ConnectionClosed):
                await self._reset_with_retry()
                continue
            next_features = flatten_obs(next_obs)
            self.farm.agent.update(self.features, action, reward, next_features, done)
            self.farm.agent.training_steps += 1

            self.features = next_features
            self.score = next_obs["score_raw"]
            self.recent_rewards.append(reward)
            if len(self.recent_rewards) > self.farm.args.reward_window:
                del self.recent_rewards[0]
            self.farm.steps += 1
            self.total_steps += 1

            if done:
                await self._reset_with_retry()
        await self.env.close()

    async def run_scripted(self):
        """Fixed-baseline loop: policy picks actions, no weights update.
        Rewards/scores are still tracked so RL runs can compare directly."""
        while not self.farm.stop.is_set():
            if self.farm.args.steps and self.farm.steps >= self.farm.args.steps:
                self.farm.stop.set()
                break
            action = self.policy.select(self.env)
            try:
                next_obs, reward, done, _info = await self.env.step(action)
            except (ConnectionError, OSError, websockets.ConnectionClosed):
                await self._reset_with_retry()
                continue
            self.features = flatten_obs(next_obs)
            self.score = next_obs["score_raw"]
            self.recent_rewards.append(reward)
            if len(self.recent_rewards) > self.farm.args.reward_window:
                del self.recent_rewards[0]
            self.farm.steps += 1
            self.total_steps += 1
            if done:
                await self._reset_with_retry()
        await self.env.close()


async def evaluator(farm):
    """Every --eval-every seconds, look at the most-fit bot and promote the
    current weights if it beat the stored best."""
    while not farm.stop.is_set():
        await asyncio.sleep(farm.args.eval_every)
        if farm.steps >= farm.next_checkpoint:
            cands = [b.fitness() for b in farm.bots]
            cands = [c for c in cands if c is not None]
            if cands and max(cands) > farm.best_fitness + 1e-6:
                best_bot = max(farm.bots, key=lambda b: b.fitness() or float("-inf"))
                farm.promote(max(cands), best_bot.score)
            while farm.next_checkpoint <= farm.steps:
                farm.next_checkpoint += max(1, farm.args.checkpoint_every)
        # periodic status line so you can watch the farm from the console
        per = "  ".join(f"{b.name}={b.score:.1f}" for b in farm.bots)
        best = farm.best_fitness if farm.best_fitness > float("-inf") else 0.0
        print(f"[farm] steps={farm.steps} eps={farm.epsilon_now():.2f} "
              f"best={best:.4f} bots: {per}")


def parse_args():
    p = argparse.ArgumentParser(description="Concurrent multi-bot ML training farm.")
    p.add_argument("--bots", type=int, default=4, help="concurrent bot characters (default 4)")
    p.add_argument("--name-prefix", default="FarmBot", help="bot names become <prefix><i>")
    p.add_argument("--url", default="ws://localhost:8765")
    p.add_argument("--steps", type=int, default=0, help="total env-steps, 0 = run until stopped")
    p.add_argument("--checkpoint-every", type=int, default=200)
    p.add_argument("--eval-every", type=float, default=5.0, help="seconds between evaluations")
    p.add_argument("--reward-window", type=int, default=200, help="rolling reward window for fitness")
    p.add_argument("--weights", default=WEIGHTS_FILE)
    p.add_argument("--scripted", default="none",
                   choices=("none", "gather", "dungeon", "market",
                            "commissioner", "quester", "crafter", "party_leader",
                            "mixed"),
                   help="run fixed behavior-tree baselines instead of training "
                        "(one role each, or round-robin with 'mixed')")
    p.add_argument("--roles", default=None,
                    help="explicit per-role counts, e.g. "
                         "gather:11,dungeon:4,market:7,commissioner:2,"
                         "flex:gather (flex fills the remainder); overrides "
                         "--scripted")
    p.add_argument("--epsilon-start", type=float, default=1.0)
    p.add_argument("--epsilon-end", type=float, default=0.05)
    p.add_argument("--epsilon-decay-steps", type=int, default=5000)
    args = p.parse_args()
    if args.bots < 1:
        p.error("--bots must be >= 1")
    try:
        build_role_list(args)
    except ValueError as e:
        p.error(str(e))
    args.weights = os.path.abspath(args.weights)
    return args


async def main():
    args = parse_args()
    farm = Farm(args)
    farm.bots = [BotRunner(i, farm) for i in range(args.bots)]

    loop = asyncio.get_running_loop()
    stop_setter = lambda: farm.stop.set()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop_setter)
        except (NotImplementedError, ValueError):
            pass  # platform doesn't support it; KeyboardInterrupt fallback below

    # Staggered start: each bot begins after a short delay so they experience
    # slightly different initial game states (different room, NPC layout, etc.).
    bot_tasks = []
    for i, b in enumerate(farm.bots):
        async def start_delayed(bot=b, idx=i):
            await asyncio.sleep(2 * idx)  # 2‑second delay per bot index
            await bot.run()
        bot_tasks.append(asyncio.create_task(start_delayed(), name=b.name))
    eval_task = asyncio.create_task(evaluator(farm))
    print(f"[farm] {args.bots} bots training on {args.url} — Ctrl+C to stop.")

    interrupted = False
    try:
        await asyncio.gather(*bot_tasks)
    except KeyboardInterrupt:
        print("\nStopping (KeyboardInterrupt)...")
        interrupted = True
        farm.stop.set()
    finally:
        if not farm.stop.is_set():
            farm.stop.set()
        await asyncio.gather(eval_task, return_exceptions=True)

    print("\n--- Farm summary ---")
    for b in farm.bots:
        print(f"  {b.name}: score={b.score:.2f} steps={b.total_steps}")
    print(f"  total steps: {farm.steps}")
    print(f"  best kept: {farm.best_fitness if farm.best_fitness > float('-inf') else 0.0:.4f} fitness, "
          f"score={farm.best_score if farm.best_score > float('-inf') else 0.0:.2f}")
    farm.save_weights()
    if farm.best_fitness > float("-inf"):
        print(f"  best policy snapshot: {BEST_FILE}")

    # Exit with code 1 if interrupted by Ctrl+C, else 0 (steps exhausted / normal end)
    raise SystemExit(1 if interrupted else 0)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (OSError, ConnectionError) as e:
        print(f"Could not reach the server: {e}")
        print("Start the engine first: python server.py")
