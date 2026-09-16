"""
A PyTorch-based Deep Q-Network (DQN) agent for the text MMO.
Trains on the same observation/action space as ml_env.py, using the
server's score as the reward signal (inherits the anti-grind variety/
diminishing-returns curve for free).

Key design:
  - Observation: same flattened vector as flatten_obs() in ml_env.py,
    including the 7-dim quest block (active/ready/has_charm/3 mats/giver_here)
  - Action space: same N_ACTIONS as in ml_env.py (includes market AND quest
    actions: quest_accept / quest_turn_in / craft_charm)
  - Reward: change in server score between steps (covers kills, room
    discovery, dungeon clears, crafting, market trades, quest turn-ins,
    assist payouts)
  - Quest model: two Town Guard repeatable quests are first-class --
    guard_charm (craft the Ancient Guardian Charm from 1x Treant Bark +
    1x Troll Hide + 1x Ectoplasm) and delver (clear dungeon floors), both
    accepted/turned in by the guard in Town Square for fixed XP + gold +
    score. The agent tracks each quest's stage from the observation,
    predicts imminent quest reward with its quest head, and gets a small
    intrinsic bonus the first time each stage transition fires per cycle
    (accept/craft-or-ready) to bootstrap the long chains.
  - Auxiliary heads: predict immediate gold gain, loot value, market P&L,
    and quest score from the observation, providing self-supervised
    representation learning. The loot target is quest-aware: charm-craft
    steps regress toward the charm's gold-terms net (turn-in gold minus
    mat merchant value -- negative, teaching the sell-mats-vs-quest
    profitability tradeoff) instead of the naive inventory proxy.
  - Auxiliary heads: predict immediate gold gain, loot value, market P&L,
    and quest score from the observation, providing self-supervised
    representation learning
  - Market tracking: monitors posted/buy/cancel orders and computes
    trading profit/loss signals for auxiliary shaping, with exact
    after-tax accounting (server takes TAX_RATE with a TAX_MINIMUM floor)
  - Architecture: 3-layer MLP with shared trunk + 5 output heads
    (Q-values, gold, loot, market-value, quest-value)
  - Training: experience replay; online TD update per step with
    auxiliary losses
  - Exploration: epsilon-greedy with linear decay, plus RND curiosity
  (frozen random target vs trained predictor; normalized prediction error
  rides the TD target with weight rnd_lambda, predictor trains separately)
- Save/load weights to torch_agents/ml_weights.json (torch.save format, separate
     from the JSON weights used by ml_client.py). Note: adding quest dims
     changed OBS_SIZE/N_ACTIONS, so checkpoints saved before quests need
     retraining (load warns instead of crashing on shape mismatch).
"""

import asyncio
import json
import random
import sys
from pathlib import Path

# Allow running as `python torch_agents/dqn_agent.py` from the repo root
# without extra PYTHONPATH setup: the Gym-style env lives in the sibling ml/
# folder.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "ml"))

import torch
import torch.nn as nn
import torch.optim as optim

from ml_env import (
    TextMMOEnv, ACTIONS, N_ACTIONS, OBS_SIZE, flatten_obs,
    market_tax, market_net, TAX_RATE, TAX_MINIMUM,
    QUESTS, QUEST_REWARD_POINTS, QUEST_DELVER_REWARD_POINTS,
    quest_stage, quest_charm_cost, quest_charm_net,
)


def _fmt_loss(v) -> str:
    """Format a loss component that is None while the replay buffer warms up."""
    return f"{v:.3f}" if v is not None else "warmup"


def checkpoint_version() -> dict:
    """Reproducibility metadata embedded in every checkpoint (#53): git
    SHA, config hash (server_config.json + ml/ml_config.json), obs/action
    dims, timestamp. All steps best-effort -- "unknown"/"missing" markers
    instead of crashes when git or the files are absent."""
    import hashlib
    import subprocess
    import time as _time
    sha = "unknown"
    try:
        out = subprocess.run(["git", "rev-parse", "HEAD"], capture_output=True,
                             text=True, timeout=5, check=False)
        sha = out.stdout.strip() or "unknown"
    except (OSError, subprocess.SubprocessError) as e:
        print(f"checkpoint_version: git lookup failed ({e}); using 'unknown'")
    here = Path(__file__).resolve().parent
    h = hashlib.sha256()
    for p in (here.parent / "server_config.json", here.parent / "ml" / "ml_config.json"):
        try:
            h.update(p.read_bytes())
        except OSError:
            h.update(f"missing:{p.name}".encode())
    return {
        "git_sha": sha,
        "config_hash": h.hexdigest()[:16],
        "obs_size": OBS_SIZE,
        "n_actions": N_ACTIONS,
        "saved_at": _time.strftime("%Y-%m-%d %H:%M:%S"),
    }

