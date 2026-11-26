"""Balance gate harness unit tests (#537): no server, no torch.

Covers the evaluation half only, which is the half that must be trusted:
the conservation identity, treasury/market/combat metric math, threshold
resolution and profile overrides, the report schema, the PASS/FAIL exit
code of ``evaluate``, cross-run determinism, and the scenario matrix's
declared skips.

Run from the repo root:  python tests/test_balance_harness.py
"""

import asyncio
import contextlib
import copy
import io
import json
import os
import sys
import tempfile
from types import SimpleNamespace

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import ml.balance as bal  # noqa: E402


def _player(name, deaths, level=1, wild_deaths=None, civ_deaths=None, kills=None):
    if wild_deaths is None and civ_deaths is None:
        civ_deaths = deaths // 2
    return {
        "name": name,
        "level": level,
        "deaths": deaths,
        "kills": deaths * 2 if kills is None else kills,
        "wild_deaths": (
            deaths - (civ_deaths or 0) if wild_deaths is None else wild_deaths
        ),
        "civ_deaths": civ_deaths or 0,
        "gold": 5,
    }


def make_samples(alive=8, final_deaths=(2, 2), craft_seq=(3,)):
    """Three samples: spawn, steady state, end. Two tracked players."""
    base = {
        "server": {"players_online": 2},
        "market": {
            "reserve": 500.0,
            "tax_effective_pct": 22.22,
            "trade_count": 0,
            "sunk_lifetime": 0.0,
            "orders": [],
            "bids": [],
            "history": [],
        },
        "quests": {"completions": {"wolf": 0}},
        "craft_history": [],
        "dungeon_clears": {"solo_clears": 0, "group_clears": 0},
        "scores": [],
        "npc_ai": {
            "since": 1000.0,
            "aggro_acquired": 5,
            "target_switched": 2,
            "target_cleared": 1,
            "leash_broken": 1,
            "flee_started": 2,
            "flee_succeeded": 1,
            "time_in_combat_s": 12.5,
        },
    }

    def sample(
        t, deaths, treasury, sunk, trades, orders=(), bids=(), history=(), craft=()
    ):
        state = copy.deepcopy(base)
        state["players"] = [
            _player("alpha", deaths[0]),
            _player("beta", deaths[1]),
        ]
        state["market"].update(
            market_treasury=treasury,
            treasury=treasury,
            sunk_lifetime=sunk,
            trade_count=trades,
            orders=list(orders),
            bids=list(bids),
            history=list(history),
        )
        state["craft_history"] = [{"seq": seq} for seq in craft]
        state["quests"] = {"completions": {"wolf": int(trades)}}
        return {
            "t": t,
            "alive": alive,
            "episodes": int(t // 100),
            "state": state,
            "tables": {"treasury": treasury, "treasury_lifetime": sunk},
        }

    book_ask = [{"item": "Wolf Pelt", "price": 110.0}]
    book_bid = [{"item": "Wolf Pelt", "price": 100.0}]
    trades = [{"buyer": "alpha", "seller": "beta", "item": "Wolf Pelt", "price": 100}]
    return [
        sample(0.0, (0, 0), 500.0, 0.0, 0),
        sample(1800.0, (1, 1), 510.0, 10.0, 6, book_ask, book_bid, trades),
        sample(
            2400.0,
            final_deaths,
            505.0,
            15.0,
            12,
            book_ask,
            book_bid,
            trades,
            craft=craft_seq,
        ),
    ]


LEDGER = {
    "items": {"created": 10, "consumed": 3, "destroyed": 1, "held": 6},
    "gold": {"created": 100, "consumed": 20, "destroyed": 10, "held": 70},
    "craft_input_loss": 0,
    "negative_inventory": 0,
    "craft_refunded_total": 3,
    "craft_escrowed_total": 5,
}

TELEMETRY = {"companion_damage_share_pct": 25.0}

# What the guild probe leaves in /api/state while its vault still holds gold
# and an item: deposit 6, withdraw 2, six audit entries, two members.
GUILD_PEAK = {
    "count": 1,
    "members_total": 2,
    "online_members": 2,
    "largest": 2,
    "vault_gold_total": 4,
    "vault_items_total": 1,
    "invites_pending": 0,
    "audit_entries": 6,
    "vault_gold_in_total": 6,
    "vault_gold_out_total": 2,
    "audit_entries_written": 6,
}
GUILD_PROBE = {
    "guild": "Probe Guild 700001234",
    "item": "Iron Ore",
    "steps": 15,
    "gold_conserved": True,
    "problems": [],
    "skipped": "",
    "error": "",
}

# What the arena probe leaves behind after two scripted matches (#656): the
# rating delta is a transfer, and no gold, item or death counter moved.
ARENA_PROBE = {
    "pairs_matched": True,
    "matches_completed": 2,
    "rating_delta_sum": 0.0,
    "rating_conserved": True,
    "gold_delta": 0.0,
    "items_delta": 0,
    "deaths_delta": 0,
    "civ_deaths_delta": 0,
    "wild_deaths_delta": 0,
    "determinism": True,
    "fingerprint": "0f1e2d3c4b5a6978",
    "fingerprint_second": "0f1e2d3c4b5a6978",
    "steps": 18,
    "problems": [],
    "skipped": False,
    "skip_reason": None,
    "error": None,
}

ARENA_SNAPSHOT = {
    "version": 1,
    "queues": {"1v1": 0, "2v2": 0},
    "live": [],
    "ladder": [],
    "ladder_size": 8,
    "ranked_size": 0,
    "provisional_size": 8,
    "min_matches": 5,
    "recent": [],
    "rating_clamps": 0,
}


def with_arena(samples, probe=None):
    """Attach the probe's ledger to the final sample, the way ``collect`` does.

    In place on purpose: the arena probe runs before the closing sample, so
    its rows share that timestamp and do not stretch the run's agent-hours.
    """
    samples[-1]["state"]["arena"] = dict(ARENA_SNAPSHOT)
    samples[-1]["state"]["arena_probe"] = dict(ARENA_PROBE if probe is None else probe)
    return samples


def with_guilds(samples, peak=None, probe=None, empty_after=False):
    """Add the probe's peak sample, the way ``collect`` does."""
    sample = copy.deepcopy(samples[-1])
    sample["t"] = round(sample["t"] + 10.0, 1)
    sample["state"]["guilds"] = dict(GUILD_PEAK if peak is None else peak)
    sample["state"]["guild_probe"] = dict(GUILD_PROBE if probe is None else probe)
    samples.append(sample)
    if empty_after:
        # The probe disbands, so the last sample always shows an empty world.
        final = copy.deepcopy(sample)
        final["t"] = round(sample["t"] + 10.0, 1)
        final["state"]["guilds"] = {
            "count": 0,
            "members_total": 0,
            "online_members": 0,
            "largest": 0,
            "vault_gold_total": 0,
            "vault_items_total": 0,
            "invites_pending": 0,
            "audit_entries": 0,
            "vault_gold_in_total": 6,
            "vault_gold_out_total": 6,
            "audit_entries_written": 8,
        }
        samples.append(final)
    return samples


def make_measurements(profile="smoke", seed=700, alive=8):
    samples = with_arena(with_guilds(make_samples(alive=alive)))
    telemetry = dict(TELEMETRY)
    telemetry.update(bal._telemetry(samples))
    return {
        "meta": {"profile": profile, "seed": seed, "agents": 10, "duration_s": 2400},
        "samples": samples,
        "derived": bal.derive(samples, LEDGER, telemetry),
    }


def _failing(report):
    return set(report["summary"]["failing"])


# --- conservation math ------------------------------------------------------


def test_conservation_identity():
    assert bal.conservation_drift(10, 3, 1, 6) == 0
    assert bal.conservation_drift(10, 3, 1, 7) == -1
    assert bal.conservation_drift(10, 3, 1, 5) == 1
    leaked = dict(
        LEDGER, items={"created": 10, "consumed": 3, "destroyed": 1, "held": 6.5}
    )
    derived = bal.derive(make_samples(), leaked, TELEMETRY)
    assert derived["conservation"]["items_drift"] == -0.5
    report = bal.evaluate_run(
        {"meta": {}, "samples": [], "derived": derived},
        bal.load_thresholds(profile="smoke"),
        "smoke",
        700,
    )
    # -0.5 is as wrong as +0.5: the identity allows no drift either way.
    assert "conservation.items_drift" in _failing(report)
    print("CONSERVATION_IDENTITY_OK")


# --- derive ---------------------------------------------------------------


def test_derive_metrics():
    derived = bal.derive(make_samples(), LEDGER, TELEMETRY)
    assert derived["treasury"]["reserve"] == 500.0
    assert derived["treasury"]["sunk_start"] == 0.0
    assert derived["treasury"]["sunk_end"] == 15.0
    assert derived["treasury"]["tax_effective_pct"] == 22.22
    assert derived["treasury"]["series"][-1] == {"t": 2400.0, "value": 505.0}
    # Two players x 2400s = 1.333 agent-hours; four deaths => 3.0/h.
    assert derived["combat"]["deaths"] == 4
    assert abs(derived["combat"]["agent_hours"] - 1.333) < 0.01
    assert abs(derived["combat"]["deaths_per_hour"] - 3.0) < 0.01
    # Traffic splits evenly: two deaths in wild rooms, two in civilian ones.
    assert abs(derived["combat"]["wild_deaths_per_hour"] - 1.5) < 0.01
    assert abs(derived["combat"]["civ_deaths_per_hour"] - 1.5) < 0.01
    assert derived["combat"]["civ_death_share_pct"] == 50.0
    assert derived["combat"]["kills"] == 8
    assert derived["combat"]["deaths_per_kill"] == 0.5
    assert derived["combat"]["companion_damage_share_pct"] == 25.0
    # (ask 110, bid 100) => 9.52% spread; trade_count 0 -> 12.
    assert derived["market"]["median_spread_pct"] == 9.524
    assert derived["market"]["trade_volume"] == 12
    assert derived["market"]["wash_trades"] == 0
    assert derived["craft"]["completions"] == 1
    assert derived["craft"]["refund_excess"] == 0
    assert derived["progression"]["quest_completions"] == 12
    assert derived["liveness"]["min_alive"] == 8
    assert derived["liveness"]["final_alive"] == 8
    assert derived["determinism"]["fingerprint"]
    print("DERIVE_METRICS_OK")


def test_derive_without_players_does_not_crash():
    # A run whose samples carry no player roster (server restart mid-run,
    # bots all offline) must still derive; last_players defaults empty.
    samples = make_samples()
    for sample in samples:
        sample["state"]["players"] = []
    derived = bal.derive(samples, LEDGER, TELEMETRY)
    assert derived["combat"]["deaths_per_hour"] is None
    assert derived["liveness"]["final_alive"] == 8
    print("DERIVE_NO_PLAYERS_OK")


def test_zone_deaths_accumulate_deltas():
    # Bot names are reused, so a run starts with lifetime counters already
    # on the books: only the growth inside the window may be scored.
    samples = make_samples()
    for index, sample in enumerate(samples):
        grew = 1 if index else 0
        for player in sample["state"]["players"]:
            player["deaths"] = 100 + grew
            player["wild_deaths"] = 60 + grew
            player["civ_deaths"] = 40
    derived = bal.derive(samples, LEDGER, TELEMETRY)
    assert derived["combat"]["deaths"] == 2
    assert abs(derived["combat"]["wild_deaths_per_hour"] - 1.5) < 0.01
    assert derived["combat"]["civ_death_share_pct"] == 0.0
    print("ZONE_DEATH_DELTA_OK")


def test_missing_zone_counters_skip_not_fail():
    samples = make_samples()
    for sample in samples:
        for player in sample["state"]["players"]:
            player.pop("wild_deaths")
            player.pop("civ_deaths")
    derived = bal.derive(samples, LEDGER, TELEMETRY)
    assert derived["combat"]["wild_deaths_per_hour"] is None
    assert derived["combat"]["civ_death_share_pct"] is None
    report = bal.evaluate_run(
        {"meta": {}, "samples": [], "derived": derived},
        bal.load_thresholds(profile="smoke"),
        "smoke",
        700,
    )
    statuses = {c["id"]: c["status"] for c in report["checks"]}
    assert statuses["combat.civ_death_share"] == "skip"
    assert statuses["combat.wild_deaths_per_hour"] == "skip"
    print("ZONE_TELEMETRY_ABSENT_SKIP_OK")


def test_derive_without_ledger_skips_not_passes():
    derived = bal.derive(make_samples())
    assert derived["conservation"]["items_drift"] is None
    assert derived["craft"]["input_loss"] is None
    report = bal.evaluate_run(
        {"meta": {}, "samples": [], "derived": derived},
        bal.load_thresholds(profile="smoke"),
        "smoke",
        700,
    )
    statuses = {c["id"]: c["status"] for c in report["checks"]}
    assert statuses["conservation.items_drift"] == "skip"
    assert statuses["conservation.gold_drift"] == "skip"
    # Absence of a ledger cannot turn the rest of the smoke gate green.
    assert "conservation.items_drift" not in _failing(report)
    print("NO_LEDGER_SKIP_OK")


def test_zero_craft_and_unexercised_sink():
    samples = make_samples(craft_seq=())
    for index, treasury in enumerate((0.0, 10.0, 17.0)):
        samples[index]["state"]["market"]["treasury"] = treasury
        samples[index]["state"]["market"]["sunk_lifetime"] = 0.0
        samples[index]["tables"]["treasury"] = treasury
    derived = bal.derive(samples, LEDGER, TELEMETRY)
    # An empty feed is a measured zero, not missing data.
    assert derived["craft"]["completions"] == 0
    report = bal.evaluate_run(
        {"meta": {}, "samples": [], "derived": derived},
        bal.load_thresholds(profile="smoke"),
        "smoke",
        700,
    )
    statuses = {c["id"]: c["status"] for c in report["checks"]}
    assert statuses["craft.completions"] == "pass"
    # Treasury never reached the reserve, so the sink path is untested.
    assert statuses["treasury.sink_grew"] == "skip"
    print("ZERO_CRAFT_SINK_SKIP_OK")


def test_wash_trade_detected():
    samples = make_samples()
    samples[-1]["state"]["market"]["history"] = [
        {"buyer": "alpha", "seller": "alpha", "item": "Wolf Pelt", "price": 5}
    ]
    derived = bal.derive(samples, LEDGER, TELEMETRY)
    assert derived["market"]["wash_trades"] == 1
    report = bal.evaluate_run(
        {"meta": {}, "samples": [], "derived": derived},
        bal.load_thresholds(profile="smoke"),
        "smoke",
        700,
    )
    assert "market.wash_trades" in _failing(report)
    print("WASH_TRADE_OK")


# --- evaluation / report schema -------------------------------------------


def test_evaluate_pass_and_schema():
    measurements = make_measurements()
    thresholds = bal.load_thresholds(profile="smoke")
    report = bal.evaluate_run(measurements, thresholds, "smoke", 700)
    assert report["verdict"] == "PASS", report["summary"]
    assert set(report) >= {"seed", "verdict", "checks", "scenarios", "summary"}
    assert report["summary"]["fail"] == 0
    ids = {check["id"] for check in report["checks"]}
    for expected in (
        "conservation.items_drift",
        "treasury.band",
        "combat.civ_deaths_per_hour",
        "combat.civ_death_share",
        "combat.wild_deaths_per_hour",
        "combat.deaths_per_kill",
        "combat.deaths_per_hour",
        "craft.completions",
        "acquisition.drop_rate",
        "market.median_spread",
        "liveness.min_agents",
    ):
        assert expected in ids, expected
    for check in report["checks"]:
        assert set(check) >= {
            "id",
            "scenario",
            "metric",
            "op",
            "value",
            "limit",
            "status",
            "blocking",
            "provisional",
        }
        assert check["status"] in ("pass", "fail", "skip")

    profile = bal.evaluate_profile(
        [{"seed": 700, "measurements": measurements}], thresholds, "smoke"
    )
    assert profile["schema"] == bal.REPORT_SCHEMA
    assert profile["profile"] == "smoke"
    assert profile["provisional_thresholds"] is True
    assert profile["thresholds"]["treasury"]["band_pct_of_reserve"] == 10.0
    assert profile["seeds"] == [700]
    assert profile["summary"]["verdict"] == "PASS"
    assert profile["scenarios"]
    print("EVALUATE_PASS_SCHEMA_OK")


def test_evaluate_fail_paths():
    measurements = make_measurements()
    derived = copy.deepcopy(measurements["derived"])
    derived["combat"]["civ_death_share_pct"] = 99.0
    derived["market"]["median_spread_pct"] = 40.0
    derived["treasury"]["reserve"] = 1000.0
    broken = dict(measurements, derived=derived)
    report = bal.evaluate_run(
        broken, bal.load_thresholds(profile="smoke"), "smoke", 700
    )
    failing = _failing(report)
    assert report["verdict"] == "FAIL"
    assert {
        "combat.civ_death_share",
        "market.median_spread",
        "treasury.band",
    } <= failing, failing
    assert report["summary"]["fail"] == len(failing)
    print("EVALUATE_FAIL_OK")


def test_empty_run_cannot_pass_gate():
    empty = {"meta": {"profile": "smoke", "seed": 700}, "samples": [], "derived": {}}
    report = bal.evaluate_run(empty, bal.load_thresholds(profile="smoke"), "smoke", 700)
    assert report["verdict"] == "FAIL"
    failing = _failing(report)
    for scenario in ("combat", "craft", "market", "liveness"):
        assert f"coverage.{scenario}" in failing, (scenario, failing)
    # Economy's thresholds need a 30-minute window, so smoke records it as
    # observed/skipped instead of failing coverage on a 10-minute run.
    assert "coverage.economy" not in failing
    print("EMPTY_RUN_FAILS_OK")


def test_gate_coverage_guard():
    # Every scenario the gate claims to cover must fail on an empty run.
    # Without this, a scenario can silently drop out of coverage and the
    # gate passes on absence of evidence (#589).
    empty = {"meta": {"profile": "gate", "seed": 700}, "samples": [], "derived": {}}
    report = bal.evaluate_run(empty, bal.load_thresholds(profile="gate"), "gate", 700)
    failing = _failing(report)
    for scenario in bal.PROFILES["gate"]["coverage"]:
        assert f"coverage.{scenario}" in failing, (scenario, failing)
    # npc_ai_v2 landed with #536 and measures in the gate now; on an empty
    # run it is still skipped for lack of telemetry, and the guard names that.
    row = next(r for r in report["scenarios"] if r["name"] == "npc_ai_v2")
    assert row["status"] == "skipped"
    assert "no measurement" in row["reason"], row
    print("GATE_COVERAGE_GUARD_OK")


def test_acquisition_telemetry_survives_trim_and_derive():
    # A counter added to /api/state must reach derive() through _trim_state;
    # the trim is the silent-drop site this test pins down.
    samples = make_samples()
    acquisition = {
        "sources": {
            "boss": {
                "earned": 3,
                "attempts": 6,
                "first_earn_at": 123.0,
                "first_earn_s": 45.0,
            },
            "craft": {
                "earned": 1,
                "attempts": 4,
                "first_earn_at": 200.0,
                "first_earn_s": 300.0,
            },
            "shop": {
                "earned": 1,
                "attempts": 2,
                "first_earn_at": 400.0,
                "first_earn_s": 900.0,
            },
            "adopt": {
                "earned": 2,
                "attempts": 3,
                "first_earn_at": 500.0,
                "first_earn_s": 1200.0,
            },
        },
        "adoptions": {"earned": 2, "gm": 0},
        "craft_cost_ratio": 0.92,
        "not_in_build": {"quest": "no quest rewards a companion deed"},
    }
    samples[-1]["state"]["acquisition"] = acquisition
    trimmed = bal._trim_state(samples[-1]["state"])
    assert trimmed["acquisition"] == acquisition
    telemetry = bal._telemetry(samples)
    assert telemetry["drop_rate"] == 0.5
    assert telemetry["craft_cost_ratio"] == 0.92
    assert telemetry["shop_time_to_earn_s"] == 900.0
    derived = bal.derive(samples, LEDGER, telemetry)
    assert derived["acquisition"]["drop_rate"] == 0.5
    assert derived["acquisition"]["sources"]["boss"]["earned"] == 3
    assert derived["acquisition"]["adoptions"]["gm"] == 0
    assert derived["acquisition"]["not_in_build"]["quest"]
    report = bal.evaluate_run(
        {"meta": {}, "samples": [], "derived": derived},
        bal.load_thresholds(profile="gate"),
        "gate",
        700,
    )
    rows = {row["id"]: row for row in report["checks"]}
    assert rows["acquisition.drop_rate"]["status"] == "pass"
    assert "coverage.acquisition" not in _failing(report)
    print("ACQUISITION_TELEMETRY_OK")


def test_death_log_survives_trim_and_attributes_deaths():
    samples = make_samples()
    first = {
        "t": 100.1,
        "player": "alpha",
        "level": 4,
        "room": "wild_woods",
        "killer": "Dire Wolf",
        "killer_room": "wild_woods",
        "dmg": 12,
        "hp_before": 8,
    }
    second = dict(first, t=101.2, player="beta", room="old_town", killer="Guard")
    samples[0]["state"]["death_log"] = [first]
    samples[1]["state"]["death_log"] = [first, second]
    samples[2]["state"]["death_log"] = [first, second]

    trimmed = bal._trim_state(samples[1]["state"])
    assert trimmed["death_log"] == [first, second]
    telemetry = bal._telemetry(samples)
    assert telemetry["death_attribution"]["count"] == 2
    assert telemetry["death_attribution"]["by_room"] == {
        "old_town": 1,
        "wild_woods": 1,
    }
    assert telemetry["death_attribution"]["by_killer"] == {
        "Dire Wolf": 1,
        "Guard": 1,
    }
    derived = bal.derive(samples, LEDGER, telemetry)
    assert derived["combat"]["death_attribution"] == telemetry["death_attribution"]
    print("DEATH_LOG_ATTRIBUTION_OK")


def test_death_log_is_bounded_deduplicated_and_restart_safe():
    samples = make_samples()
    rows = [
        {
            "id": f"boot-a:{i}",
            "t": 1.0,
            "player": f"player-{i}",
            "room": "same_room" if i % 2 else "other_room",
            "killer": None if i % 2 else "Wolf",
        }
        for i in range(401)
    ]
    state = dict(samples[0]["state"], death_log=rows + ["not-an-event"])
    seen = set()
    first = bal._trim_state(state, seen)
    second = bal._trim_state(state, seen)
    assert len(first["death_log"]) == 399
    assert second["death_log"] == []
    assert first["death_log"][0]["id"] == "boot-a:2"
    assert first["death_log"][-1]["id"] == "boot-a:400"

    restarted = dict(samples[1])
    restarted["state"] = dict(
        samples[1]["state"],
        death_log=[
            {
                "id": "boot-b:1",
                "t": 1.0,
                "player": "player-0",
                "room": "same_room",
                "killer": None,
            }
        ],
    )
    samples = [
        {"state": {"death_log": [first["death_log"][0]]}},
        restarted,
    ]
    attribution = bal._death_attribution(samples)
    assert attribution["count"] == 2
    assert attribution["by_room"] == {"other_room": 1, "same_room": 1}
    assert attribution["by_killer"] == {"Wolf": 1, "unknown": 1}
    assert "events" not in attribution
    print("DEATH_LOG_BOUNDS_AND_IDS_OK")


def test_death_attribution_tie_order_is_stable():
    samples = [
        {
            "state": {
                "death_log": [
                    {"id": "1", "room": "z_room", "killer": "Wolf"},
                    {"id": "2", "room": "a_room", "killer": "Bear"},
                ]
            }
        }
    ]
    result = bal._death_attribution(samples)
    assert list(result["by_room"]) == ["a_room", "z_room"]
    assert list(result["by_killer"]) == ["Bear", "Wolf"]
    print("DEATH_ATTRIBUTION_TIE_ORDER_OK")


def test_npc_ai_telemetry_survives_trim_and_derive():
    # The same pin for the NPC AI v2 counters: they must reach derive() from
    # a sample state just like acquisition's do, or the gate measures zero.
    samples = make_samples()
    npc_ai = {
        "since": 1000.0,
        "aggro_acquired": 5,
        "target_switched": 2,
        "target_cleared": 1,
        "leash_broken": 1,
        "flee_started": 2,
        "flee_succeeded": 1,
        "time_in_combat_s": 12.5,
    }
    samples[-1]["state"]["npc_ai"] = npc_ai
    trimmed = bal._trim_state(samples[-1]["state"])
    assert trimmed["npc_ai"] == npc_ai
    telemetry = bal._telemetry(samples)
    assert telemetry["npc_ai"]["aggro_acquired"] == 5
    derived = bal.derive(samples, LEDGER, telemetry)
    npc_ai_v2 = derived["npc_ai_v2"]
    assert npc_ai_v2["telemetry_present"] is True
    assert npc_ai_v2["aggro_acquired"] == 5
    assert npc_ai_v2["leash_broken"] == 1
    assert npc_ai_v2["flee_delta"] == -1.0
    report = bal.evaluate_run(
        {"meta": {}, "samples": [], "derived": derived},
        bal.load_thresholds(profile="gate"),
        "gate",
        700,
    )
    rows = {row["id"]: row for row in report["checks"]}
    assert rows["npc_ai_v2.telemetry_present"]["status"] == "pass"
    assert rows["npc_ai_v2.aggro_acquired"]["status"] == "pass"
    assert rows["npc_ai_v2.flee_consistency"]["status"] == "pass"
    assert "coverage.npc_ai_v2" not in _failing(report)
    print("NPC_AI_TELEMETRY_OK")


def test_guilds_telemetry_survives_trim_and_derive():
    # The same pin as acquisition, plus the peak rule: the probe disbands, so
    # the last sample is an empty world and must not be the one that counts.
    samples = with_guilds(make_samples(), empty_after=True)
    trimmed = bal._trim_state(samples[-2]["state"])
    assert trimmed["guilds"] == GUILD_PEAK
    assert trimmed["guild_probe"]["gold_conserved"] is True
    telemetry = bal._telemetry(samples)
    assert telemetry["guilds"]["count"] == 1
    assert telemetry["guilds"]["vault_gold_out_total"] == 2
    derived = bal.derive(samples, LEDGER, dict(TELEMETRY, **telemetry))
    guilds = derived["guilds"]
    assert guilds["count"] == 1
    assert guilds["members"] == 2
    assert guilds["vault_gold_in"] == 6
    assert guilds["vault_gold_out"] == 2
    assert guilds["vault_gold_held"] == 4
    assert guilds["vault_items"] == 1
    assert guilds["audit_entries"] == 6
    assert guilds["conservation_drift"] == 0
    assert guilds["gold_conserved"] is True
    assert guilds["skip_reason"] == ""
    report = bal.evaluate_run(
        {"meta": {}, "samples": [], "derived": derived},
        bal.load_thresholds(profile="gate"),
        "gate",
        700,
    )
    rows = {row["id"]: row for row in report["checks"]}
    for check_id in (
        "guilds.count",
        "guilds.members",
        "guilds.vault_gold_in",
        "guilds.vault_gold_out",
        "guilds.vault_items",
        "guilds.audit_entries",
        "guilds.conservation",
        "guilds.gold_conserved",
    ):
        assert rows[check_id]["status"] == "pass", (check_id, rows[check_id])
    assert "coverage.guilds" not in _failing(report)
    print("GUILDS_TELEMETRY_OK")


def test_guild_telemetry_ignores_a_bot_founded_guild():
    # #688: on seed 700 a bot-founded one-member guild pair outranked the
    # probe on guild count while holding nothing, so the vault rows read zero
    # for a probe that had moved gold. The probe's own peak read decides.
    samples = with_guilds(make_samples())
    bot = {
        "count": 2,
        "members_total": 2,
        "online_members": 2,
        "largest": 1,
        "vault_gold_total": 0,
        "vault_items_total": 0,
        "invites_pending": 0,
        "audit_entries": 2,
        "vault_gold_in_total": 0,
        "vault_gold_out_total": 0,
        "audit_entries_written": 13,
    }
    hijack = copy.deepcopy(samples[0])
    hijack["t"] = 1621.5
    hijack["state"]["guilds"] = bot
    samples.insert(0, hijack)
    telemetry = bal._telemetry(samples)
    assert telemetry["guilds"]["count"] == 1, telemetry["guilds"]
    assert telemetry["guilds"]["vault_gold_in_total"] == 6
    derived = bal.derive(samples, LEDGER, dict(TELEMETRY, **telemetry))
    guilds = derived["guilds"]
    assert guilds["vault_gold_in"] == 6, guilds
    assert guilds["vault_gold_out"] == 2, guilds
    assert guilds["vault_items"] == 1, guilds
    assert guilds["audit_entries"] == 6, guilds
    # The retired rule picked the bot sample; nothing else explains the zero.
    assert bal._guild_telemetry([hijack])["guilds"]["count"] == 2
    print("GUILD_TELEMETRY_NO_HIJACK_OK")


def test_guild_absence_and_zero_world_fail_gate():
    # A covered scenario that ships but measures nothing must fail, never skip
    # (#589): null, an explicitly skipped probe, and a zero world all FAIL.
    thresholds = bal.load_thresholds(profile="gate")
    null = bal.derive(make_samples(), LEDGER, {})
    assert null["guilds"]["count"] is None
    report = bal.evaluate_run(
        {"meta": {}, "samples": [], "derived": null}, thresholds, "gate", 700
    )
    failing = _failing(report)
    assert "coverage.guilds" in failing, failing
    row = next(r for r in report["scenarios"] if r["name"] == "guilds")
    assert row["status"] == "skipped", row

    # The probe reports the absent #588 surface; the gate still fails, and the
    # scenario says which ticket would land it.
    skip = bal.derive(
        make_samples(),
        LEDGER,
        {"guild_probe": {"skipped": "guilds not in this build"}},
    )
    report = bal.evaluate_run(
        {"meta": {}, "samples": [], "derived": skip}, thresholds, "gate", 700
    )
    assert "coverage.guilds" in _failing(report)
    row = next(r for r in report["scenarios"] if r["name"] == "guilds")
    assert row["status"] == "skipped", row
    assert bal.GUILD_SURFACE_ISSUE in row["reason"], row

    # A chartered but empty world moves no gold: the floors fail, not skip.
    zero = dict(
        GUILD_PEAK,
        count=0,
        members_total=0,
        vault_gold_total=0,
        vault_items_total=0,
        vault_gold_in_total=0,
        vault_gold_out_total=0,
        audit_entries=0,
    )
    samples = with_guilds(make_samples(), peak=zero)
    telemetry = bal._telemetry(samples)
    derived = bal.derive(samples, LEDGER, dict(TELEMETRY, **telemetry))
    report = bal.evaluate_run(
        {"meta": {}, "samples": [], "derived": derived}, thresholds, "gate", 700
    )
    failing = _failing(report)
    for check_id in ("guilds.count", "guilds.vault_gold_in", "guilds.audit_entries"):
        assert check_id in failing, (check_id, failing)
    print("GUILDS_ABSENCE_FAILS_OK")


def test_profile_overrides_and_threshold_file():
    short = make_measurements()
    short["meta"]["duration_s"] = 600
    # Smoke keeps the 30-minute convergence window (a 10-minute run cannot
    # reach it, so the check reports skip instead of a fake pass) and
    # tolerates zero craft completions in 10 minutes.
    smoke = bal.load_thresholds(profile="smoke")
    assert smoke["treasury"]["steady_after_s"] == 1800.0
    assert smoke["craft"]["min_completions"] == 0
    assert bal.load_thresholds(profile="gate")["craft"]["min_completions"] == 1
    # A stored override wins over the profile default.
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "thresholds.json")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump({"combat": {"civ_death_share_pct_max": 99.0}}, fh)
        overridden = bal.load_thresholds(path, "smoke")
    assert overridden["combat"]["civ_death_share_pct_max"] == 99.0
    assert overridden["combat"]["deaths_per_kill_max"] == 1.5
    # A typo in a threshold path is an error, never a silent pass.
    try:
        bal.resolve_threshold(overridden, ("combat", "nope"))
        raise AssertionError("missing threshold must raise")
    except KeyError:
        print("THRESHOLD_OVERRIDE_OK")


