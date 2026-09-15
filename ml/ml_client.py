"""
A simple machine-learning client for the text MMO: an online linear
Q-learning agent that connects through ml_env.TextMMOEnv and learns from
experience, using the server's own score as its reward signal.

This deliberately uses the simplest thing that qualifies as "learning"
(a linear function approximator, trained with plain Python lists -- no
numpy/torch/gym dependency) rather than a full deep-RL stack, so the whole
pipeline is easy to read top to bottom. Once you've confirmed it works,
swapping LinearQAgent for a small PyTorch MLP is a drop-in change: it only
needs to expose the same act()/update() shape.

Run:
    python3 server.py                       # in one terminal
    python3 ml_client.py                    # in another
    python3 ml_client.py --name MLAgent --steps 5000   # longer run, same character

Weights persist to ml_weights.json (loaded automatically if present), so
repeated runs keep improving the same policy rather than starting over.
By default this trains one persistent character continually (same name
every run) rather than resetting each "episode" -- see README.md's
"Episodic vs. continual training" note for why that fits this game better.
"""

import argparse
import asyncio
import json
import os
import random

try:
    from .ml_env import TextMMOEnv, ACTIONS, N_ACTIONS, OBS_SIZE, flatten_obs
except ImportError:
    # Running as a script (python ml/ml_client.py) or imported as a
    # top-level module (tests/test_persistence.py): no parent package.
    from ml_env import TextMMOEnv, ACTIONS, N_ACTIONS, OBS_SIZE, flatten_obs

_HERE = os.path.dirname(os.path.abspath(__file__))
WEIGHTS_FILE = os.path.join(_HERE, "ml_weights.json")


class LinearQAgent:
    """Q(s, a) = weights[a] . features(s) + bias[a], trained online with
    the standard Q-learning TD update:

        target    = reward + gamma * max_a' Q(next_state, a')
        td_error  = target - Q(state, action)
        weights[action] += alpha * td_error * features

    One weight vector per action. No matrix library needed -- everything
    here is plain Python lists and a couple of loops, which is enough for
    an OBS_SIZE-dimensional observation and an N_ACTIONS-action space
    (both imported from ml_env, so this agent tracks env changes).
    """

    def __init__(self, obs_size, n_actions, alpha=0.05, gamma=0.9):
        self.obs_size = obs_size
        self.n_actions = n_actions
        self.alpha = alpha
        self.gamma = gamma
        self.weights = [[0.0] * obs_size for _ in range(n_actions)]
        self.bias = [0.0] * n_actions
        self.training_steps = 0

    def q_values(self, features):
        return [
            sum(w * f for w, f in zip(self.weights[a], features)) + self.bias[a]
            for a in range(self.n_actions)
        ]

    def act(self, features, epsilon, mask=None):
        """Epsilon-greedy over valid actions only: explore randomly among
        valid actions with probability epsilon, otherwise take the best
        valid action. Invalid actions (mask 0) are never picked, so agents
        stop wasting steps on guaranteed-error commands."""
        valid = [a for a in range(self.n_actions) if mask is None or mask[a]]
        if not valid:
            valid = list(range(self.n_actions))
        if random.random() < epsilon:
            return random.choice(valid)
        qs = self.q_values(features)
        return max(valid, key=lambda a: qs[a])

    def update(self, features, action, reward, next_features, done):
        next_q = 0.0 if done else max(self.q_values(next_features))
        target = reward + self.gamma * next_q
        current = self.q_values(features)[action]
        td_error = target - current
        for i in range(self.obs_size):
            self.weights[action][i] += self.alpha * td_error * features[i]
        self.bias[action] += self.alpha * td_error
        return td_error

    def save(self, path):
        tmp = path + ".tmp"
        with open(tmp, "w") as f:
            json.dump({
                "weights": self.weights,
                "bias": self.bias,
                "training_steps": self.training_steps,
                "obs_size": self.obs_size,
                "n_actions": self.n_actions,
            }, f)
        os.replace(tmp, path)

    def load(self, path):
        if not os.path.exists(path):
            return False
        with open(path) as f:
            data = json.load(f)
        if (len(data.get("weights", [])) == self.n_actions
                and data["weights"]
                and len(data["weights"][0]) == self.obs_size
                and len(data.get("bias", [])) == self.n_actions):
            self.weights = data["weights"]
            self.bias = data["bias"]
            self.training_steps = int(data.get("training_steps", 0))
            return True
        print(f"Warning: {path} doesn't match current obs/action size, starting fresh.")
        return False


