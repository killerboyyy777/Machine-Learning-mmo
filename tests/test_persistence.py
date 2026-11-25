"""Offline save/load checks for both ML agents.

Run from the repo root: python tests/test_persistence.py
No game server is needed.
"""

import asyncio
import json
import os
import re
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "ml"))
sys.path.insert(0, os.path.join(ROOT, "torch_agents"))

import runlog  # noqa: E402
import server as srv  # noqa: E402
from dqn_agent import TorchDQNAgent  # noqa: E402
from ml_client import LinearQAgent  # noqa: E402
from ml_env import N_ACTIONS, OBS_SIZE  # noqa: E402


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


def linear_bad_checkpoint_check(folder):
    path = os.path.join(folder, "bad-linear.json")
    cases = [
        {"weights": [[0, 0, 0], [0]], "bias": [0, 0]},
        {
            "weights": [[0, 0, 0], [0, float("nan"), 0]],
            "bias": [0, 0],
        },
        {"weights": [[0, 0, 0], [0, 0, 0]], "bias": [0, float("inf")]},
        {
            "weights": [[0, 0, 0], [0, 0, 0]],
            "bias": [0, 0],
            "training_steps": "not-an-int",
        },
        [],
    ]
    for data in cases:
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(data, fh)
        agent = LinearQAgent(3, 2)
        original = [row[:] for row in agent.weights]
        assert not agent.load(path), data
        assert agent.weights == original

    with open(path, "w", encoding="utf-8") as fh:
        fh.write("{")
    assert not LinearQAgent(3, 2).load(path)
    print("LINEAR_BAD_CHECKPOINT_OK")


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
    assert (
        loaded.q.q_head.bias.detach().tolist() == agent.q.q_head.bias.detach().tolist()
    )
    assert (
        loaded.target.q_head.bias.detach().tolist()
        == agent.target.q_head.bias.detach().tolist()
    )
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

    run = runlog.start_run(
        "dqn", root=root, seed=7, label="unit", hparams={"lr": 1e-3, "device": None}
    )
    rid = run.run_id
    assert runlog.valid_run_id(rid), rid
    run.record(steps=100, score=5, td_loss=0.8)
    run.record(steps=200, score=12, td_loss=0.4)
    run.note(checkpoint="torch_agents/ml_weights.json")
    run.finish("finished", best_score=12)

    got = runlog.read_run(root, rid)
    assert got["status"] == "finished" and got["seed"] == 7 and got["label"] == "unit"
    assert got["metrics"]["score"] == 12 and got["metrics"]["score_hr"] > 0, got[
        "metrics"
    ]
    assert got["ended"] > got["started"] and got["elapsed"] > 0
    assert got["checkpoint"] == "torch_agents/ml_weights.json"
    # argparse namespaces hold Paths and Nones; the manifest must stay plain JSON.
    assert got["hparams"]["lr"] == 1e-3 and got["hparams"]["device"] is None
    # Session provenance (#318): every run carries a timestamped session id,
    # and a caller spanning one logical session passes a shared one through.
    _sid_pat = r"\d{8}-\d{6}-[0-9a-f]{6}"
    assert re.fullmatch(_sid_pat, got["session_id"]), got["session_id"]
    _kw = {"root": root, "seed": 9, "session_id": "20250101-000000-abc123"}
    with runlog.start_run("soak", **_kw) as sess:
        assert sess.manifest["session_id"] == "20250101-000000-abc123"
    assert runlog.read_run(root, sess.run_id)["session_id"] == _kw["session_id"]
    _s1, _s2 = runlog.new_session_id(), runlog.new_session_id()
    assert re.fullmatch(r"\d{8}-\d{6}-[0-9a-f]{6}", _s1), _s1
    assert re.fullmatch(r"\d{8}-\d{6}-[0-9a-f]{6}", _s2), _s2
    assert _s1 != _s2, (_s1, _s2)
    print("SESSION_ID_OK")
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