def test_stability_thresholds_are_resolvable_and_flip_verdicts():
    profile = bal.load_thresholds(profile="stability")
    stability = profile["stability"]
    names = (
        "economic_slope_max",
        "level_slope_max",
        "score_slope_max",
        "min_population_ratio",
    )
    for name in names:
        assert bal.resolve_threshold(profile, ("stability", name)) == stability[name]
        assert name not in profile
    assert stability["min_population_ratio"] == 0.8

    measurements = make_measurements(profile="stability", alive=16)
    measurements["derived"]["stability"].update(
        economic_slope=-0.0005,
        level_slope=-0.0005,
        score_slope=-0.0005,
        population_ratio=0.75,
    )
    ids = (
        "stability.economic_drift",
        "stability.level_decline",
        "stability.score_decline",
        "stability.population_vitality",
    )
    before = bal.evaluate_run(measurements, profile, "stability", 700)
    assert before["verdict"] == "FAIL"
    for check_id in ids:
        row = next(c for c in before["checks"] if c["id"] == check_id)
        assert row["status"] == "fail"
        assert not isinstance(row["limit"], tuple)

    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "thresholds.json")
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(
                {
                    "stability": {
                        "economic_slope_max": -0.001,
                        "level_slope_max": -0.001,
                        "score_slope_max": -0.001,
                        "min_population_ratio": 0.5,
                    }
                },
                fh,
            )
        relaxed = bal.load_thresholds(path, "stability")
    after = bal.evaluate_run(measurements, relaxed, "stability", 700)
    assert after["verdict"] == "PASS"
    assert not _failing(after)
    assert set(ids) <= _failing(before)
    assert bal.resolve_threshold(relaxed, ("stability", "min_population_ratio")) == 0.5
    print("STABILITY_THRESHOLDS_OK")


