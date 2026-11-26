"""Minimal-repro and red-team plugin tests (#624), no live server needed.

Run from the repo root:  python tests/test_repro.py
"""

import json
import os
import sys
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "plugins"))

from ml.repro import (  # noqa: E402
    SCHEMA_VERSION,
    compact_repro,
    read_repro,
    repro_fingerprint,
    write_repro,
)
from ml.plugins import REGISTRY  # noqa: E402
import redteam  # noqa: E402


def make_rows():
    """Four unsorted transcript rows across two incarnations/episodes."""
    return [
        {
            "agent_id": "a1",
            "episode": 1,
            "step": 1,
            "incarnation": 0,
            "elapsed_s": 2.0,
            "action": 3,
            "reward": -1.0,
            "done": False,
            "prev_obs": {"hp": 10, "gold": 5},
            "obs": {"hp": 9, "gold": 5},
            "info": {"reward_vector": {"score": -1.0}, "extra": "drop"},
        },
        {
            "agent_id": "a1",
            "episode": 1,
            "step": 0,
            "incarnation": 0,
            "elapsed_s": 1.0,
            "action": 1,
            "reward": 0.0,
            "done": False,
            "prev_obs": {"hp": 10, "gold": 5},
            "obs": {"hp": 10, "gold": 5},
            "info": {"reward_mode": "score"},
        },
        {
            "agent_id": "a1",
            "episode": 1,
            "step": 0,
            "incarnation": 1,
            "elapsed_s": 3.0,
            "action": 2,
            "reward": 4.5,
            "done": False,
            "prev_obs": {"hp": 8, "gold": 5},
            "obs": {"hp": 8, "gold": 500},
            "info": {"reward_vector": {"gold": 4.5}, "extra": "drop"},
        },
        {
            "agent_id": "b2",
            "episode": 1,
            "step": 0,
            "incarnation": 0,
            "elapsed_s": 0.5,
            "action": 0,
            "reward": 0.0,
            "done": True,
            "prev_obs": {"hp": 10},
            "obs": {"hp": 10},
            "info": {},
        },
    ]


def make_episode_rows():
    """Two episodes of one incarnation, steps restarting at 0, unsorted."""

    def row(episode, step):
        return {
            "agent_id": "a1",
            "episode": episode,
            "step": step,
            "incarnation": 0,
            "action": episode * 10 + step,
            "reward": float(episode),
            "prev_obs": {"hp": 10},
            "obs": {"hp": 10 - step},
        }

    return [row(2, 1), row(1, 0), row(2, 0), row(1, 1)]


def test_compact_repro_filters_sorts_and_diffs():
    rows = make_rows()
    repro = compact_repro(rows, agent_id="a1", episode=1, defect_class="gold_spike")
    assert repro["schema_version"] == SCHEMA_VERSION
    assert repro["defect_class"] == "gold_spike"
    assert repro["agent_id"] == "a1" and repro["episode"] == 1
    assert repro["n_steps"] == 3
    # Ordered by (incarnation, step), independent of file order.
    assert [(s["incarnation"], s["step"]) for s in repro["steps"]] == [
        (0, 0),
        (0, 1),
        (1, 0),
    ]
    first = repro["steps"][0]
    assert first["changed"] == {}, "unchanged observation must have no diff"
    gold_step = repro["steps"][2]
    assert gold_step["changed"] == {"gold": 500}
    assert gold_step["reward"] == 4.5
    assert gold_step["info"] == {"reward_vector": {"gold": 4.5}}
    assert "extra" not in gold_step["info"]
    assert repro["fingerprint"] == repro_fingerprint(repro["steps"])
    print("REPRO_FILTER_SORT_OK")


def test_compact_repro_incarnation_and_max_steps():
    rows = make_rows()
    only_live = compact_repro(rows, agent_id="a1", incarnation=1)
    assert only_live["n_steps"] == 1 and only_live["steps"][0]["reward"] == 4.5
    assert only_live["incarnation"] == 1
    first_two = compact_repro(rows, agent_id="a1", max_steps=2)
    assert first_two["n_steps"] == 2
    assert [(s["incarnation"], s["step"]) for s in first_two["steps"]] == [
        (0, 0),
        (0, 1),
    ]
    print("REPRO_LIMIT_OK")


def test_compact_repro_orders_episodes_before_steps():
    rows = make_episode_rows()
    repro = compact_repro(rows, agent_id="a1")
    assert [(s["episode"], s["step"]) for s in repro["steps"]] == [
        (1, 0),
        (1, 1),
        (2, 0),
        (2, 1),
    ]
    assert "episode" in repro["steps"][0], "each step carries its episode"
    limited = compact_repro(rows, agent_id="a1", max_steps=2)
    assert [(s["episode"], s["step"]) for s in limited["steps"]] == [
        (1, 0),
        (1, 1),
    ]
    # The old (incarnation, step) sort spliced episodes together; it must hash
    # differently from the timeline order so dedup cannot merge the two.
    spliced = sorted(repro["steps"], key=lambda s: (s["incarnation"], s["step"]))
    assert repro["fingerprint"] != repro_fingerprint(spliced)
    print("REPRO_EPISODE_ORDER_OK")


def test_compact_repro_empty_and_bad_rows():
    empty = compact_repro([], agent_id="nobody")
    assert empty["n_steps"] == 0 and empty["steps"] == []
    assert empty["agent_id"] == "nobody" and empty["episode"] is None
    assert empty["fingerprint"] == repro_fingerprint([])
    noisy = compact_repro([None, "bad", {"agent_id": "x", "step": "?"}])
    assert noisy["n_steps"] == 1, "dict rows kept, junk skipped"
    assert noisy["steps"][0]["step"] == "?" and noisy["steps"][0]["reward"] is None
    print("REPRO_EDGE_OK")