def runlog_sendback_check(folder):
    """The #65 review findings: a query string is attacker-controlled, a bool
    extra is not a metric, and two trainers can start in the same second."""
    root = os.path.join(folder, "runs2")

    # --- query parsing (#6) ------------------------------------------------
    # Only the LAST runs= used to survive, so a multi-param query silently dropped
    # every earlier one. Ids are minted-shape-checked, so use real shapes here.
    assert runlog.parse_run_ids("?runs=20260101-120000&runs=20260101-130000") == [
        "20260101-120000",
        "20260101-130000",
    ]
    # A percent-encoded id never reached valid_run_id before, and an encoded
    # traversal is the case that matters.
    assert runlog.parse_run_ids("?runs=20260101-1200%30") == []
    assert runlog.parse_run_ids("?runs=..%2f..%2fetc") == []
    # 100k ids is 100k file opens per request unless the caller caps it.
    flood = "?" + "&".join(f"runs=20260101-{i:06d}" for i in range(100000))
    assert len(runlog.parse_run_ids(flood)) == runlog.MAX_RUN_IDS
    # Over-long query strings are truncated before parsing, not parsed slowly.
    assert len(runlog.parse_run_ids("?" + "x" * (runlog.MAX_QUERY_CHARS + 5000))) == 0
    # Duplicates collapse: two boxes for one run must not open it twice.
    assert runlog.parse_run_ids("?runs=20260101-120000,20260101-120000") == [
        "20260101-120000"
    ]
    # runs_payload re-validates, so a caller bypassing parse_run_ids is safe too.
    assert runlog.runs_payload(root=root, ids=["../../etc"])["series"] == {}

    # --- bool extras (#8) --------------------------------------------------
    # stopped=True is a state flag, not a metric; as a manifest key it vanished
    # from the dashboard's field list and could never be compared.
    with runlog.start_run("torch_farm", root=root) as b:
        b.record(steps=5, total_reward=1.0)
        b.finish("finished", steps=5, stopped=True, crash_reason=None)
    m = runlog.read_run(root, b.run_id)
    assert m["metrics"]["stopped"] is True, m["metrics"]
    assert m["metrics"]["crash_reason"] is None
    assert m["metrics"]["steps"] == 5
    assert any(
        f["name"] == "stopped" for f in runlog.runs_payload(root=root)["fields"]
    ), "bool field missing"

    # --- direction (#5) ---------------------------------------------------
    # A substring match called these "higher", so a loss was ranked as a win.
    # A loss, though, must still rank by its own end.
    for tok in ("td_loss", "rnd_loss", "loss", "mae", "rmse", "value_error"):
        assert runlog.higher_is_better(tok) is False, tok
    # Direction is a whole word, not a fragment: "gloss" (g-loss) and "terror"
    # (t-error) are not losses.
    for tok in (
        "gloss",
        "terror",
        "my_lossy_metric",
        "losses",
        "runtimE",
        "myloss",
        "avg_reward",
    ):
        assert runlog.higher_is_better(tok) is True, tok

    # --- same-second collision (#4) ---------------------------------------
    # exists()+makedirs(exist_ok=True) raced: two runs could share one run.json
    # and interleave their jsonl.
    made = {runlog.start_run("ml_client", root=root).run_id for _ in range(4)}
    assert len(made) == 4, made

    # --- per-kind columns (#1) -------------------------------------------
    kinds = runlog.kind_fields(
        [
            {"kind": "dqn", "metrics": {"steps": 1, "total_steps": 1}},
            {"kind": "ml_botfarm", "metrics": {"fitness": 1.0}},
            {"kind": "soak", "metrics": {"episodes": 2}},
        ]
    )
    assert kinds["ml_botfarm"] == ["fitness"], kinds
    assert kinds["soak"] == ["episodes"], kinds
    # A kind the table has never seen still gets its canonical columns rather
    # than nothing, so a first botfarm run is readable.
    assert runlog.kind_fields(
        [{"kind": "ml_botfarm", "metrics": {"top_score": 4}}]
    ) == {"ml_botfarm": ["top_score"]}, runlog.kind_fields(
        [{"kind": "ml_botfarm", "metrics": {"top_score": 4}}]
    )
    # An empty index still needs columns for the picker to have anything.
    assert runlog.kind_fields([]) == {}, runlog.kind_fields([])

    # --- no absolute path to the client (#7) -----------------------------
    assert runlog.runs_payload(root=root)["root_name"] == "runs2"
    assert "root" not in runlog.runs_payload(root=root)
    print("RUNLOG_SENDBACK_OK")


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