# ---------------------------------------------------------------------------
# Random Network Distillation (curiosity) -- see TorchDQNAgent below
# ---------------------------------------------------------------------------

class RNDNet(nn.Module):
    """Small MLP embedding observations into a k-dim curiosity space."""

    def __init__(self, obs_size: int, hidden: int = 128, out_dim: int = 32):
        super().__init__()
        self.net = nn.Sequential(
            nn.Linear(obs_size, hidden),
            nn.ReLU(),
            nn.Linear(hidden, out_dim),
        )

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        return self.net(x)


# ---------------------------------------------------------------------------
# Q-Network architecture with auxiliary heads + market value head
# ---------------------------------------------------------------------------

class DQN(nn.Module):
    """MLP with shared trunk + 5 output heads:

    - Q-head: N_ACTIONS Q-values (for RL)
    - Gold-head: 1 value (predict immediate gold gain)
    - Loot-head: 1 value (predict total loot value dropped)
    - Market-head: 1 value (predict immediate market P&L from buy/post/cancel)
    - Quest-head: 1 value (predict immediate quest score from a turn-in)

    All heads share the first two linear layers, then branch out. The quest
    head learns *what the quest is worth*: it regresses toward
    QUEST_REWARD_POINTS on turn-in steps and 0 otherwise, so the shared
    trunk must represent quest stage (active/ready/mats/giver) to minimize
    its error -- that representation then serves the Q-head for free.
    """

    def __init__(self, obs_size: int, n_actions: int, hidden: int = 128):
        super().__init__()
        self.trunk = nn.Sequential(
            nn.Linear(obs_size, hidden),
            nn.ReLU(),
            nn.Linear(hidden, hidden),
            nn.ReLU(),
        )
        self.q_head = nn.Linear(hidden, n_actions)
        self.gold_head = nn.Linear(hidden, 1)   # predicted gold this step
        self.loot_head = nn.Linear(hidden, 1)  # predicted loot value this step
        self.market_head = nn.Linear(hidden, 1)  # predicted market P&L this step
        self.quest_head = nn.Linear(hidden, 1)  # predicted quest score this step

    def forward(self, x: torch.Tensor) -> dict[str, torch.Tensor]:
        features = self.trunk(x)
        return {
            "q": self.q_head(features),
            "gold": self.gold_head(features).squeeze(-1),
            "loot": self.loot_head(features).squeeze(-1),
            "market": self.market_head(features).squeeze(-1),
            "quest": self.quest_head(features).squeeze(-1),
        }

    def get_q(self, x: torch.Tensor) -> torch.Tensor:
        return self.forward(x)["q"]


# ---------------------------------------------------------------------------
# Agent
# ---------------------------------------------------------------------------