# --- determinism ----------------------------------------------------------


def test_determinism_repeats():
    measurements = make_measurements(profile="gate", alive=16)
    thresholds = bal.load_thresholds(profile="gate")
    same = bal.evaluate_profile(
        [
            {"seed": 700, "measurements": measurements},
            {"seed": 700, "measurements": copy.deepcopy(measurements)},
        ],
        thresholds,
        "gate",
    )
    det = [c for c in same["checks"] if c["id"] == "determinism.same_seed.700"]
    assert len(det) == 1 and det[0]["status"] == "pass", det

    other = copy.deepcopy(measurements)
    other["derived"]["determinism"]["fingerprint"] = "different"
    different = bal.evaluate_profile(
        [
            {"seed": 700, "measurements": measurements},
            {"seed": 700, "measurements": other},
        ],
        thresholds,
        "gate",
    )
    assert "determinism.same_seed.700" in _failing(different)
    assert different["summary"]["verdict"] == "FAIL"
    print("DETERMINISM_OK")


# --- multi-seed decision rule ---------------------------------------------


def _gate_entry(seed, wild, civ):
    measurements = make_measurements(profile="gate", seed=seed, alive=16)
    measurements["derived"]["combat"]["wild_deaths_per_hour"] = wild
    measurements["derived"]["combat"]["civ_deaths_per_hour"] = civ
    measurements["derived"]["acquisition"]["drop_rate"] = 1.0
    # npc_ai_v2 measures in the gate now (#589): give each seed a passing
    # telemetry block or the coverage guard fails it on absence of evidence.
    measurements["derived"]["npc_ai_v2"] = {
        "telemetry_present": True,
        "since": 1000.0,
        "aggro_acquired": 5,
        "target_switched": 2,
        "target_cleared": 1,
        "leash_broken": 1,
        "flee_started": 2,
        "flee_succeeded": 1,
        "flee_delta": -1.0,
        "time_in_combat_s": 12.5,
    }
    return {"seed": seed, "measurements": measurements}


