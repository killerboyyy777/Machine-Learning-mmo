"""Offline save/load checks for both ML agents.

Run from the repo root: python tests/test_persistence.py
No game server is needed.
"""
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "ml"))
sys.path.insert(0, os.path.join(ROOT, "torch_agents"))

import runlog
from ml_client import LinearQAgent
from ml_env import N_ACTIONS, OBS_SIZE
from dqn_agent import TorchDQNAgent


def linear_check(folder):
    path = os.path.join(folder, "linear.json")
    agent = LinearQAgent(OBS_SIZE, N_ACTIONS)
    features = [0.25] * OBS_SIZE
    agent.training_steps = 123
    agent.update(features, 3, 2.5, features, False)
    expected = agent.q_values(features)
    agent.save(path)

    loaded = LinearQAgent(OBS_SIZE, N_ACTIONS)
    assert loaded.load(path)
    assert loaded.training_steps == 123
    assert loaded.q_values(features) == expected
    assert loaded.bias == agent.bias
    # version metadata round-trips and matches the live build
    v = loaded.ckpt_version
    assert v and v["obs_size"] == OBS_SIZE and v["n_actions"] == N_ACTIONS, v
    assert v["git_sha"] and v["config_hash"] and v["saved_at"], v
    print("LINEAR_PERSISTENCE_OK")


def torch_check(folder):
    path = os.path.join(folder, "torch.pt")
    agent = TorchDQNAgent()
    agent.q.q_head.bias.data.fill_(1.25)
    agent.target.load_state_dict(agent.q.state_dict())
    agent.t_step = 456
    agent.learn_step = 17
    agent.best_score = 89.5
    agent.save_weights(path)

    loaded = TorchDQNAgent()
    assert loaded.load_weights(path)
    assert loaded.t_step == 456
    assert loaded.learn_step == 17
    assert loaded.best_score == 89.5
    assert loaded.q.q_head.bias.detach().tolist() == agent.q.q_head.bias.detach().tolist()
    assert loaded.target.q_head.bias.detach().tolist() == agent.target.q_head.bias.detach().tolist()
    # #53: version metadata round-trips and matches the live build
    v = loaded.ckpt_version
    assert v and v["obs_size"] == OBS_SIZE and v["n_actions"] == N_ACTIONS, v
    assert v["git_sha"] and v["config_hash"] and v["saved_at"], v
    print("TORCH_PERSISTENCE_OK")


def runlog_check(folder):
    """Run records (#65): the manifest, the sample log and the traversal guard
    are the whole point -- a run that cannot be compared after it exits is the
    bug this file is here to stop."""
    root = os.path.join(folder, "runs")
    assert runlog.higher_is_better("score") is True
    assert runlog.higher_is_better("td_loss") is False
    assert runlog.rate_per_hour(None, 5) is None and runlog.rate_per_hour(10, 0) is None
    assert runlog.seed_everything(None) is False and runlog.seed_everything(7) is True

    run = runlog.start_run("dqn", root=root, seed=7, label="unit",
                           hparams={"lr": 1e-3, "device": None})
    rid = run.run_id
    assert runlog.valid_run_id(rid), rid
    run.record(steps=100, score=5, td_loss=0.8)
    run.record(steps=200, score=12, td_loss=0.4)
    run.note(checkpoint="torch_agents/ml_weights.json")
    run.finish("finished", best_score=12)

    got = runlog.read_run(root, rid)
    assert got["status"] == "finished" and got["seed"] == 7 and got["label"] == "unit"
    assert got["metrics"]["score"] == 12 and got["metrics"]["score_hr"] > 0, got["metrics"]
    assert got["ended"] > got["started"] and got["elapsed"] > 0
    assert got["checkpoint"] == "torch_agents/ml_weights.json"
    # argparse namespaces hold Paths and Nones; the manifest must stay plain JSON.
    assert got["hparams"]["lr"] == 1e-3 and got["hparams"]["device"] is None
    samples = runlog.read_samples(root, rid)
    assert len(samples) == 2 and samples[-1]["score"] == 12
    assert samples[-1]["_ts"] >= samples[0]["_ts"]

    # A crash must land as status=failed, not as a permanent "running".
    try:
        with runlog.start_run("soak", root=root, seed=8) as crashed:
            crashed.record(episodes=3, mean_reward=1.5)
            raise RuntimeError("boom")
    except RuntimeError:
        pass
    failed = [r for r in runlog.list_runs(root) if r["status"] == "failed"]
    assert len(failed) == 1 and "boom" in failed[0]["error"], failed
    assert failed[0]["metrics"]["episodes"] == 3

    # A torn final line (killed writer) costs that sample, not the file.
    with open(os.path.join(root, rid, "metrics.jsonl"), "a", encoding="utf-8") as fh:
        fh.write('{"_ts": 1, "score"')
    assert len(runlog.read_samples(root, rid)) == 2

    # Ids reach the reader from a dashboard query string, so only the minted
    # shape may be joined onto the runs root.
    for bad in ("../../etc", "..", "", "20260101-120000/../..", "x" * 40, None):
        assert runlog.read_run(root, bad) is None, bad
        assert runlog.read_samples(root, bad) == [], bad

    # A never-finished run (killed trainer) still ages, or the Age column
    # reads "0s" forever.
    runlog.start_run("ml_client", root=root).record(steps=1)
    ages = [r["elapsed"] for r in runlog.list_runs(root) if r["status"] == "running"]
    assert ages and all(a >= 0 for a in ages), ages

    payload = runlog.runs_payload(root=root, ids=[rid])
    ids = [r["run_id"] for r in payload["runs"]]
    assert ids == sorted(ids, reverse=True), ids
    assert "series" not in runlog.runs_payload(root=root)
    assert len(payload["series"][rid]) == 2
    fields = {f["name"]: f["higher_is_better"] for f in payload["fields"]}
    assert fields.get("score") is True and fields.get("td_loss") is False, fields
    assert fields.get("score_hr") is True, fields
    # A directory with no manifest is skipped, not allowed to break the tab.
    os.makedirs(os.path.join(root, "20260101-999999", "junk"), exist_ok=True)
    assert len(runlog.list_runs(root)) == len(ids)
    print("RUNLOG_OK")


def runlog_pkg_import_check():
    """soak.py imports runlog as ml.runlog, which leaves ml/ off sys.path, so
    the flat `import versioning` cannot resolve.  Provenance must survive that
    or every soak run is an unidentifiable row; the sys.path squash is what
    makes this the same import shape."""
    import subprocess
    probe = (
        "import os, sys; "
        "sys.path = [p for p in sys.path if os.path.basename(p) != 'ml']; "
        "assert not any(os.path.basename(p) == 'ml' for p in sys.path); "
        "import ml.runlog as rl; "
        "p = rl._provenance(); print(p['git_sha'], p['config_hash'])"
    )
    res = subprocess.run(
        [sys.executable, "-c", probe],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    assert res.returncode == 0, res.stderr
    sha, _, cfg = res.stdout.strip().partition(" ")
    assert len(sha) >= 7 and cfg, res.stdout
    print("RUNLOG_PKG_IMPORT_OK")


with tempfile.TemporaryDirectory() as folder:
    linear_check(folder)
    torch_check(folder)
    runlog_check(folder)
    runlog_pkg_import_check()
print("PERSISTENCE_ALL_OK")