async def train(name, url, total_steps, save_every, epsilon_start, epsilon_end, epsilon_decay_steps):
    agent = LinearQAgent(OBS_SIZE, N_ACTIONS)
    if agent.load(WEIGHTS_FILE):
        print(f"Loaded existing weights from {WEIGHTS_FILE}")
    else:
        print("Starting from fresh (zeroed) weights")

    env = TextMMOEnv(name, url=url)
    obs = await env.reset()
    features = flatten_obs(obs)

    total_reward = 0.0
    recent_rewards = []
    action_counts = [0] * N_ACTIONS

    for _ in range(total_steps):
        agent.training_steps += 1
        progress = min(1.0, agent.training_steps / epsilon_decay_steps)
        epsilon = epsilon_start + (epsilon_end - epsilon_start) * progress

        action = agent.act(features, epsilon, env.valid_action_mask())
        action_counts[action] += 1
        next_obs, reward, done, info = await env.step(action)
        next_features = flatten_obs(next_obs)

        agent.update(features, action, reward, next_features, done)

        total_reward += reward
        recent_rewards.append(reward)
        if len(recent_rewards) > 200:
            recent_rewards.pop(0)

        features, obs = next_features, next_obs

        if agent.training_steps % 50 == 0:
            avg_recent = sum(recent_rewards) / len(recent_rewards)
            print(f"step {agent.training_steps:>6}  eps={epsilon:.3f}  score={obs['score_raw']:.2f}  "
                  f"avg_reward(last {len(recent_rewards)})={avg_recent:+.4f}  "
                  f"room={obs['room_id']}")

        if agent.training_steps % save_every == 0:
            agent.save(WEIGHTS_FILE)

        if done:
            obs = await env.reset()
            features = flatten_obs(obs)

    agent.save(WEIGHTS_FILE)
    print(f"\nTraining finished after {total_steps} steps.")
    print(f"Final score: {obs['score_raw']:.2f}   Total reward accumulated: {total_reward:.2f}")
    print("Action usage:", {ACTIONS[i]: c for i, c in enumerate(action_counts) if c})
    print(f"Weights saved to {WEIGHTS_FILE} -- rerun with the same --name to keep training this character.")
    await env.close()


def main():
    parser = argparse.ArgumentParser(description="Simple linear Q-learning client for the text MMO.")
    parser.add_argument("--name", default="MLAgent",
                         help="Character name. Reuse the same name across runs to keep training "
                              "one persistent character (recommended) rather than starting fresh each time.")
    parser.add_argument("--url", default="ws://localhost:8765")
    parser.add_argument("--steps", type=int, default=2000, help="Total training steps this run")
    parser.add_argument("--save-every", type=int, default=100, help="Checkpoint weights every N steps")
    parser.add_argument("--epsilon-start", type=float, default=1.0, help="Starting exploration rate")
    parser.add_argument("--epsilon-end", type=float, default=0.05, help="Final exploration rate")
    parser.add_argument("--epsilon-decay-steps", type=int, default=1500,
                         help="Steps over which epsilon decays from start to end")
    args = parser.parse_args()

    asyncio.run(train(
        args.name, args.url, args.steps, args.save_every,
        args.epsilon_start, args.epsilon_end, args.epsilon_decay_steps,
    ))


if __name__ == "__main__":
    main()