def test_median_seed_decides_profile():
    # The 2026-11-03 shape: seed 701 is quiet, the other two are not.
    report = bal.evaluate_profile(
        [
            _gate_entry(700, 1.102, 1.089),
            _gate_entry(701, 0.301, 0.388),
            _gate_entry(702, 3.534, 1.228),
        ],
        bal.load_thresholds(profile="gate"),
        "gate",
    )
    rows = {check["id"]: check for check in report["checks"]}
    assert report["summary"]["verdict"] == "PASS", report["summary"]
    assert report["summary"]["blocking_fail"] == 0
    for check_id, value in (
        ("combat.wild_deaths_per_hour", 1.102),
        ("combat.civ_deaths_per_hour", 1.089),
    ):
        median_row = rows[f"{check_id}.median"]
        assert median_row["status"] == "pass", median_row
        assert median_row["blocking"] is True
        assert median_row["value"] == value
        assert "median across seeds 700, 701, 702" in median_row["detail"]
        assert rows[f"{check_id}.worst_seed"]["status"] == "pass"
        quiet = [
            check
            for check in report["checks"]
            if check["id"] == check_id and check.get("seed") == 701
        ]
        # The quiet seed stays visible as failed evidence, never as a veto.
        assert len(quiet) == 1 and quiet[0]["status"] == "fail", quiet
        assert quiet[0]["blocking"] is False
        for run in report["runs"]:
            for check in run["checks"]:
                if check["id"] == check_id:
                    assert check["blocking"] is False
    json.dumps(report)
    print("MEDIAN_AGGREGATION_OK")


