"""#469: reward-mode evaluation must report the reward the env paid.

The old eval_seed returned obs["score_raw"] and discarded the step reward,
so --reward-mode changed the learning signal but never the reported number.
These tests pin both halves of the fix: eval_seed returns a separate reward
total, and the CLI writes it into the run record. No live server needed --
the env and the checkpoint loader are replaced with fakes.
"""

import asyncio
import json
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "torch_agents"))
sys.path.insert(0, ROOT)

import eval as ev  # noqa: E402


class FakeEnv:
    """Three steps: score_raw climbs, reward is a distinct constant."""

    def __init__(
        self,
        name,
        url=None,
        max_steps=None,
        reward_mode=None,
        reward_formula=None,
        **kwargs,
    ):
        self.name = name
        self.url = url
        self.max_steps = max_steps
        self.reward_mode = reward_mode
        self.reward_formula = reward_formula
        self.steps = 0

    async def reset(self):
        return {"score_raw": 10.0}

    def valid_action_mask(self):
        return [True]

    async def step(self, action):
        self.steps += 1
        return (
            {"score_raw": 10.0 + self.steps},
            2.5,
            self.steps >= 3,
            {"reward_vector": {"score": 2.5}},
        )

    async def close(self):
        pass


def test_eval_seed_reports_reward_distinct_from_score():
    saved_env = ev.TextMMOEnv
    saved_flat = ev.flatten_obs
    made = []
    try:
        ev.TextMMOEnv = lambda *a, **k: made.append(FakeEnv(*a, **k)) or made[-1]
        ev.flatten_obs = lambda obs: obs
        result = asyncio.run(
            ev.eval_seed(
                lambda feats, mask: 0,
                "ws://unused",
                7,
                3,
                "xp",
                tag="T",
                reward_formula="xp_delta * 2",
            )
        )
    finally:
        ev.TextMMOEnv = saved_env
        ev.flatten_obs = saved_flat

    assert result["score"] == 13.0, result
    assert result["reward"] == 7.5, result
    assert result["reward"] != result["score"]
    assert made[0].reward_mode == "xp"
    assert made[0].reward_formula == "xp_delta * 2"
    assert made[0].name.startswith("Eval"), made[0].name
    assert len(made[0].name) == 24, made[0].name
    print("EVAL_SEED_REWARD_OK")


def test_evaluate_collects_parallel_score_and_reward_lists():
    saved_env = ev.TextMMOEnv
    saved_flat = ev.flatten_obs
    saved_load = ev._load_agent
    made = []
    try:
        ev.TextMMOEnv = lambda *a, **k: made.append(FakeEnv(*a, **k)) or made[-1]
        ev.flatten_obs = lambda obs: obs
        ev._load_agent = lambda policy, cp: lambda feats, mask: 0
        result = asyncio.run(
            ev.evaluate(
                "weights.pt",
                "torch",
                "ws://unused",
                [0, 1],
                3,
                "econ",
                run_id="same-run",
            )
        )
    finally:
        ev.TextMMOEnv = saved_env
        ev.flatten_obs = saved_flat
        ev._load_agent = saved_load

    assert result["scores"] == [13.0, 13.0], result
    assert result["rewards"] == [7.5, 7.5], result
    assert result["evaluation_id"] == "same-run", result
    assert len({env.name for env in made}) == 2, [env.name for env in made]
    assert all(len(env.name) == 24 for env in made), [env.name for env in made]
    print("EVALUATE_REWARD_LISTS_OK")


def test_main_writes_rewards_and_formula_to_run_record(tmp_path=None):
    saved_eval = ev.evaluate
    saved_report = ev.report
    saved_argv = sys.argv
    seen = {}

    async def fake_evaluate(*args, **kwargs):
        seen["reward_formula"] = kwargs.get("reward_formula")
        seen["run_id"] = kwargs.get("run_id")
        return {"scores": [1.0, 2.0], "rewards": [3.0, 4.0]}

    if tmp_path is None:
        tmp = tempfile.TemporaryDirectory()
        folder = tmp.name
    else:
        folder = tmp_path
    out = os.path.join(folder, "_eval_rewards_record.json")
    try:
        ev.evaluate = fake_evaluate
        ev.report = lambda name, scores: (0.0, 0.0)
        sys.argv = [
            "eval",
            "--checkpoint",
            "weights.pt",
            "--reward-formula",
            "xp_delta * 2",
            "--test",
            "none",
            "--out",
            out,
        ]
        ev.main()
        with open(out, encoding="utf-8") as fh:
            record = json.load(fh)
    finally:
        ev.evaluate = saved_eval
        ev.report = saved_report
        sys.argv = saved_argv

    assert seen["reward_formula"] == "xp_delta * 2", seen
    assert seen["run_id"] == record["evaluation_id"], (seen, record)
    assert len(record["evaluation_id"]) == 12, record
    assert record["challenger"] == [1.0, 2.0], record
    assert record["challenger_rewards"] == [3.0, 4.0], record
    assert record["reward_formula"] == "xp_delta * 2", record
    assert record["comparison"] is None, record
    print("EVAL_RECORD_REWARDS_OK")


def test_eval_names_are_unique_across_runs_and_checkpoint_paths():
    first = ev._eval_character_name("weightsabc123", "run-one", 7)
    second = ev._eval_character_name("weightsabc123", "run-two", 7)
    other_checkpoint = ev._eval_character_name("otherabc123", "run-one", 7)
    assert first != second
    assert first != other_checkpoint
    assert len(first) == len(second) == len(other_checkpoint) == 24
    assert first.isalnum() and second.isalnum() and other_checkpoint.isalnum()
    assert ev._run_tag("a/model.pt") != ev._run_tag("b/model.pt")
    print("EVAL_NAMES_RUN_SCOPED_OK")


if __name__ == "__main__":
    test_eval_seed_reports_reward_distinct_from_score()
    test_eval_names_are_unique_across_runs_and_checkpoint_paths()
    test_evaluate_collects_parallel_score_and_reward_lists()
    test_main_writes_rewards_and_formula_to_run_record()
    print("ALL_EVAL_REWARDS_OK")