def test_repro_round_trip_does_not_overwrite_source():
    with tempfile.TemporaryDirectory() as tmp:
        source = os.path.join(tmp, "measurements-gate-700.json")
        with open(source, "w", encoding="utf-8") as handle:
            handle.write('{"keep": true}\n')
        before = open(source, "rb").read()
        report = compact_repro(make_rows(), agent_id="a1")
        out = os.path.join(tmp, "nested", "finding.json")
        written = write_repro(report, out)
        assert written == os.path.abspath(out)
        assert open(source, "rb").read() == before, "source must be untouched"
        loaded = read_repro(written)
        assert loaded == json.loads(json.dumps(report))
        assert os.path.exists(os.path.dirname(written)), "parent dir created"
    print("REPRO_WRITE_OK")


def test_redteam_plugin_contract():
    assert "redteam" in REGISTRY
    plugin = redteam.RedTeam(seed=1, gold_spike=10.0, profit_spike=10.0)
    # act stays within the valid mask and is deterministic for a fixed seed.
    actions = [plugin.act({"hp": 10}, "a1", [False, True, True]) for _ in range(5)]
    assert all(a in (1, 2) for a in actions), actions
    same = redteam.RedTeam(seed=1, gold_spike=10.0, profit_spike=10.0)
    assert [
        same.act({"hp": 10}, "a1", [False, True, True]) for _ in range(5)
    ] == actions
    assert plugin.act({"hp": 10}, "a1", []) == 0, "empty mask is safe"
    # learn buffers transitions and advances the step counter.
    plugin.learn({"hp": 10}, 1, 0.0, {"hp": 9}, False)
    assert len(plugin._buffer) == 1
    plugin.learn({"hp": 9}, 2, 1.0, {"hp": 8}, True)
    assert plugin._episode == 2 and len(plugin._buffer) == 2
    # No spike -> abstain, no finding. A gold spike -> one finding with a repro.
    assert plugin.reward({"gold_delta": 2.0, "profit": 1.0}) is None
    assert plugin.findings() == []
    assert plugin.reward({"gold_delta": 99.0, "profit": 0.0}) is None
    findings = plugin.findings()
    assert len(findings) == 1 and findings[0]["defect_class"] == "gold_spike"
    assert findings[0]["value"] == 99.0
    assert findings[0]["repro"]["n_steps"] == 2
    assert findings[0]["trigger"]["gold_delta"] == 99.0
    assert plugin.reward({"gold_delta": 0.0, "profit": 55.0}) is None
    assert plugin.findings()[1]["defect_class"] == "profit_spike"
    assert len(plugin.drain_findings()) == 2 and plugin.findings() == []
    print("REDTEAM_PLUGIN_OK")


def test_redteam_finding_buffer_is_bounded():
    plugin = redteam.RedTeam(gold_spike=1.0, max_findings=3)
    for _ in range(10):
        plugin.reward({"gold_delta": 5.0})
    assert len(plugin.findings()) == 3
    # Unknown config is refused by the base contract.
    try:
        redteam.RedTeam(not_a_param=1)
    except ValueError:
        pass
    else:
        raise AssertionError("unknown config must be refused")
    print("REDTEAM_BOUNDED_OK")


def test_redteam_writes_retrievable_findings_to_out_dir():
    # The headline behavior must survive process exit: with out_dir configured,
    # each finding becomes its own on-disk file, and a second finding (even an
    # identical one, or one from a restarted instance) never overwrites it.
    with tempfile.TemporaryDirectory() as tmp:
        plugin = redteam.RedTeam(out_dir=tmp, gold_spike=10.0)
        plugin.learn({"hp": 10}, 1, 0.0, {"hp": 9}, False)
        assert plugin.reward({"gold_delta": 20.0}) is None
        names = sorted(os.listdir(tmp))
        assert len(names) == 1, names
        assert names[0].endswith(".json")
        saved = read_repro(os.path.join(tmp, names[0]))
        assert saved["defect_class"] == "gold_spike"
        assert saved["value"] == 20.0
        assert saved["repro"]["n_steps"] == 1
        assert os.path.basename(saved["path"]) == names[0]

        # An identical finding gets a distinct file, not an overwrite.
        plugin.reward({"gold_delta": 20.0})
        names = sorted(os.listdir(tmp))
        assert len(names) == 2 and names[0] != names[1], names
        before = {name: open(os.path.join(tmp, name), "rb").read() for name in names}

        # A fresh instance (PBT restart) must not clobber earlier evidence.
        restarted = redteam.RedTeam(out_dir=tmp, gold_spike=10.0)
        restarted.learn({"hp": 10}, 1, 0.0, {"hp": 9}, False)
        restarted.reward({"gold_delta": 20.0})
        after = {name: open(os.path.join(tmp, name), "rb").read() for name in names}
        assert after == before, "earlier finding files must be untouched"
        assert len(os.listdir(tmp)) == 3, sorted(os.listdir(tmp))
    print("REDTEAM_OUT_DIR_OK")


def main():
    test_compact_repro_filters_sorts_and_diffs()
    test_compact_repro_incarnation_and_max_steps()
    test_compact_repro_orders_episodes_before_steps()
    test_compact_repro_empty_and_bad_rows()
    test_repro_round_trip_does_not_overwrite_source()
    test_redteam_plugin_contract()
    test_redteam_finding_buffer_is_bounded()
    test_redteam_writes_retrievable_findings_to_out_dir()
    print("ALL_REPRO_OK")


if __name__ == "__main__":
    main()