def test_worst_seed_guard_fails_profile():
    report = bal.evaluate_profile(
        [
            _gate_entry(700, 1.102, 1.089),
            _gate_entry(701, 0.2, 0.388),
            _gate_entry(702, 3.534, 1.228),
        ],
        bal.load_thresholds(profile="gate"),
        "gate",
    )
    rows = {check["id"]: check for check in report["checks"]}
    assert report["summary"]["verdict"] == "FAIL"
    assert rows["combat.wild_deaths_per_hour.median"]["status"] == "pass"
    guard = rows["combat.wild_deaths_per_hour.worst_seed"]
    assert guard["status"] == "fail", guard
    assert guard["blocking"] is True
    assert guard["value"] == 0.2
    assert guard["seed"] == 701
    assert "worst seed 701" in guard["detail"]
    assert "combat.wild_deaths_per_hour.worst_seed" in report["summary"]["failing"]
    # The civilian floor is well above the guard, so it stays green.
    assert rows["combat.civ_deaths_per_hour.worst_seed"]["status"] == "pass"
    print("WORST_SEED_GUARD_OK")


def test_death_free_seed_can_veto_gate():
    # #928: a seed with zero deaths reports no rate, but must still veto.
    report = bal.evaluate_profile(
        [
            _gate_entry(700, 1.0, 1.0),
            _gate_entry(701, None, 1.0),
            _gate_entry(702, 1.0, 1.0),
        ],
        bal.load_thresholds(profile="gate"),
        "gate",
    )
    rows = {row["id"]: row for row in report["checks"]}
    guard = rows["combat.wild_deaths_per_hour.worst_seed"]
    assert guard["status"] == "fail", guard
    assert guard["value"] == 0.0
    assert guard["seed"] == 701
    assert "worst seed 701" in guard["detail"]
    assert guard["id"] in report["summary"]["failing"]
    assert report["summary"]["verdict"] == "FAIL"
    assert rows["combat.wild_deaths_per_hour.median"]["value"] == 1.0
    print("DEATH_FREE_SEED_GUARD_OK")