class TorchDQNAgent:
    """DQN agent with auxiliary gold/loot/market/quest prediction heads.

    The agent learns the main RL task (maximize score via the server's
    anti-grind reward signal) while simultaneously predicting the
    immediate gold, loot, market P&L, and quest score from observations.
    The auxiliary losses shape a richer state representation for free.

    Key additions for market trading:
      • Market head predicts immediate P&L from market actions
      • Agent tracks posted/buy/cancel order counts in the observation,
        plus the live tax terms and its own listings' after-tax net
      • P&L targets use the exact server tax formula (via market_net):
        buys cost full price, filled listings net price minus tax
      • Auxiliary market loss shapes the agent toward profitable trading
      • Market reward signal is added to the main score reward with
        a small weighting so it guides without overwhelming the anti-grind curve

    Key additions for quests (Town Guard repeatable quest):
      • Quest head predicts imminent quest score (QUEST_REWARD_POINTS on a
        turn-in step, 0 otherwise), forcing the trunk to encode quest stage
      • Observation carries quest_active / quest_ready / quest_has_charm /
        3 mat flags / quest_giver_here; see ml_env.QUESTS for the symbolic
        quest definition (giver, inputs, fixed repeatable reward)
      • Turn-ins already pay score via the server, so the main TD target
        learns quest value directly; the quest head is auxiliary shaping
    """

    def __init__(
        self,
        name: str = "TorchBot",
        url: str = "ws://localhost:8765",
        hidden: int = 128,
        alpha: float = 0.001,
        gamma: float = 0.99,
        epsilon_start: float = 1.0,
        epsilon_end: float = 0.05,
        epsilon_decay_steps: int = 50000,
        replay_size: int = 10000,
        batch_size: int = 32,
        aux_lambda: float = 0.3,  # weight for aux losses vs TD loss
        market_lambda: float = 0.1,  # weight for market auxiliary vs TD
        quest_lambda: float = 0.3,  # weight for quest auxiliary vs TD
        intrinsic_lambda: float = 1.0,  # weight for quest exploration bonus
        intrinsic_accept: float = 1.0,  # bonus on quest accept (per cycle)
        intrinsic_progress: float = 2.0,  # bonus on charm craft / delver ready
        rnd_lambda: float = 0.1,  # weight for RND curiosity bonus
        rnd_hidden: int = 128,  # RND embedding width
        rnd_dim: int = 32,  # RND embedding size
        rnd_lr: float = 1e-3,  # RND predictor learning rate
        rnd_ema: float = 0.01,  # running-stat momentum for bonus norm
    ):
        self.name = name
        self.url = url
        self.device = torch.device("cuda" if torch.cuda.is_available() else "cpu")

        self.q = DQN(OBS_SIZE, N_ACTIONS, hidden=hidden).to(self.device)
        self.target = DQN(OBS_SIZE, N_ACTIONS, hidden=hidden).to(self.device)
        self.target.load_state_dict(self.q.state_dict())
        self.target.eval()

        self.optimizer = optim.Adam(self.q.parameters(), lr=alpha)

        self.gamma = gamma
        self.epsilon_start = epsilon_start
        self.epsilon_end = epsilon_end
        self.epsilon_decay_steps = epsilon_decay_steps
        self.replay_size = replay_size
        self.batch_size = batch_size
        self.aux_lambda = aux_lambda
        self.market_lambda = market_lambda
        self.quest_lambda = quest_lambda
        # Quest-gated exploration bonus (step 1): small intrinsic reward on
        # first-time stage transitions per quest cycle (accept, charm craft /
        # delver ready). The quest state machine enforces progression, so the
        # bonus can't be farmed without doing the work; turn-ins already pay
        # real score, so they get no bonus. Keeps the long craft/delve chains
        # discoverable under epsilon-greedy without overwhelming the score.
        self.intrinsic_lambda = intrinsic_lambda
        self.intrinsic_accept = intrinsic_accept
        self.intrinsic_progress = intrinsic_progress

        # RND curiosity (Random Network Distillation): a frozen random
        # target net plus a predictor trained on visited states. Prediction
        # error is high in rarely-seen areas (dungeons, crafting chains)
        # and decays as they become familiar -- a pure exploration bonus
        # that complements the quest-gated bonus above. Normalized by a
        # running error std so the scale stays stable through training.
        self.rnd_lambda = rnd_lambda
        self.rnd_ema = rnd_ema
        self.rnd_target = RNDNet(OBS_SIZE, hidden=rnd_hidden, out_dim=rnd_dim).to(self.device)
        self.rnd_target.eval()
        for p in self.rnd_target.parameters():
            p.requires_grad_(False)
        self.rnd_pred = RNDNet(OBS_SIZE, hidden=rnd_hidden, out_dim=rnd_dim).to(self.device)
        self.rnd_opt = optim.Adam(self.rnd_pred.parameters(), lr=rnd_lr)
        self._rnd_lr = rnd_lr
        self._rnd_mean = 0.0
        self._rnd_var = 1.0

        self.replay: list = [None] * replay_size
        self.replay_idx = 0

        self.t_step = 0
        self.learn_step = 0
        self.best_score: float = -float("inf")
        self.ckpt_version = None  # version dict of the loaded checkpoint

        # Tracking per-episode for auxiliary supervision
        self._prev_gold: float = 0.0
        self._prev_loot_value: float = 0.0
        self._prev_inventory_ids: set[str] | None = None

        # Market tracking state
        self._posted_orders: int = 0
        self._bought_items: int = 0
        self._cancelled_orders: int = 0
        self._prev_money_spent: float = 0.0
        self._prev_gold_earned: float = 0.0
        # Own standing sell orders from the last step (env info), used to
        # spot fills: a vanished order means someone bought it, netting us
        # market_net(price) after the server's tax cut.
        self._prev_own_orders: list = []

    # ---- epsilon ------------------------------------------------------------

    def _epsilon(self) -> float:
        progress = min(1.0, self.t_step / max(1, self.epsilon_decay_steps))
        return self.epsilon_start + (self.epsilon_end - self.epsilon_start) * progress

    # ---- action selection ---------------------------------------------------

    def act(self, features: list[float], epsilon: float | None = None, mask: list[int] | None = None) -> int:
        if epsilon is None:
            epsilon = self._epsilon()
        valid = [a for a in range(N_ACTIONS) if mask is None or mask[a]]
        if not valid:
            valid = list(range(N_ACTIONS))
        if random.random() < epsilon:
            return random.choice(valid)
        with torch.no_grad():
            x = torch.tensor([features], dtype=torch.float32, device=self.device)
            q_vals = self.q.get_q(x).squeeze(0)
            best = max(valid, key=lambda a: float(q_vals[a].item()))
        return int(best)

    # ---- RND curiosity ------------------------------------------------------

    def _rnd_error(self, x: torch.Tensor) -> torch.Tensor:
        """Per-row mean-squared prediction error (no grad)."""
        with torch.no_grad():
            target = self.rnd_target(x)
        pred = self.rnd_pred(x)
        return ((pred - target) ** 2).mean(dim=1)

    def rnd_bonus(self, features: list[float]) -> float:
        """Normalized curiosity bonus for one observation. Updates the
        running error stats online, so repeated states pay less."""
        x = torch.tensor([features], dtype=torch.float32, device=self.device)
        raw = float(self._rnd_error(x).item())
        self._rnd_mean += self.rnd_ema * (raw - self._rnd_mean)
        self._rnd_var += self.rnd_ema * ((raw - self._rnd_mean) ** 2 - self._rnd_var)
        std = max(1e-4, self._rnd_var ** 0.5)
        return max(0.0, (raw - self._rnd_mean) / std)

    def update_rnd(self, states: torch.Tensor) -> float:
        """One predictor step toward the frozen target on a batch of
        states. Returns the mean predictor loss."""
        self.rnd_pred.train()
        self.rnd_opt.zero_grad()
        with torch.no_grad():
            target = self.rnd_target(states)
        loss = nn.functional.mse_loss(self.rnd_pred(states), target)
        loss.backward()
        self.rnd_opt.step()
        return float(loss.detach())

    # ---- storage ------------------------------------------------------------

    def store(self, transition: dict) -> None:
        self.replay[self.replay_idx] = transition
        self.replay_idx = (self.replay_idx + 1) % self.replay_size

    # ---- auxiliary supervision from environment ---------------------------

    def _compute_auxiliary(self, transition: dict) -> tuple[torch.Tensor, torch.Tensor, torch.Tensor, torch.Tensor]:
        """Return (gold, loot, market, quest) targets for this transition.

        gold: delta in player gold
        loot: approximated from inventory value changes
        market: P&L from the step's market activity
        quest: QUEST_REWARD_POINTS if this step turned the guard quest in,
            else 0.0 (regressing toward this forces quest-stage encoding)
        """
        t = transition
        gold_target = float(t.get("gold_delta", 0.0))
        loot_target = float(t.get("loot_delta", 0.0))

        # Market target: compute P&L from the step's market activity.
        # We approximate using the change in player gold that can be attributed
        # to market operations (buying vs selling).  If the env doesn't expose
        # a breakdown we fall back to 0.0 so learning still occurs.
        market_target = float(t.get("market_pnl", 0.0))
        quest_target = float(t.get("quest_reward", 0.0))
        return gold_target, loot_target, market_target, quest_target

    # ---- learning -----------------------------------------------------------

    def learn(self) -> dict[str, float | None]:
        """One learning step: sample a minibatch from replay and perform
        a TD update with auxiliary gold/loot/market/quest prediction heads.

        TD target uses shaped reward r = r_score + λi * r_intrinsic, where
        r_intrinsic is the quest-gated exploration bonus stored per
        transition. Loss = L_TD + α * (L_gold + L_loot) + β * L_market
        + γq * L_quest, with α/β/γq = aux/market/quest lambdas.
        Returns a dict of loss components for logging."""
        # Buffer warm-up
        if self.t_step < self.replay_size:
            return {"td": None, "gold": None, "loot": None, "market": None,
                    "quest": None, "rnd": None}

        # Sample minibatch
        available = [i for i, t in enumerate(self.replay) if t is not None]
        if len(available) < self.batch_size:
            return {"td": None, "gold": None, "loot": None, "market": None,
                    "quest": None, "rnd": None}
        indices = random.sample(available, self.batch_size)
        batch = [self.replay[i] for i in indices if self.replay[i] is not None]

        if len(batch) < self.batch_size:
            return {"td": None, "gold": None, "loot": None, "market": None,
                    "quest": None, "rnd": None}

        # ---- build tensors from batch ----
        states = torch.tensor(
            [b["state"] for b in batch], dtype=torch.float32, device=self.device
        )
        actions = torch.tensor(
            [b["action"] for b in batch], dtype=torch.int64, device=self.device
        )
        rewards = torch.tensor(
            [b["reward"] for b in batch], dtype=torch.float32, device=self.device
        )
        next_states = torch.tensor(
            [b["next_state"] for b in batch], dtype=torch.float32, device=self.device
        )
        dones = torch.tensor(
            [b["done"] for b in batch], dtype=torch.float32, device=self.device
        )

        # ---- auxiliary targets ----
        gold_targets = torch.tensor(
            [b.get("gold_delta", 0.0) for b in batch], dtype=torch.float32, device=self.device
        )
        loot_targets = torch.tensor(
            [b.get("loot_delta", 0.0) for b in batch], dtype=torch.float32, device=self.device
        )
        market_targets = torch.tensor(
            [b.get("market_pnl", 0.0) for b in batch], dtype=torch.float32, device=self.device
        )
        quest_targets = torch.tensor(
            [b.get("quest_reward", 0.0) for b in batch], dtype=torch.float32, device=self.device
        )
        intrinsic_targets = torch.tensor(
            [b.get("quest_intrinsic", 0.0) for b in batch], dtype=torch.float32, device=self.device
        )
        rnd_targets = torch.tensor(
            [b.get("rnd_bonus", 0.0) for b in batch], dtype=torch.float32, device=self.device
        )

        # ---- current Q-values for the actions taken ----
        # (forward() returns a dict of heads; Q-values live under "q")
        q_vals = self.q.get_q(states).gather(1, actions.unsqueeze(1)).squeeze(1)

        # ---- TD target on the shaped reward ----
        # r = score + quest-intrinsic + RND curiosity (both exploration
        # bonuses ride the TD target; the predictor itself trains below).
        shaped = (rewards + self.intrinsic_lambda * intrinsic_targets
                  + self.rnd_lambda * rnd_targets)
        with torch.no_grad():
            target_q = self.target.get_q(next_states).max(1)[0]
            td_targets = shaped + self.gamma * target_q * (1.0 - dones)

        # ---- TD loss (Huber) ----
        td_loss = nn.functional.smooth_l1_loss(q_vals, td_targets)

        # ---- auxiliary losses ----
        # Forward pass through all heads
        heads = self.q(states)  # dict {"q":..., "gold":..., "loot":..., "market":..., "quest":...}
        pred_gold = heads["gold"]
        pred_loot = heads["loot"]
        pred_market = heads["market"]
        pred_quest = heads["quest"]

        gold_loss = nn.functional.l1_loss(pred_gold, gold_targets)
        loot_loss = nn.functional.l1_loss(pred_loot, loot_targets)
        market_loss = nn.functional.l1_loss(pred_market, market_targets)
        quest_loss = nn.functional.l1_loss(pred_quest, quest_targets)

        # ---- total loss ----
        total_loss = (td_loss + self.aux_lambda * (gold_loss + loot_loss)
                      + self.market_lambda * market_loss + self.quest_lambda * quest_loss)

        self.optimizer.zero_grad()
        total_loss.backward()
        self.optimizer.step()

        # RND predictor chase: fit visited states toward the frozen target
        # (own optimizer -- curiosity representation stays independent of
        # the Q-value trunk). Skipped when curiosity is disabled.
        rnd_loss = self.update_rnd(states) if self.rnd_lambda else 0.0

        # Periodically sync target network
        self.learn_step += 1
        if self.learn_step % 100 == 0:
            self.target.load_state_dict(self.q.state_dict())

        return {
            "td": float(td_loss.detach()),
            "gold": float(gold_loss.detach()),
            "loot": float(loot_loss.detach()),
            "market": float(market_loss.detach()),
            "quest": float(quest_loss.detach()),
            "rnd": rnd_loss,
        }

    # ---- weight persistence -------------------------------------------------

    def save_weights(self, path: str | None = None) -> None:
        if path is None:
            path = str(Path(__file__).with_name("ml_weights.json"))
        tmp = str(path) + ".tmp"
        torch.save({
            "q_state_dict": self.q.state_dict(),
            "target_state_dict": self.target.state_dict(),
            "optimizer_state_dict": self.optimizer.state_dict(),
            "rnd_pred_state_dict": self.rnd_pred.state_dict(),
            "rnd_opt_state_dict": self.rnd_opt.state_dict(),
            "rnd_mean": self._rnd_mean,
            "rnd_var": self._rnd_var,
            "epsilon": self._epsilon(),
            "training_steps": self.t_step,
            "learn_step": self.learn_step,
            "best_score": self.best_score,
            "obs_size": OBS_SIZE,
            "n_actions": N_ACTIONS,
            "version": checkpoint_version(),
        }, tmp)
        import os
        os.replace(tmp, path)
        print(f"Weights saved to {path} (epsilon={self._epsilon():.3f})")

    def load_weights(self, path: str | None = None) -> bool:
        if path is None:
            path = str(Path(__file__).with_name("ml_weights.json"))
        if not Path(path).exists():
            print(f"No weights file at {path}, starting fresh.")
            return False
        try:
            ckpt = torch.load(path, weights_only=True)
            self.q.load_state_dict(ckpt["q_state_dict"])
            if "target_state_dict" in ckpt:
                self.target.load_state_dict(ckpt["target_state_dict"])
            else:
                self.target.load_state_dict(self.q.state_dict())
            if "optimizer_state_dict" in ckpt:
                self.optimizer.load_state_dict(ckpt["optimizer_state_dict"])
            # RND state: absent in pre-curiosity checkpoints (keep fresh),
            # shape-mismatched after RND resizing (keep fresh, warn below).
            if "rnd_pred_state_dict" in ckpt:
                try:
                    self.rnd_pred.load_state_dict(ckpt["rnd_pred_state_dict"])
                except RuntimeError as e:
                    print(f"RND predictor shape changed, starting fresh: {e}")
            if "rnd_opt_state_dict" in ckpt:
                try:
                    self.rnd_opt.load_state_dict(ckpt["rnd_opt_state_dict"])
                except (RuntimeError, ValueError, KeyError):
                    pass
            self._rnd_mean = float(ckpt.get("rnd_mean", 0.0))
            self._rnd_var = float(ckpt.get("rnd_var", 1.0))
            self.ckpt_version = ckpt.get("version")
            if self.ckpt_version:
                v = self.ckpt_version
                cur = checkpoint_version()
                notes = []
                if v.get("obs_size") != OBS_SIZE or v.get("n_actions") != N_ACTIONS:
                    notes.append(f"dims {v.get('obs_size')}/{v.get('n_actions')} "
                                 f"vs current {OBS_SIZE}/{N_ACTIONS}")
                if v.get("git_sha") != cur["git_sha"]:
                    notes.append(f"git {str(v.get('git_sha'))[:8]} "
                                 f"vs current {cur['git_sha'][:8]}")
                if v.get("config_hash") != cur["config_hash"]:
                    notes.append("config differs")
                if notes:
                    print(f"Checkpoint {path} version notes: " + "; ".join(notes))
            self.t_step = int(ckpt.get("training_steps", 0))
            self.learn_step = int(ckpt.get("learn_step", 0))
            self.best_score = float(ckpt.get("best_score", self.best_score))
        except RuntimeError as e:
            # Shape mismatch: e.g. checkpoints saved before the quest block
            # grew OBS_SIZE/N_ACTIONS (or changed head count). Warn and keep
            # the fresh network instead of crashing the run.
            print(f"Weights at {path} don't match current obs/action size "
                  f"(OBS_SIZE={OBS_SIZE}, N_ACTIONS={N_ACTIONS}), starting fresh: {e}")
            return False
        print(f"Weights loaded from {path}")
        return True

    # ---- environment loop ---------------------------------------------------

    async def train(self, total_steps: int, save_every: int = 500):
        _ACTIONS = ACTIONS

        env = TextMMOEnv(self.name, url=self.url)
        obs = await env.reset()
        features = flatten_obs(obs)
        epsilon = self._epsilon()

        # Initialise auxiliary tracking from the first snapshot
        # (obs is the structured dict; flatten only for the network).
        self._prev_gold = float(obs.get("gold_raw", 0.0))
        self._prev_loot_value = 0.0
        self._prev_inventory_ids = set(obs.get("inv_names", []) or [])

        # Market tracking init. The env's step info carries live tax terms,
        # detected buy fills, and our standing orders with nets, so P&L
        # targets below can use exact after-tax accounting.
        self._posted_orders = 0
        self._bought_items = 0
        self._cancelled_orders = 0
        self._prev_own_orders = []

        total_reward = 0.0
        recent_rewards: list[float] = []
        action_counts: dict[int, int] = {i: 0 for i in range(N_ACTIONS)}
        quest_turnins = 0
        quest_accepts = 0
        quest2_turnins = 0
        quest2_accepts = 0
        intrinsic_total = 0.0

        start_step = self.t_step
        for offset in range(total_steps):
            self.t_step = start_step + offset + 1
            epsilon = self._epsilon()
            action = self.act(features, epsilon, env.valid_action_mask())
            action_counts[action] += 1
            next_obs, reward, done, info = await env.step(action)
            next_features = flatten_obs(next_obs)

            # ---- compute auxiliary targets from observation changes ----
            # gold: current player gold minus previous
            cur_gold = float(next_obs.get("gold_raw", self._prev_gold))
            gold_delta = cur_gold - self._prev_gold
            self._prev_gold = cur_gold

            # loot: inventory value change, but quest-aware (step 2) --
            # when this step crafted the Ancient Guardian Charm, regress
            # toward the charm's gold-terms net (turn-in gold minus the
            # merchant value of the consumed mats) instead of the naive
            # proxy. The mats would sell for quest_charm_cost() gold while
            # the turn-in pays QUEST_REWARD_GOLD, so the target is negative
            # whenever the quest is gold-costly: the head learns the
            # sell-mats-vs-quest profitability tradeoff, while the XP/score
            # upside flows through the quest head and the real reward.
            cur_inv = set(next_obs.get("inv_names", []) or [])
            prev_inv = self._prev_inventory_ids or set()
            new_items = cur_inv - prev_inv
            qinfo_early = (info or {}).get("quest") or {}
            if qinfo_early.get("crafted_charm"):
                loot_delta = float(quest_charm_net())
            else:
                loot_delta = 0.0
                if new_items:
                    loot_delta = min(1.0, cur_gold) / max(1.0, len(new_items))
            self._prev_inventory_ids = cur_inv

            # market P&L, tax-aware (server takes TAX_RATE with TAX_MINIMUM
            # floor, mirrored exactly by ml_env.market_net):
            # - buying: we pay full price -> negative P&L of the fill cost;
            # - our listing filled by someone else: exact after-tax net of
            #   each own order that vanished since the previous step.
            action_name = _ACTIONS[action]
            fill = (info or {}).get("market_fill") or {}
            if action_name == "market_buy" and fill.get("cost", 0.0) > 0:
                market_pnl = -float(fill["cost"])
                self._bought_items += 1
            else:
                prev_ids = {o["id"]: o for o in (self._prev_own_orders or [])}
                cur_ids = {o["id"]: o for o in (info.get("own_orders") or [])}
                market_pnl = sum(
                    market_net(o["price"]) for i, o in prev_ids.items() if i not in cur_ids
                )
            self._prev_own_orders = info.get("own_orders") or []

            # quest reward target: each turn-in pays its fixed score points
            # via the server (already inside `reward` above); the quest head
            # regresses toward the summed fixed value on turn-in steps so the
            # trunk learns both quests' stage encoding.
            qinfo = (info or {}).get("quest") or {}
            byq = qinfo.get("by_quest") or {}
            guard_turned = bool((byq.get("guard_charm") or {}).get("turned_in"))
            delver_turned = bool((byq.get("delver") or {}).get("turned_in"))
            guard_accepted = bool((byq.get("guard_charm") or {}).get("accepted"))
            delver_accepted = bool((byq.get("delver") or {}).get("accepted"))
            quest_reward = ((float(QUEST_REWARD_POINTS) if guard_turned else 0.0)
                            + (float(QUEST_DELVER_REWARD_POINTS) if delver_turned else 0.0))
            if guard_turned:
                quest_turnins += 1
            if delver_turned:
                quest2_turnins += 1
            if guard_accepted:
                quest_accepts += 1
            if delver_accepted:
                quest2_accepts += 1

            # quest-gated exploration bonus (step 1): accept and progress
            # transitions only -- turn-ins already pay real score. The state
            # machine enforces order (can't re-accept while active, can't
            # re-craft while flagged), so the bonus bootstraps chains
            # without becoming a farmable substitute for turn-ins.
            quest_intrinsic = 0.0
            if guard_accepted or delver_accepted:
                quest_intrinsic += self.intrinsic_accept
            if qinfo.get("crafted_charm") or qinfo.get("delver_became_ready"):
                quest_intrinsic += self.intrinsic_progress
            intrinsic_total += quest_intrinsic

            # RND curiosity on the post-step observation (novel states pay
            # more; the predictor fit in learn() makes them familiar).
            # Skipped entirely when rnd_lambda is 0 ("disables" curiosity).
            rnd_bonus = self.rnd_bonus(next_features) if self.rnd_lambda else 0.0

            # Store transition with all auxiliary targets
            self.store(
                {
                    "state": features,
                    "action": action,
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

            total_reward += reward
            recent_rewards.append(reward)
            if len(recent_rewards) > 200:
                recent_rewards.pop(0)

            features, obs = next_features, next_obs

            # Learning step
            if self.t_step >= self.replay_size:
                losses = self.learn()
            else:
                losses = {"td": None, "gold": None, "loot": None, "market": None,
                          "quest": None, "rnd": None}

            # Log every 50 steps
            if self.t_step % 50 == 0:
                avg_recent = sum(recent_rewards) / len(recent_rewards)
                print(
                    f"step {self.t_step:>6}  eps={epsilon:.3f}  score={obs['score_raw']:.2f}  "
                    f"avg_reward(last {len(recent_rewards)})={avg_recent:+.4f}  "
                    f"room={obs['room_id']}  quest={obs.get('quest_stage', '?')}/{obs.get('quest2_stage', '?')}  "
                    f"quests(acc/turn)={quest_accepts}/{quest_turnins} "
                    f"delver(acc/turn)={quest2_accepts}/{quest2_turnins} intr={intrinsic_total:.1f}  "
                    f"losses(TD/Gold/Loot/Mkt/Qst/Rnd)={_fmt_loss(losses['td'])}/{_fmt_loss(losses['gold'])}/{_fmt_loss(losses['loot'])}/{_fmt_loss(losses['market'])}/{_fmt_loss(losses['quest'])}/{_fmt_loss(losses['rnd'])}"
                )

            # Save checkpoint + best-model snapshot
            if self.t_step % save_every == 0:
                self.save_weights()
                if obs["score_raw"] > self.best_score:
                    self.best_score = obs["score_raw"]
                    self.save_weights(str(Path(__file__).with_name("ml_best.json")))
                    print(
                        f"\nNew best score {self.best_score:.2f} -> saved ml_best.json"
                    )

            if done:
                obs = await env.reset()
                features = flatten_obs(obs)
                epsilon = self._epsilon()
                # reset auxiliary tracking
                self._prev_gold = float(obs.get("gold_raw", 0.0))
                self._prev_loot_value = 0.0
                self._prev_inventory_ids = set(obs.get("inv_names", []) or [])
                # reset market tracking
                self._posted_orders = 0
                self._bought_items = 0
                self._cancelled_orders = 0
                self._prev_own_orders = []

        self.save_weights()
        print(f"\nTraining finished after {self.t_step} steps.")
        print(f"Final score: {obs['score_raw']:.2f}  Total reward: {total_reward:.2f}")
        print(f"Quest accepts: {quest_accepts}  Quest turn-ins: {quest_turnins}")
        print(f"Delver accepts: {quest2_accepts}  Delver turn-ins: {quest2_turnins}")
        print(f"Intrinsic exploration bonus total: {intrinsic_total:.1f}")
        print("Action usage:", {_ACTIONS[i]: c for i, c in enumerate(action_counts) if c})
        print("Weights saved to torch_agents/ml_weights.json (separate from ml/ml_weights.json)")
        await env.close()


# ---------------------------------------------------------------------------
# Quick smoke-test entry point
# ---------------------------------------------------------------------------

async def _demo():
    from ml_env import ACTIONS, N_ACTIONS, OBS_SIZE, flatten_obs, QUESTS, quest_charm_cost, quest_charm_net

    env = TextMMOEnv("TorchDemo")
    obs = await env.reset()
    flat = flatten_obs(obs)
    print(f"Observation size: {len(flat)} floats, {N_ACTIONS} actions")
    print(f"Action space: {ACTIONS[:5]}... (first 5 of {N_ACTIONS})")
    print(f"Quest catalog: {list(QUESTS)} (guard_charm: accept->craft charm->turn in; "
          f"delver: accept->clear floors->turn in; both repeatable)")
    print(f"Charm econ: mats cost {quest_charm_cost()}g at merchant vs quest gold "
          f"{QUESTS['guard_charm']['reward_gold']}g -> net {quest_charm_net():+}g (XP/score upside separate)")
    print(f"Quest stage now: {obs.get('quest_stage')}/{obs.get('quest2_stage')}  giver_here={obs.get('quest_giver_here')}")

    # Take one random step
    action = random.randrange(N_ACTIONS)
    print(f"Sample action: {ACTIONS[action]}")
    next_obs, reward, done, info = await env.step(action)
    print(f"Step reward: {reward:+.3f}  done={done}  quest={info.get('quest')}")
    await env.close()


def main():
    import argparse

    parser = argparse.ArgumentParser(description="PyTorch DQN agent for the text MMO.")
    parser.add_argument("--demo", action="store_true", help="quick smoke-test: one random env step")
    parser.add_argument("--name", default="TorchBot", help="character name (reuse across runs to keep training)")
    parser.add_argument("--url", default="ws://localhost:8765", help="game WebSocket URL")
    parser.add_argument("--steps", type=int, default=2000, help="total training steps this run")
    parser.add_argument("--save-every", type=int, default=500, help="checkpoint weights every N steps")
    parser.add_argument("--rnd-lambda", type=float, default=0.1, help="RND curiosity weight (0 disables)")
    parser.add_argument("--rnd-lr", type=float, default=1e-3, help="RND predictor learning rate")
    args = parser.parse_args()

    if args.demo:
        asyncio.run(_demo())
        return
    agent = TorchDQNAgent(name=args.name, url=args.url,
                          rnd_lambda=args.rnd_lambda, rnd_lr=args.rnd_lr)
    agent.load_weights()
    asyncio.run(agent.train(total_steps=args.steps, save_every=args.save_every))


if __name__ == "__main__":
    try:
        main()
    except (OSError, ConnectionError) as e:
        print(f"Could not reach the server: {e}")
        print("Start the engine first: python server.py (or start.bat)")