def freight_refund_persistence_check(folder):
    """Terminal freight refunds survive a save/load cycle (#857)."""
    path = os.path.join(folder, "freight.json")
    saved = (
        srv.FREIGHT,
        srv._freight_counter,
        srv._freight_dirty,
        srv.FREIGHT_FILE_BLOCKED,
    )
    row = {
        "poster": "Poster",
        "poster_kind": "player",
        "hauler": "Hauler",
        "origin": "market",
        "destination": "smithy",
        "items": [],
        "fee": 20,
        "deposit": 10,
        "bonus": 0,
        "bonus_until": 0.0,
        "posted_at": 1.0,
        "expires_at": 100.0,
        "state": "lost",
        "refund": 30,
        "delivered_at": 0.0,
        "lost_at": 50.0,
    }
    try:
        srv.FREIGHT = {}
        srv.FREIGHT_FILE_BLOCKED = False
        srv.FREIGHT[1] = dict(row, id=1)
        srv.FREIGHT[2] = dict(row, id=2, state="open")
        srv.FREIGHT[3] = dict(row, id=3, state="open", refund=0, items=["rat_tail"])
        srv.FREIGHT[4] = dict(row, id=4, refund=0)
        assert srv.save_freight(path) is True
        srv.FREIGHT = {}
        loaded = srv.load_freight(path)
        assert set(loaded) == {1, 3}, loaded
        assert loaded[1]["state"] == "lost" and loaded[1]["refund"] == 30, loaded[1]
        assert loaded[1]["items"] == [], loaded[1]
        assert loaded[3]["items"] == ["rat_tail"], loaded[3]
        srv.FREIGHT = loaded
        poster = srv.Player(
            ws=None,
            id=1,
            name="Poster",
            logged_in=False,
            hp=10,
            max_hp=10,
            room="market",
            gold=0,
            inventory=[],
        )
        owed = srv._freight_flush_refunds(poster)
        assert owed == 30 and poster.gold == 30, (owed, poster.gold)
        assert loaded[1]["refund"] == 0, loaded[1]
    finally:
        (
            srv.FREIGHT,
            srv._freight_counter,
            srv._freight_dirty,
            srv.FREIGHT_FILE_BLOCKED,
        ) = saved
    print("FREIGHT_REFUND_PERSISTENCE_OK")


def freight_death_refund_save_load_check(folder):
    """A real hauler death leaves a refund row that survives save/load (#872).

    The #857 check injects an empty-items row by hand. Here a real package goes
    post -> accept -> death, so a production pop that drops the items key is
    caught before the row reaches disk.
    """
    path = os.path.join(folder, "freight-death-refund.json")
    marker = "ISSUE872_REFUND_MARKER"
    saved = (
        srv.FREIGHT,
        srv._freight_counter,
        srv._freight_dirty,
        srv.FREIGHT_FILE_BLOCKED,
        dict(srv.FREIGHT_STATS),
    )
    try:
        srv.FREIGHT = {}
        srv.FREIGHT_FILE_BLOCKED = False
        poster = srv.Player(
            ws=None,
            id=1,
            name=marker,
            logged_in=False,
            hp=10,
            max_hp=10,
            room="market",
            gold=100,
            inventory=[marker],
        )
        hauler = srv.Player(
            ws=None,
            id=2,
            name="Issue872Hauler",
            logged_in=True,
            hp=10,
            max_hp=10,
            room="market",
            gold=50,
            inventory=[],
        )
        asyncio.run(
            srv.cmd_freight(
                poster,
                {
                    "action": "post",
                    "destination": "smithy",
                    "fee": 20,
                    "items": [marker],
                },
            )
        )
        rec = next(iter(srv.FREIGHT.values()))
        assert rec["items"] == [marker], rec
        deposit = rec["deposit"]
        asyncio.run(srv.cmd_freight(hauler, {"action": "accept", "id": rec["id"]}))
        assert rec["state"] == "carried", rec["state"]
        srv._freight_release_on_death(hauler, "deep_forest")
        assert rec["state"] == "lost", rec["state"]
        assert rec["items"] == [], rec
        assert marker in srv._ground_items("deep_forest")
        expected = 20 + deposit
        assert rec["refund"] == expected, rec["refund"]
        assert srv.save_freight(path) is True
        cid = rec["id"]
        srv.FREIGHT = {}
        loaded = srv.load_freight(path)
        assert cid in loaded, loaded
        assert loaded[cid]["poster"] == marker, loaded[cid]
        assert (
            loaded[cid]["state"] == "lost" and loaded[cid]["refund"] == expected
        ), loaded[cid]
        assert loaded[cid]["items"] == [], loaded[cid]
        srv.FREIGHT = loaded
        poster.gold = 0
        owed = srv._freight_flush_refunds(poster)
        assert owed == expected and poster.gold == expected, (owed, poster.gold)
        assert loaded[cid]["refund"] == 0, loaded[cid]
    finally:
        srv._remove_ground("deep_forest", marker)
        (
            srv.FREIGHT,
            srv._freight_counter,
            srv._freight_dirty,
            srv.FREIGHT_FILE_BLOCKED,
            stats,
        ) = saved
        srv.FREIGHT_STATS.clear()
        srv.FREIGHT_STATS.update(stats)
    print("FREIGHT_DEATH_REFUND_SAVE_LOAD_OK")


with tempfile.TemporaryDirectory() as folder:
    linear_check(folder)
    linear_bad_checkpoint_check(folder)
    torch_check(folder)
    runlog_check(folder)
    runlog_sendback_check(folder)
    runlog_pkg_import_check()
    freight_refund_persistence_check(folder)
    freight_death_refund_save_load_check(folder)
print("PERSISTENCE_ALL_OK")