# --- repeat measurement files ---------------------------------------------


def test_run_repeats_write_distinct_measurement_files():
    # #899: repeats expand the seed list, but a filename keyed only by seed
    # let the last repeat overwrite the earlier determinism evidence.
    seeds = [700, 701, 702] * 2
    collected = []

    async def fake_collect(args, profile, seed):
        collected.append(seed)
        return make_measurements(profile=profile, seed=seed)

    original = bal.collect
    original_verdict = bal._print_verdict
    bal.collect = fake_collect
    bal._print_verdict = lambda report: None
    try:
        with tempfile.TemporaryDirectory() as tmp:
            args = SimpleNamespace(
                thresholds=None,
                baseline=None,
                save_measurements=True,
                out_dir=tmp,
                out=None,
            )
            asyncio.run(bal._run(args, "gate", seeds))

            assert collected == seeds
            names = sorted(os.listdir(tmp))
            assert names == sorted(
                [
                    "balance_report.json",
                    "measurements-gate-700-r1.json",
                    "measurements-gate-700.json",
                    "measurements-gate-701-r1.json",
                    "measurements-gate-701.json",
                    "measurements-gate-702-r1.json",
                    "measurements-gate-702.json",
                ]
            ), names
            # Report entries stay keyed only by seed; the suffix is disk-only.
            with open(os.path.join(tmp, "balance_report.json"), encoding="utf-8") as fh:
                report = json.load(fh)
            assert report["seeds"] == seeds
            for seed in (700, 701, 702):
                for name in (
                    f"measurements-gate-{seed}.json",
                    f"measurements-gate-{seed}-r1.json",
                ):
                    with open(os.path.join(tmp, name), encoding="utf-8") as fh:
                        assert json.load(fh)["meta"]["seed"] == seed
    finally:
        bal.collect = original
        bal._print_verdict = original_verdict
    print("REPEAT_MEASUREMENT_FILES_OK")


# --- CLI ------------------------------------------------------------------


def _write_measurements(tmp, measurements):
    path = os.path.join(tmp, "measurements.json")
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(measurements, fh)
    return path


def test_cli_exit_codes():
    with tempfile.TemporaryDirectory() as tmp:
        passing = _write_measurements(tmp, make_measurements())
        out = os.path.join(tmp, "report.json")
        code = bal.main(["evaluate", "--measurements", passing, "--out", out])
        assert code == 0, code
        with open(out, encoding="utf-8") as fh:
            report = json.load(fh)
        assert report["schema"] == bal.REPORT_SCHEMA
        assert report["summary"]["verdict"] == "PASS"

        broken = make_measurements()
        broken["derived"]["combat"]["civ_death_share_pct"] = 99.0
        failing = _write_measurements(tmp, broken)
        code = bal.main(["evaluate", "--measurements", failing])
        assert code == 1, code

        missing = os.path.join(tmp, "missing.json")
        with open(missing, "w", encoding="utf-8") as fh:
            json.dump({"meta": {}}, fh)
        assert bal.main(["evaluate", "--measurements", missing]) == 2
        assert (
            bal.main(["evaluate", "--measurements", passing, "--profile", "nope"]) == 2
        )
        assert bal.main(["matrix"]) == 0
    print("CLI_EXIT_CODES_OK")


def test_evaluate_input_errors():
    with tempfile.TemporaryDirectory() as tmp:
        missing = os.path.join(tmp, "missing.json")
        corrupt = os.path.join(tmp, "corrupt.json")
        with open(corrupt, "w", encoding="utf-8") as fh:
            fh.write("{not json")
        passing = _write_measurements(tmp, make_measurements())

        cases = [
            ["evaluate", "--measurements", missing],
            ["evaluate", "--measurements", corrupt],
            ["evaluate", "--measurements", passing, "--baseline", missing],
            ["evaluate", "--measurements", passing, "--thresholds", missing],
            ["evaluate", "--measurements", passing, "--thresholds", corrupt],
        ]
        for argv in cases:
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                code = bal.main(argv)
            assert code == 2, (argv, code)
            diag = err.getvalue()
            assert "[balance]" in diag, (argv, diag)
            assert "Traceback" not in diag, (argv, diag)
    print("EVALUATE_INPUT_ERRORS_OK")


def test_evaluate_rejects_wrong_field_types():
    # #929: key presence alone lets a corrupt field crash the scorer.
    with tempfile.TemporaryDirectory() as tmp:
        for field, bad in (("meta", []), ("samples", {}), ("derived", "oops")):
            measurements = make_measurements()
            measurements[field] = bad
            path = _write_measurements(tmp, measurements)
            err = io.StringIO()
            with contextlib.redirect_stderr(err):
                code = bal.main(["evaluate", "--measurements", path])
            assert code == 2, (field, code)
            diag = err.getvalue()
            assert "[balance]" in diag, (field, diag)
            assert "Traceback" not in diag, (field, diag)
    print("EVALUATE_FIELD_TYPES_OK")


def test_baseline_comparison():
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, "baseline.json")
        # The shape of the committed v0.10.0 baseline: derived blocks with
        # market/players/soak, no native samples.
        baseline = {
            "meta": {
                "name": "v0.10.0 economy baseline",
                "master_sha": "08f7453",
                "seed": 700,
                "duration_s": 3600.0,
                "agents": 20,
            },
            "derived": {
                "market": {
                    "treasury": 700.0,
                    "sunk_lifetime": 35.0,
                    "trade_count": 33,
                    "tax_effective_pct": 22.22,
                },
                "players": {"deaths": 917},
                "soak": {"alive": 20},
            },
        }
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(baseline, fh)
        measurements = make_measurements()
        report = bal.evaluate_profile(
            [{"seed": 700, "measurements": measurements}],
            bal.load_thresholds(profile="smoke"),
            "smoke",
            baseline=bal._load_baseline(path),
        )
    block = report["baseline"]
    assert block["meta"]["master_sha"] == "08f7453"
    rows = {row["metric"]: row for row in block["deltas"]}
    assert rows["treasury.end"]["baseline"] == 700.0
    assert rows["treasury.end"]["current"] == 505.0
    assert rows["market.trade_count"]["baseline"] == 33
    assert rows["combat.deaths"]["baseline"] == 917
    assert rows["liveness.final_alive"]["current"] == 8
    print("BASELINE_COMPARE_OK")


# --- matrix ---------------------------------------------------------------


def test_matrix_catalog():
    gate = bal.PROFILES["gate"]
    assert gate["duration_s"] == 14400.0
    assert gate["agents"] == 20
    assert gate["seeds"] == (700, 701, 702)
    smoke = bal.PROFILES["smoke"]
    assert smoke["duration_s"] == 600.0
    assert smoke["agents"] == 10
    # Declared scenarios are never dropped; landed ones leave the unlanded
    # placeholder catalog and move into gate coverage.
    assert "npc_ai_v2" in bal.SCENARIOS
    assert "npc_ai_v2" in bal.PROFILES["gate"]["coverage"]
    assert "unlanded" not in bal.SCENARIOS["npc_ai_v2"]
    assert "unlanded" not in bal.SCENARIOS["acquisition"]
    assert "acquisition" in bal.PROFILES["gate"]["coverage"]
    assert bal.SCENARIOS["progression"]["observed_only"]
    thresholds = bal.load_thresholds(profile="gate")
    assert thresholds["liveness"]["min_agents"] == 16
    assert thresholds["combat"]["civ_deaths_per_hour_min"] == 0.5
    assert thresholds["combat"]["civ_death_share_pct_max"] == 95.0
    assert thresholds["combat"]["wild_deaths_per_hour_min"] == 0.5
    assert bal.WORST_SEED_DEATHS_PER_HOUR_MIN == 0.25
    assert set(bal.WORST_SEED_GUARD_IDS) == {
        "combat.wild_deaths_per_hour",
        "combat.civ_deaths_per_hour",
    }
    assert thresholds["combat"]["deaths_per_kill_max"] == 1.5
    assert thresholds["market"]["max_median_spread_pct"] == 25.0
    print("MATRIX_CATALOG_OK")


def test_committed_baseline_is_readable():
    path = os.path.join(ROOT, "balance", "baseline-08f7453.json")
    assert os.path.isfile(path), "the reference baseline must stay committed"
    with open(path, encoding="utf-8") as fh:
        baseline = json.load(fh)
    assert baseline["meta"]["master_sha"] == "08f7453"
    assert baseline["meta"]["seed"] == 700
    assert baseline["derived"]["market"]["reserve"] == 500.0
    print("COMMITTED_BASELINE_OK")


def test_import_has_no_heavy_deps():
    assert "torch" not in sys.modules
    assert "websockets" not in sys.modules
    print("NO_HEAVY_DEPS_OK")


def main():
    test_conservation_identity()
    test_derive_metrics()
    test_derive_without_players_does_not_crash()
    test_zone_deaths_accumulate_deltas()
    test_missing_zone_counters_skip_not_fail()
    test_derive_without_ledger_skips_not_passes()
    test_wash_trade_detected()
    test_zero_craft_and_unexercised_sink()
    test_evaluate_pass_and_schema()
    test_evaluate_fail_paths()
    test_empty_run_cannot_pass_gate()
    test_gate_coverage_guard()
    test_acquisition_telemetry_survives_trim_and_derive()
    test_death_log_survives_trim_and_attributes_deaths()
    test_death_log_is_bounded_deduplicated_and_restart_safe()
    test_death_attribution_tie_order_is_stable()
    test_npc_ai_telemetry_survives_trim_and_derive()
    test_guilds_telemetry_survives_trim_and_derive()
    test_guild_telemetry_ignores_a_bot_founded_guild()
    test_guild_absence_and_zero_world_fail_gate()
    test_profile_overrides_and_threshold_file()
    test_stability_thresholds_are_resolvable_and_flip_verdicts()
    test_determinism_repeats()
    test_median_seed_decides_profile()
    test_worst_seed_guard_fails_profile()
    test_death_free_seed_can_veto_gate()
    test_run_repeats_write_distinct_measurement_files()
    test_cli_exit_codes()
    test_evaluate_input_errors()
    test_evaluate_rejects_wrong_field_types()
    test_baseline_comparison()
    test_matrix_catalog()
    test_committed_baseline_is_readable()
    test_import_has_no_heavy_deps()
    print("BALANCE_HARNESS_OK")


if __name__ == "__main__":
    main()
