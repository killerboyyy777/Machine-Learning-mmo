"""Balance gate harness (#537): scenario matrix + explicit PASS/FAIL thresholds.

The nightly soak (``ml/conductor/soak.py``) answers "are agents still
alive?". This harness answers "is the game balanced enough to release?":
it runs a mixed-agent profile against a live server, samples the dashboard
(``/api/state``) and the loopback GM stream (``gm_tables``), derives the
balance metrics, scores them against provisional thresholds, and writes
``balance/balance_report.json``. A failing check exits nonzero, so a CI
job or the release checklist can gate on it. The nightly soak is untouched.

Usage::

    # Offline: the matrix, what each scenario needs, what is skipped and why
    python ml/balance.py matrix

    # PR smoke (10 min, 10 agents; server must already be up, see README)
    python ml/balance.py run --profile smoke

    # Release gate (4h x 20 agents x seeds 700/701/702)
    python ml/balance.py run --profile gate

    # Re-score stored measurements, comparing against the v0.10.0 baseline
    python ml/balance.py evaluate --measurements balance/measurements-gate-700.json \
        --baseline balance/baseline-08f7453.json

    # Capture a baseline (default: 08f7453, seed 700, 1h, 20 agents)
    python ml/balance.py baseline --seed 700 --commit 8f7453 \
        --out balance/baseline-08f7453.json

Override the thresholds without editing code with ``--thresholds FILE``, a
JSON object deep-merged over :data:`DEFAULT_THRESHOLDS`; profile overrides
in :data:`PROFILES` only relax checks a shorter profile cannot exercise.

A multi-seed profile is decided on the median seed: each thresholded metric
is scored once against the median of its per-seed values, and the per-seed
rows stay in the report as non-blocking evidence. The two combat death-rate
floors also keep a hard worst-seed guard at
:data:`WORST_SEED_DEATHS_PER_HOUR_MIN` so a quiet seed cannot be averaged
away.
"""

import argparse
import asyncio
import hashlib
import json
import math
import os
import statistics
import sys
import time
import urllib.error
import urllib.request
from itertools import pairwise

# ``python ml/balance.py`` puts ml/ on sys.path, not the repo root; the
# conductor/plugins imports below expect the root (same as soak.py).
_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

REPORT_SCHEMA = "balance_report/v1"
BASELINE_SCHEMA = "balance_baseline/v1"

# The reference capture from issue #537 section 1. Later runs compare
# against the stored file, never against a freshly run scenario.
DEFAULT_BASELINE = "balance/baseline-08f7453.json"
DEFAULT_REFERENCE_COMMIT = "08f7453"

# Startup ramp: waves spawn over the first minute, so the liveness floor is
# read after this window (the final sample still counts on its own).
LIVENESS_WARMUP_S = 60.0

# Provisional thresholds: defaults from issue #537 section 4. Keep them
# here (not scattered through the checks) so the owner can tune one block.
DEFAULT_THRESHOLDS = {
    "conservation": {
        "max_items_drift": 0.0,
        "max_gold_drift": 0.0,
    },
    "treasury": {
        "band_pct_of_reserve": 10.0,
        "steady_after_s": 1800.0,
    },
    "combat": {
        # Civilian rooms are most of the map and the normal hunting ground, so
        # their share of deaths is high by construction; the share check only
        # guards against wild zones going inert. What #538 actually promises is
        # that civ zones still kill at a nonzero rate, just less often and with
        # no item scatter -- the rate floor below is that promise.
        "civ_deaths_per_hour_min": 0.5,
        "civ_death_share_pct_max": 95.0,
        "wild_deaths_per_hour_min": 0.5,
        "deaths_per_kill_max": 1.5,
        "companion_damage_share_pct_min": 20.0,
        "companion_damage_share_pct_max": 40.0,
    },
    "craft": {
        "max_input_loss": 0.0,
        "max_refund_excess": 0.0,
        "max_negative_inventory": 0,
        "min_completions": 1,
    },
    "market": {
        "max_median_spread_pct": 25.0,
        "min_trade_volume": 1,
        "max_wash_trades": 0,
    },
    "acquisition": {
        # Provisional measurement floor, not a balance floor: a measured zero
        # still proves the boss kill path reached the counter.
        "min_drop_rate": 0.0,
    },
    "guilds": {
        # Probe floors, not balance floors: each seed founds a charter and
        # moves gold through the vault, so a zero here means the measurement
        # broke, not that the world was quiet.
        "min_guilds": 1,
        "min_members": 2,
        "min_vault_gold_in": 1,
        "min_vault_gold_out": 1,
        "min_vault_items": 1,
        "min_audit_entries": 6,
        "max_conservation_drift": 0.0,
    },
    "liveness": {
        "min_agents": 10,
    },
    "determinism": {
        "enabled": True,
    },
    "npc_ai_v2": {
        "min_aggro_acquired": 1,
        "max_flee_delta": 0.0,
    },
}

# The median seed decides a multi-seed profile, but the combat death-rate
# floors need a floor no single seed may fall below: averaging away an inert
# world would make the gate green on a run that never fought.
WORST_SEED_DEATHS_PER_HOUR_MIN = 0.25
WORST_SEED_GUARD_IDS = (
    "combat.wild_deaths_per_hour",
    "combat.civ_deaths_per_hour",
)

# Scenario catalog. ``unlanded`` is a hard skip: the measurement does not
# exist yet, so the scenario is reported and never silently counted as
# passing. ``observed_only`` scenarios collect what the server exposes and
# carry no thresholds (they feed the release-notes table instead).

# The guild probe can only skip against a server built before its surface; the
# gate guard turns that skip into a failure while this build ships the verb.
GUILD_SURFACE_ISSUE = "#588"

SCENARIOS = {
    "economy": {
        "title": "Sources vs sinks, treasury convergence, market sink",
        "issue": "#537",
    },
    "combat": {
        "title": "Deaths/hour, attacks-to-kill, potion use, companion share",
        "issue": "#537",
    },
    "craft": {
        "title": "Throughput, cancel/refund, escrow conservation",
        "issue": "#501",
    },
    "market": {
        "title": "Spread, volume, price drift, wash trades",
        "issue": "#537",
    },
    "liveness": {
        "title": "Alive agents through the run (after the spawn ramp)",
        "issue": "#537",
    },
    "determinism": {
        "title": "Same seed -> identical fingerprints",
        "issue": "#537",
    },
    "acquisition": {
        "title": "Drop rate, craft cost, shop price vs time-to-earn",
        "issue": "#532",
    },
    "guilds": {
        "title": "Charter, membership, vault movement, audit log",
        "issue": "#588",
    },
    "progression": {
        "title": "XP/hour, level curve, quest completion rate",
        "issue": "#537",
        "observed_only": "server exposes score, not xp; quest completions "
        "and the level curve are recorded, xp/hour is not measurable",
    },
    "party": {
        "title": "Solo vs party, aggro tanking, downed/revive spend",
        "issue": "#537",
        "observed_only": "dungeon solo/group clears are recorded; aggro "
        "attribution and revive spend are not exposed",
    },
    "npc_ai_v2": {
        "title": "Aggro/leash/flee rates, deaths per room",
        "issue": "#536",
    },
}

PROFILES = {
    "smoke": {
        "description": "PR smoke: economy + craft + combat on a fresh world",
        "duration_s": 600.0,
        "agents": 10,
        "min_agents": 8,
        "seeds": (700,),
        "slots": (
            "linear",
            "hunter",
            "gather",
            "dungeon",
            "market",
            "crafter",
        ),
        "scenarios": ("economy", "combat", "craft", "market", "liveness"),
        # 10 minutes is shorter than the treasury convergence window, so
        # economy records observations without a coverage requirement here.
        "coverage": ("combat", "craft", "market", "liveness"),
        "threshold_overrides": {
            # 10 minutes cannot exercise a 30-minute convergence window or
            # guarantee recipe inputs are found; the counts are reported,
            # the gate reads them over 4 hours.
            "craft": {"min_completions": 0},
        },
    },
    "gate": {
        "description": "Release gate: full matrix, 4h, seeds 700/701/702; "
        "median seed decides with a 0.25/h worst-seed guard",
        "duration_s": 14400.0,
        "agents": 20,
        "min_agents": 16,
        "seeds": (700, 701, 702),
        "slots": (
            "linear",
            "hunter",
            "gather",
            "dungeon",
            "market",
            "commissioner",
            "crafter",
            "quester",
            "party_leader",
        ),
        "scenarios": (
            "economy",
            "combat",
            "craft",
            "acquisition",
            "progression",
            "party",
            "market",
            "determinism",
            "liveness",
            "guilds",
            "npc_ai_v2",
        ),
        "coverage": (
            "economy",
            "combat",
            "craft",
            "acquisition",
            "guilds",
            "market",
            "liveness",
            "npc_ai_v2",
        ),
        "threshold_overrides": {},
    },
    "stability": {
        "description": "Stability: 24h, 20 agents, seeds 700/701/702",
        "duration_s": 86400.0,
        "agents": 20,
        "min_agents": 16,
        "seeds": (700, 701, 702),
        "slots": (
            "linear",
            "hunter",
            "gather",
            "dungeon",
            "market",
            "commissioner",
            "crafter",
            "quester",
            "party_leader",
        ),
        "scenarios": (
            "economy",
            "combat",
            "craft",
            "market",
            "liveness",
            "determinism",
            "acquisition",
            "progression",
            "party",
            "npc_ai_v2",
        ),
        "coverage": ("economy", "combat", "craft", "market", "liveness", "progression"),
        "threshold_overrides": {
            "economic_slope_max": 1e-4,
            "level_slope_max": 1e-4,
            "score_slope_max": 1e-4,
            "min_population_ratio": 0.8,
        },
    },
    "baseline": {
        "description": "Reference capture: 1h, 20 agents, seed 700 (issue #537 section 1)",
        "duration_s": 3600.0,
        "agents": 20,
        "min_agents": 16,
        "seeds": (700,),
        "slots": (
            "linear",
            "hunter",
            "gather",
            "dungeon",
            "market",
            "commissioner",
        ),
        "scenarios": ("economy", "combat", "craft", "market", "liveness"),
        "coverage": ("economy", "combat", "craft", "market", "liveness"),
        "threshold_overrides": {},
    },
}

# (id, scenario, metric, op, threshold path, blocking)
# op: "==", "<=", ">=", "in" (inclusive band, threshold path is a pair),
# "is" (boolean identity). Every threshold path resolves inside the merged
# thresholds dict; nothing numeric lives in this table.
CHECKS = (
    (
        "conservation.items_drift",
        "economy",
        "conservation.items_drift",
        "<=",
        ("conservation", "max_items_drift"),
        True,
    ),
    (
        "conservation.gold_drift",
        "economy",
        "conservation.gold_drift",
        "<=",
        ("conservation", "max_gold_drift"),
        True,
    ),
    (
        "treasury.band",
        "economy",
        "treasury.band_pct",
        "<=",
        ("treasury", "band_pct_of_reserve"),
        True,
    ),
    ("treasury.sink_grew", "economy", "treasury.sink_grew", "is", True, True),
    (
        "treasury.not_monotonic",
        "economy",
        "treasury.monotonic_climb",
        "is",
        False,
        True,
    ),
    (
        "combat.civ_deaths_per_hour",
        "combat",
        "combat.civ_deaths_per_hour",
        ">=",
        ("combat", "civ_deaths_per_hour_min"),
        True,
    ),
    (
        "combat.civ_death_share",
        "combat",
        "combat.civ_death_share_pct",
        "<=",
        ("combat", "civ_death_share_pct_max"),
        True,
    ),
    (
        "combat.wild_deaths_per_hour",
        "combat",
        "combat.wild_deaths_per_hour",
        ">=",
        ("combat", "wild_deaths_per_hour_min"),
        True,
    ),
    (
        "combat.deaths_per_kill",
        "combat",
        "combat.deaths_per_kill",
        "<=",
        ("combat", "deaths_per_kill_max"),
        True,
    ),
    (
        "combat.deaths_per_hour",
        "combat",
        "combat.deaths_per_hour",
        ">=",
        ("combat", "wild_deaths_per_hour_min"),
        False,
    ),
    (
        "combat.companion_damage_share",
        "combat",
        "combat.companion_damage_share_pct",
        "in",
        (
            "combat",
            "companion_damage_share_pct_min",
            "combat",
            "companion_damage_share_pct_max",
        ),
        True,
    ),
    (
        "craft.input_loss",
        "craft",
        "craft.input_loss",
        "<=",
        ("craft", "max_input_loss"),
        True,
    ),
    (
        "craft.refund_excess",
        "craft",
        "craft.refund_excess",
        "<=",
        ("craft", "max_refund_excess"),
        True,
    ),
    (
        "craft.negative_inventory",
        "craft",
        "craft.negative_inventory",
        "<=",
        ("craft", "max_negative_inventory"),
        True,
    ),
    (
        "craft.completions",
        "craft",
        "craft.completions",
        ">=",
        ("craft", "min_completions"),
        True,
    ),
    (
        "market.median_spread",
        "market",
        "market.median_spread_pct",
        "<=",
        ("market", "max_median_spread_pct"),
        True,
    ),
    (
        "market.trade_volume",
        "market",
        "market.trade_volume",
        ">=",
        ("market", "min_trade_volume"),
        True,
    ),
    (
        "market.wash_trades",
        "market",
        "market.wash_trades",
        "<=",
        ("market", "max_wash_trades"),
        True,
    ),
    (
        "acquisition.drop_rate",
        "acquisition",
        "acquisition.drop_rate",
        ">=",
        ("acquisition", "min_drop_rate"),
        True,
    ),
    ("guilds.count", "guilds", "guilds.count", ">=", ("guilds", "min_guilds"), True),
    (
        "guilds.members",
        "guilds",
        "guilds.members",
        ">=",
        ("guilds", "min_members"),
        True,
    ),
    (
        "guilds.vault_gold_in",
        "guilds",
        "guilds.vault_gold_in",
        ">=",
        ("guilds", "min_vault_gold_in"),
        True,
    ),
    (
        "guilds.vault_gold_out",
        "guilds",
        "guilds.vault_gold_out",
        ">=",
        ("guilds", "min_vault_gold_out"),
        True,
    ),
    (
        "guilds.vault_items",
        "guilds",
        "guilds.vault_items",
        ">=",
        ("guilds", "min_vault_items"),
        True,
    ),
    (
        "guilds.audit_entries",
        "guilds",
        "guilds.audit_entries",
        ">=",
        ("guilds", "min_audit_entries"),
        True,
    ),
    (
        "guilds.conservation",
        "guilds",
        "guilds.conservation_drift",
        "<=",
        ("guilds", "max_conservation_drift"),
        True,
    ),
    ("guilds.gold_conserved", "guilds", "guilds.gold_conserved", "is", True, True),
    (
        "liveness.min_agents",
        "liveness",
        "liveness.min_alive",
        ">=",
        ("liveness", "min_agents"),
        True,
    ),
    (
        "npc_ai_v2.telemetry_present",
        "npc_ai_v2",
        "npc_ai_v2.telemetry_present",
        "is",
        True,
        True,
    ),
    (
        "npc_ai_v2.aggro_acquired",
        "npc_ai_v2",
        "npc_ai_v2.aggro_acquired",
        ">=",
        ("npc_ai_v2", "min_aggro_acquired"),
        True,
    ),
    (
        "npc_ai_v2.flee_consistency",
        "npc_ai_v2",
        "npc_ai_v2.flee_delta",
        "<=",
        ("npc_ai_v2", "max_flee_delta"),
        True,
    ),
)

# Metrics that are recorded but have no threshold in issue #537 section 4.
OBSERVATIONS = (
    ("economy", "economy.tax_effective_pct"),
    ("economy", "economy.sunk_lifetime"),
    ("combat", "combat.deaths"),
    ("combat", "combat.attacks_to_kill"),
    ("combat", "combat.potions_per_hour"),
    ("craft", "craft.completions"),
    ("progression", "progression.quest_completions"),
    ("progression", "progression.level_counts"),
    ("party", "party.solo_clears"),
    ("party", "party.group_clears"),
    ("party", "party.revive_gold_per_hour"),
    ("acquisition", "acquisition.drop_rate"),
    ("acquisition", "acquisition.craft_cost_ratio"),
    ("acquisition", "acquisition.shop_time_to_earn_s"),
    ("determinism", "determinism.fingerprint"),
)

REQUIRED_MEASUREMENT_FIELDS = ("meta", "samples", "derived")


# --- thresholds / profiles -------------------------------------------------


def _deep_merge(base, override):
    """Recursive dict merge; later values win, unknown keys are kept."""
    out = dict(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def load_thresholds(path=None, profile=None):
    """Defaults <- profile overrides <- JSON file (last wins)."""
    thresholds = _deep_merge(DEFAULT_THRESHOLDS, {})
    spec = PROFILES.get(profile or "", {})
    thresholds = _deep_merge(thresholds, spec.get("threshold_overrides", {}))
    if profile in PROFILES:
        thresholds = _deep_merge(
            thresholds, {"liveness": {"min_agents": PROFILES[profile]["min_agents"]}}
        )
    if path:
        with open(path, encoding="utf-8") as fh:
            thresholds = _deep_merge(thresholds, json.load(fh))
    return thresholds


def resolve_threshold(thresholds, path):
    """Fail loudly on a typo: a missing threshold must never pass a gate."""
    node = thresholds
    for key in path:
        if not isinstance(node, dict) or key not in node:
            raise KeyError(f"threshold not configured: {'.'.join(path)}")
        node = node[key]
    return node


# --- derived measurements --------------------------------------------------


def _slope(series):
    if len(series) < 2:
        return 0.0
    t_start, y_start = series[0]
    t_end, y_end = series[-1]
    if t_end == t_start:
        return 0.0
    return (y_end - y_start) / (t_end - t_start)


def _num(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return float(value)


def _state(sample):
    return sample.get("state") or {}


def _market(sample):
    return _state(sample).get("market") or {}


def _first_number(values):
    for value in values:
        number = _num(value)
        if number is not None:
            return number
    return None


def _median(values):
    clean = [v for v in values if v is not None]
    return round(statistics.median(clean), 3) if clean else None


def conservation_drift(created, consumed, destroyed, held):
    """The issue #537 identity: created - consumed - destroyed == held.

    ``held`` is inventory + ground + escrow (and, for gold, banked and
    treasury). Exact zero is the only correct answer; anything else is a
    duping or deletion bug, not a rounding error.
    """
    return round(float(created) - float(consumed) - float(destroyed) - float(held), 6)


def _ledger_drift(ledger, kind):
    precomputed = _num(ledger.get(f"{kind}_drift"))
    if precomputed is not None:
        return precomputed
    block = ledger.get(kind) or {}
    values = [
        _num(block.get(key)) for key in ("created", "consumed", "destroyed", "held")
    ]
    if any(value is None for value in values):
        return None
    return conservation_drift(*values)


def _refund_excess(ledger):
    precomputed = _num(ledger.get("craft_refund_excess"))
    if precomputed is not None:
        return precomputed
    refunded = _num(ledger.get("craft_refunded_total"))
    escrowed = _num(ledger.get("craft_escrowed_total"))
    if refunded is None or escrowed is None:
        return None
    return round(max(0.0, refunded - escrowed), 6)


def _guild_telemetry(samples):
    """Peak guild snapshot plus the probe's own ledger.

    Not the last sample: the probe disbands its charter, so the final read
    always shows an empty guild world. The measurement lives in the sample
    taken while the vault still held gold and an item. ``guild_probe`` is
    client-side evidence -- the server publishes vault totals, never the
    per-op purse deltas the conservation identity needs.
    """
    best, best_key, probe = {}, None, {}
    for sample in samples:
        state = sample.get("state") or {}
        guilds = state.get("guilds")
        if not isinstance(guilds, dict) or not guilds:
            guilds = (sample.get("tables") or {}).get("guilds")
        if isinstance(guilds, dict) and guilds:
            key = (
                _num(guilds.get("count")) or 0.0,
                _num(guilds.get("members_total")) or 0.0,
                _num(guilds.get("audit_entries_written")) or 0.0,
            )
            if best_key is None or key >= best_key:
                best, best_key = guilds, key
        candidate = state.get("guild_probe")
        if isinstance(candidate, dict) and candidate:
            probe = candidate
    return {"guilds": best, "guild_probe": probe}


def _vault_drift(stats):
    """vault_gold_in_total - vault_gold_out_total - vault_gold_total."""
    gold_in = _num(stats.get("vault_gold_in_total"))
    gold_out = _num(stats.get("vault_gold_out_total"))
    held = _num(stats.get("vault_gold_total"))
    if gold_in is None or gold_out is None or held is None:
        return None
    return round(gold_in - gold_out - held, 3)


def _guild_skip_reason(probe):
    if probe.get("skipped"):
        return f"{GUILD_SURFACE_ISSUE} {probe['skipped']}"
    if probe.get("error"):
        return f"guild probe failed: {probe['error']}"
    return ""


def _telemetry(samples):
    """Telemetry block from the last sample's state (GM tables as fallback).

    Same source of truth as the companion damage share folded in below: the
    counters are lifetime server globals, so the last sample is the final read.
    """
    if not samples:
        return {}
    sample = samples[-1]
    state = sample.get("state") or {}
    tables = sample.get("tables") or {}
    acquisition = state.get("acquisition")
    if not isinstance(acquisition, dict):
        acquisition = tables.get("acquisition")
    if not isinstance(acquisition, dict):
        acquisition = {}
    npc_ai = state.get("npc_ai")
    if not isinstance(npc_ai, dict):
        npc_ai = tables.get("npc_ai")
    if not isinstance(npc_ai, dict):
        npc_ai = {}
    sources = acquisition.get("sources") or {}
    boss = sources.get("boss") or {}
    attempts = _num(boss.get("attempts"))
    earned = _num(boss.get("earned"))
    drop_rate = None
    if attempts and earned is not None:
        drop_rate = round(earned / attempts, 4)
    shop = sources.get("shop") or {}
    guilds = _guild_telemetry(samples)
    return {
        "drop_rate": drop_rate,
        "craft_cost_ratio": _num(acquisition.get("craft_cost_ratio")),
        "shop_time_to_earn_s": _num(shop.get("first_earn_s")),
        "acquisition": acquisition,
        "guilds": guilds["guilds"],
        "guild_probe": guilds["guild_probe"],
        "npc_ai": npc_ai,
    }


def derive(samples, ledger=None, telemetry=None):
    """Fold raw samples into the metric contract the checks consume.

    ``samples`` are ``{"t", "state", "tables", "alive", "episodes"}`` rows
    produced by :func:`collect` (or a stored measurement file). ``ledger``
    and ``telemetry`` are optional blocks from a future server probe; when
    absent, the checks that need them are skipped, never passed.
    """
    samples = sorted(
        (s for s in samples if _num(s.get("t")) is not None), key=lambda s: s["t"]
    )
    ledger = ledger or {}
    telemetry = telemetry or {}
    acquisition_stats = telemetry.get("acquisition") or {}
    guild_stats = telemetry.get("guilds") or {}
    guild_probe = telemetry.get("guild_probe") or {}
    # The dashboard exposes cumulative player/companion damage in a
    # "combat" block. Prefer that to a separately probed telemetry file so
    # the share is measured even when the harness probes only snapshots.
    if telemetry.get("companion_damage_share_pct") is None and samples:
        combat_stats = (samples[-1].get("state") or {}).get("combat") or {}
        player_dmg = _num(combat_stats.get("player_damage")) or 0.0
        companion_dmg = _num(combat_stats.get("companion_damage")) or 0.0
        total_dmg = player_dmg + companion_dmg
        if total_dmg > 0:
            telemetry = dict(
                telemetry,
                companion_damage_share_pct=round(companion_dmg / total_dmg * 100.0, 3),
            )

    treasury_series = []
    reserve = None
    sunk = []
    tax_pct = None
    trade_counts = []
    death_tracking = {}
    deaths = 0.0
    last_players = []
    wild_deaths = 0.0
    civ_deaths = 0.0
    kills = 0.0
    agent_seconds = 0.0
    alive_samples = []
    episode_samples = []
    final_tables = {}
    craft_seq = []
    first_craft_seen = None
    craft_feed_present = False
    quest_totals = []
    solo_clears = []
    group_clears = []
    spreads = []
    # Stability trackers
    stability_avg_levels = []
    stability_total_scores = []
    stability_economy = []
    stability_agents = []

    previous_t = None
    for sample in samples:
        t = _num(sample.get("t")) or 0.0
        state = _state(sample)
        tables = sample.get("tables") or {}
        market = _market(sample)
        if tables and "error" not in tables:
            final_tables = tables

        treasury = _first_number((tables.get("treasury"), market.get("treasury")))
        if treasury is not None:
            treasury_series.append({"t": round(t, 1), "value": treasury})
        if reserve is None:
            reserve = _num(market.get("reserve"))
        value = _num(market.get("sunk_lifetime"))
        if value is not None:
            sunk.append(value)
        if tax_pct is None:
            tax_pct = _num(market.get("tax_effective_pct"))
        trade_count = _num(market.get("trade_count"))
        if trade_count is not None:
            trade_counts.append(trade_count)

        players = state.get("players") or []
        if players:
            last_players = players
        online = len(players)
        stability_agents.append((t, online))
        alive = _num(sample.get("alive"))
        if alive is not None:
            alive_samples.append((t, alive))
        episodes = _num(sample.get("episodes"))
        if episodes is not None:
            episode_samples.append(episodes)

        if previous_t is not None:
            agent_seconds += max(0.0, t - previous_t) * online
        for player in players:
            name = player.get("name")
            current = _num(player.get("deaths"))
            if name is None or current is None:
                continue
            wild = _num(player.get("wild_deaths")) or 0.0
            civ = _num(player.get("civ_deaths")) or 0.0
            player_kills = _num(player.get("kills")) or 0.0
            previous = death_tracking.get(name)
            if previous is not None:
                deaths += max(0.0, current - previous["deaths"])
                wild_deaths += max(0.0, wild - previous["wild_deaths"])
                civ_deaths += max(0.0, civ - previous["civ_deaths"])
                kills += max(0.0, player_kills - previous["kills"])
            death_tracking[name] = {
                "deaths": current,
                "wild_deaths": wild,
                "civ_deaths": civ,
                "kills": player_kills,
            }
        previous_t = t

        # Stability tracking
        online_players = [p for p in players if p.get("level") is not None]
        if online_players:
            avg_level = sum(_num(p.get("level")) or 0 for p in online_players) / len(
                online_players
            )
            stability_avg_levels.append((t, avg_level))

        total_score = _num(state.get("total_score"))
        if total_score is not None:
            stability_total_scores.append((t, total_score))

        total_gold = sum(_num(p.get("gold")) or 0 for p in players)
        stability_economy.append((t, total_gold))

        for entry in state.get("craft_history") or []:
            seq = _num(entry.get("seq"))
            if seq is not None:
                craft_seq.append(seq)
        craft_feed_present = craft_feed_present or "craft_history" in state
        if first_craft_seen is None:
            first_craft_seen = bool(state.get("craft_history"))
        quests = state.get("quests") or {}
        completions = quests.get("completions") or {}
        if isinstance(completions, dict):
            quest_totals.append(sum(_num(v) or 0 for v in completions.values()))
        clears = state.get("dungeon_clears") or {}
        solo = _num(clears.get("solo_clears"))
        group = _num(clears.get("group_clears"))
        if solo is not None:
            solo_clears.append(solo)
        if group is not None:
            group_clears.append(group)

        asks, bids = {}, {}
        for order in market.get("orders") or []:
            price, item = _num(order.get("price")), order.get("item")
            if price is not None and item:
                asks.setdefault(item, []).append(price)
        for bid in market.get("bids") or []:
            price, item = _num(bid.get("price")), bid.get("item")
            if price is not None and item:
                bids.setdefault(item, []).append(price)
        for item in set(asks) & set(bids):
            best_ask, best_bid = min(asks[item]), max(bids[item])
            mid = (best_ask + best_bid) / 2.0
            if mid > 0:
                spreads.append((best_ask - best_bid) / mid * 100.0)
        wash_trades = 0
        if market.get("history"):
            self_deals = sum(
                1
                for trade in market["history"]
                if trade.get("buyer") and trade.get("buyer") == trade.get("seller")
            )
            wash_trades = max(wash_trades, self_deals)

    def delta(series):
        return round(series[-1] - series[0], 3) if len(series) >= 2 else None

    trade_volume = delta(trade_counts)
    if trade_volume is None and len(samples) >= 2:
        histories = [
            len(_market(s).get("history") or []) for s in (samples[0], samples[-1])
        ]
        trade_volume = max(0, histories[-1] - histories[0]) if histories else None

    deaths_per_hour = None
    wild_deaths_per_hour = None
    civ_deaths_per_hour = None
    civ_death_share_pct = None
    deaths_per_kill = None
    if agent_seconds > 0:
        agent_hours = agent_seconds / 3600.0
        deaths_per_hour = round(deaths / agent_hours, 3)
        if wild_deaths + civ_deaths > 0:
            wild_deaths_per_hour = round(wild_deaths / agent_hours, 3)
            civ_deaths_per_hour = round(civ_deaths / agent_hours, 3)
            if deaths > 0:
                civ_death_share_pct = round(civ_deaths / deaths * 100.0, 3)
    if kills > 0:
        deaths_per_kill = round(deaths / kills, 3)

    craft_completions = None
    if craft_seq:
        craft_completions = int(max(craft_seq) - min(craft_seq))
        if not first_craft_seen:
            # The earliest sample rolled in after the first completion, so
            # the window's first seq is one past the run's starting seq.
            craft_completions += 1
    elif craft_feed_present:
        # The feed exists and stayed empty: measurable zero, not missing.
        craft_completions = 0

    level_counts = {}
    for player in last_players:
        level = player.get("level")
        if level is not None:
            level_counts[str(level)] = level_counts.get(str(level), 0) + 1

    telemetry_deaths = _num(telemetry.get("deaths_per_hour"))
    if telemetry_deaths is not None:
        deaths_per_hour = telemetry_deaths

    population_ratio = 1.0
    if stability_agents:
        agent_counts = [a for _, a in stability_agents if a > 0]
        if agent_counts:
            population_ratio = min(agent_counts) / max(agent_counts)

    npc_ai_stats = telemetry.get("npc_ai") or {}
    npc_ai_since = _num(npc_ai_stats.get("since"))
    npc_ai_flee_started = _num(npc_ai_stats.get("flee_started"))
    npc_ai_flee_succeeded = _num(npc_ai_stats.get("flee_succeeded"))
    npc_ai_flee_delta = None
    if npc_ai_flee_started is not None and npc_ai_flee_succeeded is not None:
        npc_ai_flee_delta = round(npc_ai_flee_succeeded - npc_ai_flee_started, 3)

    return {
        "treasury": {
            "reserve": reserve,
            "series": treasury_series,
            "sunk_start": sunk[0] if sunk else None,
            "sunk_end": sunk[-1] if sunk else None,
            "tax_effective_pct": tax_pct,
        },
        "economy": {
            "tax_effective_pct": tax_pct,
            "sunk_lifetime": sunk[-1] if sunk else None,
        },
        "combat": {
            "deaths": round(deaths, 3),
            "kills": round(kills, 3),
            "agent_hours": round(agent_seconds / 3600.0, 3),
            "deaths_per_hour": deaths_per_hour,
            "deaths_per_kill": deaths_per_kill,
            "wild_deaths_per_hour": wild_deaths_per_hour,
            "civ_deaths_per_hour": civ_deaths_per_hour,
            "civ_death_share_pct": civ_death_share_pct,
            "companion_damage_share_pct": _num(
                telemetry.get("companion_damage_share_pct")
            ),
            "attacks_to_kill": _num(telemetry.get("attacks_to_kill")),
            "potions_per_hour": _num(telemetry.get("potions_per_hour")),
        },
        "craft": {
            "completions": craft_completions,
            "input_loss": _num(ledger.get("craft_input_loss")),
            "refund_excess": _refund_excess(ledger),
            "negative_inventory": _num(ledger.get("negative_inventory")),
            "refunded_total": _num(ledger.get("craft_refunded_total")),
            "escrowed_total": _num(ledger.get("craft_escrowed_total")),
        },
        "market": {
            "median_spread_pct": _median(spreads),
            "spread_samples": [round(s, 3) for s in spreads],
            "trade_volume": trade_volume,
            "wash_trades": wash_trades,
        },
        "conservation": {
            "items_drift": _ledger_drift(ledger, "items"),
            "gold_drift": _ledger_drift(ledger, "gold"),
        },
        "progression": {
            "quest_completions": delta(quest_totals),
            "level_counts": level_counts,
        },
        "party": {
            "solo_clears": delta(solo_clears),
            "group_clears": delta(group_clears),
            "revive_gold_per_hour": _num(telemetry.get("revive_gold_per_hour")),
        },
        "acquisition": {
            "drop_rate": _num(telemetry.get("drop_rate")),
            "craft_cost_ratio": _num(telemetry.get("craft_cost_ratio")),
            "shop_time_to_earn_s": _num(telemetry.get("shop_time_to_earn_s")),
            "sources": acquisition_stats.get("sources") or {},
            "adoptions": acquisition_stats.get("adoptions") or {},
            "not_in_build": acquisition_stats.get("not_in_build") or {},
        },
        "npc_ai_v2": {
            "telemetry_present": bool(npc_ai_since and npc_ai_since > 0),
            "since": npc_ai_since,
            "aggro_acquired": _num(npc_ai_stats.get("aggro_acquired")),
            "target_switched": _num(npc_ai_stats.get("target_switched")),
            "target_cleared": _num(npc_ai_stats.get("target_cleared")),
            "leash_broken": _num(npc_ai_stats.get("leash_broken")),
            "flee_started": npc_ai_flee_started,
            "flee_succeeded": npc_ai_flee_succeeded,
            "flee_delta": npc_ai_flee_delta,
            "time_in_combat_s": _num(npc_ai_stats.get("time_in_combat_s")),
        },
        "guilds": {
            "count": _num(guild_stats.get("count")),
            "members": _num(guild_stats.get("members_total")),
            "audit_entries": _num(guild_stats.get("audit_entries")),
            "audit_entries_written": _num(guild_stats.get("audit_entries_written")),
            "vault_gold_in": _num(guild_stats.get("vault_gold_in_total")),
            "vault_gold_out": _num(guild_stats.get("vault_gold_out_total")),
            "vault_gold_held": _num(guild_stats.get("vault_gold_total")),
            "vault_items": _num(guild_stats.get("vault_items_total")),
            "invites_pending": _num(guild_stats.get("invites_pending")),
            "conservation_drift": _vault_drift(guild_stats),
            "gold_conserved": guild_probe.get("gold_conserved"),
            "steps": _num(guild_probe.get("steps")),
            "skip_reason": _guild_skip_reason(guild_probe),
        },
        "liveness": {
            "min_alive": _min_alive(alive_samples),
            "final_alive": int(alive_samples[-1][1]) if alive_samples else None,
            "episodes": int(episode_samples[-1]) if episode_samples else None,
        },
        "determinism": {
            "final_tables": final_tables,
            "episodes": int(episode_samples[-1]) if episode_samples else None,
            "fingerprint": fingerprint(final_tables, episode_samples[-1:]),
        },
        "stability": {
            "economic_slope": _slope(stability_economy) if stability_economy else None,
            "level_slope": (
                _slope(stability_avg_levels) if stability_avg_levels else None
            ),
            "score_slope": (
                _slope(stability_total_scores) if stability_total_scores else None
            ),
            "population_ratio": population_ratio,
        },
    }


def fingerprint(tables, episodes):
    """Stable hash of the end state; same seed + scenario must reproduce it."""
    payload = json.dumps(
        {"tables": tables or {}, "episodes": episodes or []},
        sort_keys=True,
        default=str,
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def _min_alive(samples):
    """Liveness floor after the spawn ramp; a run shorter than the ramp
    falls back to its lowest sample rather than reporting nothing."""
    if not samples:
        return None
    steady = [value for t, value in samples if t >= LIVENESS_WARMUP_S]
    return int(min(steady if steady else [value for _, value in samples]))


# --- metric evaluation -----------------------------------------------------


def _treasury_points(measurements, thresholds):
    derived = measurements.get("derived") or {}
    series = (derived.get("treasury") or {}).get("series") or []
    steady = _num(resolve_threshold(thresholds, ("treasury", "steady_after_s"))) or 0.0
    return [p for p in series if (_num(p.get("t")) or 0.0) >= steady], steady


def _metric(measurements, thresholds, name):
    derived = measurements.get("derived") or {}
    treasury = derived.get("treasury") or {}
    economy = derived.get("economy") or {}
    combat = derived.get("combat") or {}
    craft = derived.get("craft") or {}
    market = derived.get("market") or {}
    conservation = derived.get("conservation") or {}
    liveness = derived.get("liveness") or {}
    progression = derived.get("progression") or {}
    party = derived.get("party") or {}
    acquisition = derived.get("acquisition") or {}
    guilds = derived.get("guilds") or {}
    npc_ai_v2 = derived.get("npc_ai_v2") or {}
    determinism = derived.get("determinism") or {}

    if name == "conservation.items_drift":
        value = conservation.get("items_drift")
        return abs(value) if value is not None else None
    if name == "conservation.gold_drift":
        value = conservation.get("gold_drift")
        return abs(value) if value is not None else None
    if name == "treasury.band_pct":
        points, _ = _treasury_points(measurements, thresholds)
        reserve = _num(treasury.get("reserve"))
        if not points or not reserve:
            return None
        return round(abs(points[-1]["value"] - reserve) / reserve * 100.0, 3)
    if name == "treasury.sink_grew":
        start, end = _num(treasury.get("sunk_start")), _num(treasury.get("sunk_end"))
        if start is None or end is None:
            return None
        reserve = _num(treasury.get("reserve"))
        values = [p["value"] for p in treasury.get("series") or []]
        if reserve and values and max(values) < reserve:
            # The reserve cap was never reached, so no inflow could sink:
            # skipping says "untested", which is not the same as "grew".
            return None
        return bool(end > start)
    if name == "treasury.monotonic_climb":
        points, _ = _treasury_points(measurements, thresholds)
        values = [p["value"] for p in points]
        if len(values) < 2:
            return None
        return all(later > earlier for earlier, later in pairwise(values))
    if name == "combat.deaths_per_hour":
        return combat.get("deaths_per_hour")
    if name == "combat.deaths_per_kill":
        return combat.get("deaths_per_kill")
    if name == "combat.wild_deaths_per_hour":
        return combat.get("wild_deaths_per_hour")
    if name == "combat.civ_deaths_per_hour":
        return combat.get("civ_deaths_per_hour")
    if name == "combat.civ_death_share_pct":
        return combat.get("civ_death_share_pct")
    if name == "combat.companion_damage_share_pct":
        return combat.get("companion_damage_share_pct")
    if name == "combat.deaths":
        return combat.get("deaths")
    if name == "combat.attacks_to_kill":
        return combat.get("attacks_to_kill")
    if name == "combat.potions_per_hour":
        return combat.get("potions_per_hour")
    if name == "craft.completions":
        return craft.get("completions")
    if name == "craft.input_loss":
        return craft.get("input_loss")
    if name == "craft.refund_excess":
        return craft.get("refund_excess")
    if name == "craft.negative_inventory":
        return craft.get("negative_inventory")
    if name == "market.median_spread_pct":
        return market.get("median_spread_pct")
    if name == "market.trade_volume":
        return market.get("trade_volume")
    if name == "market.wash_trades":
        return market.get("wash_trades")
    if name == "liveness.min_alive":
        return liveness.get("min_alive")
    if name == "liveness.final_alive":
        return liveness.get("final_alive")
    if name == "determinism.same_seed_match":
        return determinism.get("same_seed_match")
    if name == "determinism.fingerprint":
        return determinism.get("fingerprint")
    if name == "progression.quest_completions":
        return progression.get("quest_completions")
    if name == "progression.level_counts":
        return progression.get("level_counts") or None
    if name == "party.solo_clears":
        return party.get("solo_clears")
    if name == "party.group_clears":
        return party.get("group_clears")
    if name == "party.revive_gold_per_hour":
        return party.get("revive_gold_per_hour")
    if name == "acquisition.drop_rate":
        return acquisition.get("drop_rate")
    if name == "acquisition.craft_cost_ratio":
        return acquisition.get("craft_cost_ratio")
    if name == "acquisition.shop_time_to_earn_s":
        return acquisition.get("shop_time_to_earn_s")
    if name == "guilds.count":
        return guilds.get("count")
    if name == "guilds.members":
        return guilds.get("members")
    if name == "guilds.vault_gold_in":
        return guilds.get("vault_gold_in")
    if name == "guilds.vault_gold_out":
        return guilds.get("vault_gold_out")
    if name == "guilds.vault_items":
        return guilds.get("vault_items")
    if name == "guilds.audit_entries":
        return guilds.get("audit_entries")
    if name == "guilds.conservation_drift":
        value = guilds.get("conservation_drift")
        return abs(value) if value is not None else None
    if name == "guilds.gold_conserved":
        return guilds.get("gold_conserved")
    if name == "npc_ai_v2.telemetry_present":
        # Absent block is None (so the scenario is a skipped coverage gap),
        # not False: an empty run must fail coverage.npc_ai_v2, not a
        # telemetry check with no data behind it.
        if not npc_ai_v2:
            return None
        return bool(npc_ai_v2.get("telemetry_present"))
    if name == "npc_ai_v2.aggro_acquired":
        return npc_ai_v2.get("aggro_acquired")
    if name == "npc_ai_v2.flee_delta":
        return npc_ai_v2.get("flee_delta")
    if name == "economy.tax_effective_pct":
        return economy.get("tax_effective_pct")
    if name == "economy.sunk_lifetime":
        return economy.get("sunk_lifetime")
    return None


def _in_band(value, limits):
    low, high = limits
    return low <= value <= high


def _passes(value, op, limit):
    if op == "is":
        return bool(value) is bool(limit)
    if op == "in":
        return _in_band(float(value), limit)
    if op == "==":
        return math.isclose(float(value), float(limit), abs_tol=1e-9)
    if op == "<=":
        return float(value) <= float(limit)
    if op == ">=":
        return float(value) >= float(limit)
    raise ValueError(f"unknown check op: {op}")


def _limit_for(check, thresholds):
    _, _, _, op, limit_spec, _ = check
    if op == "is":
        return limit_spec
    if op == "in":
        return [
            resolve_threshold(thresholds, limit_spec[:2]),
            resolve_threshold(thresholds, limit_spec[2:]),
        ]
    return resolve_threshold(thresholds, limit_spec)


def _check_row(check, measurements, thresholds):
    check_id, scenario, metric_name, op, _, blocking = check
    value = _metric(measurements, thresholds, metric_name)
    row = {
        "id": check_id,
        "scenario": scenario,
        "metric": metric_name,
        "op": op,
        "value": value,
        "blocking": blocking,
        "provisional": True,
    }
    row["limit"] = _limit_for(check, thresholds)

    if value is None:
        row["status"] = "skip"
        row["detail"] = (
            f"no measurement for {metric_name} in this run; see the scenario reason"
        )
        return row
    try:
        ok = _passes(value, op, row["limit"])
    except (TypeError, ValueError) as exc:
        row["status"] = "fail"
        row["detail"] = f"cannot evaluate: {exc}"
        return row
    row["status"] = "pass" if ok else "fail"
    if not ok:
        row["detail"] = f"{metric_name}={value!r} violates {op} {row['limit']!r}"
    return row


def _median_across(values, op):
    if op == "is":
        # Identity checks are boolean, so the median seed rule is a strict
        # majority: an even split between a healthy and an unhealthy seed
        # must not pass.
        return 2 * sum(1 for value in values if value) > len(values)
    return statistics.median(values)


def _median_row(check, seed_rows, thresholds, seeds):
    check_id, scenario, metric_name, op, _, blocking = check
    values = [
        row["value"]
        for row in seed_rows
        if row["status"] != "skip" and row["value"] is not None
    ]
    row = {
        "id": f"{check_id}.median",
        "scenario": scenario,
        "metric": metric_name,
        "op": op,
        "value": None,
        "limit": _limit_for(check, thresholds),
        "blocking": blocking,
        "provisional": True,
        "seed": None,
    }
    if not values:
        row["status"] = "skip"
        row["detail"] = f"no seed of {seeds} produced a measurement for {metric_name}"
        return row
    value = _median_across(values, op)
    ok = _passes(value, op, row["limit"])
    row["value"] = value
    row["status"] = "pass" if ok else "fail"
    row["detail"] = (
        f"median across seeds {seeds}: {metric_name}={value!r} "
        f"{'satisfies' if ok else 'violates'} {op} {row['limit']!r}"
    )
    return row


def _worst_seed_row(check, seed_rows, seeds):
    check_id, scenario, metric_name, _, _, _ = check
    observed = [
        row for row in seed_rows if row["status"] != "skip" and row["value"] is not None
    ]
    row = {
        "id": f"{check_id}.worst_seed",
        "scenario": scenario,
        "metric": metric_name,
        "op": ">=",
        "value": None,
        "limit": WORST_SEED_DEATHS_PER_HOUR_MIN,
        "blocking": True,
        "provisional": True,
        "seed": None,
    }
    if not observed:
        row["status"] = "skip"
        row["detail"] = (
            f"no seed of {seeds} produced {metric_name}; " "worst-seed guard cannot run"
        )
        return row
    worst = min(observed, key=lambda item: item["value"])
    ok = worst["value"] >= WORST_SEED_DEATHS_PER_HOUR_MIN
    row["value"] = worst["value"]
    row["seed"] = worst.get("seed")
    row["status"] = "pass" if ok else "fail"
    row["detail"] = (
        f"worst seed {worst.get('seed')}: {metric_name}={worst['value']!r} "
        f"{'meets' if ok else 'violates'} floor "
        f">= {WORST_SEED_DEATHS_PER_HOUR_MIN}"
    )
    return row


def _aggregate_rows(runs, thresholds):
    """One median-of-seeds row per thresholded metric, plus the guard rows."""
    seed_rows = {}
    for run in runs:
        for row in run["checks"]:
            seed_rows.setdefault(row["id"], []).append({**row, "seed": run["seed"]})
    seeds = ", ".join(dict.fromkeys(str(run["seed"]) for run in runs))
    rows = []
    for check in CHECKS:
        own = seed_rows.get(check[0], [])
        rows.append(_median_row(check, own, thresholds, seeds))
        if check[0] in WORST_SEED_GUARD_IDS:
            rows.append(_worst_seed_row(check, own, seeds))
    return rows


def _scenario_rows(profile, checks, derived):
    rows = []
    for name in PROFILES[profile]["scenarios"]:
        spec = SCENARIOS.get(name, {})
        own = [c for c in checks if c["scenario"] == name]
        row = {"name": name, "title": spec.get("title", ""), "issue": spec.get("issue")}
        if spec.get("unlanded"):
            row.update(
                status="skipped", reason=spec["unlanded"], evaluated=0, checks=len(own)
            )
            rows.append(row)
            continue
        evaluated = [c for c in own if c["status"] in ("pass", "fail")]
        observed = [metric for scenario, metric in OBSERVATIONS if scenario == name]
        has_data = any(
            _metric({"derived": derived}, DEFAULT_THRESHOLDS, m) is not None
            for m in observed
        )
        if evaluated:
            row.update(
                status="ran", reason="", evaluated=len(evaluated), checks=len(own)
            )
        elif has_data:
            row.update(
                status="observed",
                reason="thresholds not evaluable in this run; observations recorded",
                evaluated=0,
                checks=len(own),
            )
        elif own:
            # A probe records why it produced nothing (absent verb, crash);
            # that reason is the useful one, not the generic check detail.
            block = derived.get(name)
            hint = block.get("skip_reason") if isinstance(block, dict) else None
            row.update(
                status="skipped",
                reason=hint or own[0].get("detail", "no evaluable measurement"),
                evaluated=0,
                checks=len(own),
            )
        else:
            row.update(
                status="skipped",
                reason="no measurable data in this run",
                evaluated=0,
                checks=0,
            )
        rows.append(row)
    return rows


def evaluate_run(measurements, thresholds, profile="smoke", seed=None):
    """Score one run. Pure: no server, no clock beyond the report stamp."""
    checks = [_check_row(check, measurements, thresholds) for check in CHECKS]
    derived = measurements.get("derived") or {}
    scenarios = _scenario_rows(profile, checks, derived)
    coverage = PROFILES[profile].get("coverage", ())

    # --- stability profile assertions ---
    if profile == "stability":
        stability = derived.get("stability") or {}
        economic_slope = _num(stability.get("economic_slope"))
        level_slope = _num(stability.get("level_slope"))
        score_slope = _num(stability.get("score_slope"))
        population_ratio = _num(stability.get("population_ratio"))

        if economic_slope is not None and economic_slope < -1e-4:
            checks.append(
                {
                    "id": "stability.economic_drift",
                    "scenario": "economy",
                    "metric": "stability.economic_drift",
                    "op": "<=",
                    "value": economic_slope,
                    "limit": ("stability", "economic_slope_max"),
                    "blocking": True,
                    "provisional": True,
                    "status": "fail",
                    "detail": f"economic_drift slope={economic_slope:.6f} too negative; not converging",
                }
            )
        if level_slope is not None and level_slope < -1e-4:
            checks.append(
                {
                    "id": "stability.level_decline",
                    "scenario": "progression",
                    "metric": "stability.level_slope",
                    "op": "<=",
                    "value": level_slope,
                    "limit": ("stability", "level_slope_max"),
                    "blocking": True,
                    "provisional": True,
                    "status": "fail",
                    "detail": f"level_slope={level_slope:.6f} negative; progression not monotonic",
                }
            )
        if score_slope is not None and score_slope < -1e-4:
            checks.append(
                {
                    "id": "stability.score_decline",
                    "scenario": "progression",
                    "metric": "stability.score_slope",
                    "op": "<=",
                    "value": score_slope,
                    "limit": ("stability", "score_slope_max"),
                    "blocking": True,
                    "provisional": True,
                    "status": "fail",
                    "detail": f"score_slope={score_slope:.6f} negative; score not monotonic",
                }
            )
        if population_ratio is not None and population_ratio < 0.8:
            checks.append(
                {
                    "id": "stability.population_vitality",
                    "scenario": "liveness",
                    "metric": "stability.population_ratio",
                    "op": "<=",
                    "value": population_ratio,
                    "limit": ("stability", "min_population_ratio"),
                    "blocking": True,
                    "provisional": True,
                    "status": "fail",
                    "detail": f"population_ratio={population_ratio:.3f} below 80%% threshold",
                }
            )

    # A coverage scenario with no evaluable check is a failed gate:
    # otherwise an empty run would pass with every check skipped. Scenarios
    # whose thresholds need a longer window (or telemetry this build does
    # not expose) record observations instead and are listed as skipped.
    for row in scenarios:
        if row["name"] not in coverage:
            continue
        if row["status"] != "skipped" or row.get("evaluated"):
            continue
        checks.append(
            {
                "id": f"coverage.{row['name']}",
                "scenario": row["name"],
                "metric": row["name"],
                "op": "coverage",
                "value": None,
                "limit": "evaluable measurement",
                "blocking": True,
                "provisional": False,
                "status": "fail",
                "detail": f"scenario {row['name']} produced no evaluable "
                "measurement; the gate cannot pass on absence of evidence",
            }
        )

    summary = _summarize(checks)
    return {
        "seed": seed,
        "verdict": "FAIL" if summary["blocking_fail"] else "PASS",
        "checks": checks,
        "scenarios": scenarios,
        "summary": summary,
    }


def _summarize(checks):
    failures = [c for c in checks if c["status"] == "fail"]
    skipped = [c for c in checks if c["status"] == "skip"]
    passed = [c for c in checks if c["status"] == "pass"]
    blocking = [c for c in failures if c.get("blocking", True)]
    return {
        "pass": len(passed),
        "fail": len(failures),
        "skip": len(skipped),
        "blocking_fail": len(blocking),
        "failing": [c["id"] for c in failures],
        "skipped": [c["id"] for c in skipped],
    }


def _determinism_checks(measurements_by_run, runs):
    """One same-seed fingerprint check per seed (skipped without repeats)."""
    by_seed = {}
    for entry, run in zip(measurements_by_run, runs):
        measurements = entry.get("measurements", entry)
        seed = run["seed"]
        by_seed.setdefault(seed, []).append(
            (measurements.get("derived") or {}).get("determinism") or {}
        )
    checks = []
    for seed, fingerprints in by_seed.items():
        unique = {fp.get("fingerprint") for fp in fingerprints}
        checks.append(
            {
                "id": f"determinism.same_seed.{seed}",
                "scenario": "determinism",
                "metric": "determinism.same_seed_match",
                "op": "is",
                "value": len(fingerprints) > 1 and len(unique) == 1,
                "limit": True,
                "blocking": True,
                "provisional": True,
                "status": (
                    "skip"
                    if len(fingerprints) < 2
                    else ("pass" if len(unique) == 1 else "fail")
                ),
                "detail": (
                    "single run for this seed; rerun with --repeats 2 to "
                    "compare fingerprints"
                    if len(fingerprints) < 2
                    else f"{len(unique)} distinct fingerprint(s) across "
                    f"{len(fingerprints)} runs"
                ),
            }
        )
    return checks


def evaluate_profile(measurements_by_run, thresholds, profile, baseline=None):
    """Score every run and add the cross-run determinism check."""
    runs = []
    for entry in measurements_by_run:
        measurements = entry.get("measurements", entry)
        seed = entry.get("seed", (measurements.get("meta") or {}).get("seed"))
        runs.append(evaluate_run(measurements, thresholds, profile, seed))

    determinism_on = bool((thresholds.get("determinism") or {}).get("enabled", True))
    if runs and determinism_on and "determinism" in PROFILES[profile]["scenarios"]:
        repeat_checks = _determinism_checks(measurements_by_run, runs)
        for index, run in enumerate(runs):
            seed = run["seed"]
            own = [
                check
                for check in repeat_checks
                if check["id"] == f"determinism.same_seed.{seed}"
            ]
            # One repeat check per seed, on the first run that seed appears
            # in; duplicating it per repeat would double-count the verdict.
            if own and not any(
                check["id"] == own[0]["id"]
                for earlier in runs[:index]
                for check in earlier["checks"]
            ):
                run["checks"].extend(own)
                run["summary"] = _summarize(run["checks"])
                run["verdict"] = "FAIL" if run["summary"]["blocking_fail"] else "PASS"

    all_checks = []
    # The profile verdict is decided on the medians and the worst-seed guard;
    # a single quiet seed no longer vetoes the gate, so its threshold rows stay
    # in the report as non-blocking evidence.
    per_seed_ids = {check[0] for check in CHECKS}
    for run in runs:
        for check in run["checks"]:
            if check["id"] in per_seed_ids:
                check["blocking"] = False
        run["summary"] = _summarize(run["checks"])
        run["verdict"] = "FAIL" if run["summary"]["blocking_fail"] else "PASS"
        for check in run["checks"]:
            all_checks.append({**check, "seed": run["seed"]})
    all_checks.extend(_aggregate_rows(runs, thresholds))
    summary = _summarize(all_checks)
    summary["verdict"] = "FAIL" if summary["blocking_fail"] else "PASS"
    first_derived = (
        (measurements_by_run[0].get("measurements", measurements_by_run[0])).get(
            "derived", {}
        )
        if measurements_by_run
        else {}
    )
    report = {
        "schema": REPORT_SCHEMA,
        "generated_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "profile": profile,
        "provisional_thresholds": True,
        "thresholds": thresholds,
        "seeds": [run["seed"] for run in runs],
        "runs": runs,
        "checks": all_checks,
        "scenarios": _scenario_rows(profile, all_checks, first_derived),
        "summary": summary,
    }
    if baseline and measurements_by_run:
        report["baseline"] = compare_baseline(measurements_by_run[0], baseline)
    return report


# --- baseline --------------------------------------------------------------


def _get_path(node, path):
    current = node
    for key in path:
        if not isinstance(current, dict) or key not in current:
            return None
        current = current[key]
    return current


BASELINE_METRICS = (
    ("treasury.end", ("treasury", "series"), ("market", "treasury")),
    ("market.sunk_lifetime", ("economy", "sunk_lifetime"), ("market", "sunk_lifetime")),
    ("market.trade_count", ("market", "trade_volume"), ("market", "trade_count")),
    (
        "economy.tax_effective_pct",
        ("economy", "tax_effective_pct"),
        ("market", "tax_effective_pct"),
    ),
    ("combat.deaths", ("combat", "deaths"), ("players", "deaths")),
    ("liveness.final_alive", ("liveness", "final_alive"), ("soak", "alive")),
)


def compare_baseline(measurement_entry, baseline):
    """Delta of the headline metrics against a stored baseline file.

    The baseline file may be a native capture (:data:`BASELINE_SCHEMA`) or
    the hand-distilled v0.10.0 file already in ``balance/``; both paths are
    tried. Deltas are informational -- the thresholds decide PASS/FAIL.
    """
    measurements = measurement_entry.get("measurements", measurement_entry)
    derived = measurements.get("derived") or {}
    base_derived = baseline.get("derived") or {}
    rows = []
    for label, current_path, baseline_path in BASELINE_METRICS:
        if current_path == ("treasury", "series"):
            points = _get_path(derived, current_path) or []
            current = points[-1]["value"] if points else None
        else:
            current = _get_path(derived, current_path)
        base = _get_path(base_derived, baseline_path)
        delta_pct = None
        if (
            isinstance(current, (int, float))
            and isinstance(base, (int, float))
            and base
        ):
            delta_pct = round((current - base) / base * 100.0, 2)
        rows.append(
            {
                "metric": label,
                "current": current,
                "baseline": base,
                "delta_pct": delta_pct,
            }
        )
    return {
        "path": baseline.get("_path", ""),
        "schema": baseline.get("schema", "distilled/v0"),
        "meta": {
            key: (baseline.get("meta") or {}).get(key)
            for key in ("name", "master_sha", "seed", "duration_s", "agents")
        },
        "deltas": rows,
    }


# --- live collection -------------------------------------------------------


class HarnessError(RuntimeError):
    """Preflight/collection failure: not a balance verdict, a broken harness."""


def fetch_state(http_url, timeout=10.0):
    url = http_url.rstrip("/") + "/api/state"
    with urllib.request.urlopen(url, timeout=timeout) as response:
        return json.loads(response.read().decode("utf-8"))


def _trim_state(state):
    """Keep only the fields the derivation reads, so reports stay small."""
    market = state.get("market") or {}
    quests = state.get("quests") or {}
    return {
        "server": {"players_online": (state.get("server") or {}).get("players_online")},
        "combat": {
            "player_damage": (state.get("combat") or {}).get("player_damage"),
            "companion_damage": (state.get("combat") or {}).get("companion_damage"),
        },
        "acquisition": state.get("acquisition") or {},
        "npc_ai": state.get("npc_ai") or {},
        # Client-side evidence from the guild probe: the server publishes vault
        # totals, never the per-op purse deltas the conservation identity needs.
        "guilds": state.get("guilds") or {},
        "guild_probe": state.get("guild_probe") or {},
        "market": {
            "treasury": market.get("treasury"),
            "reserve": market.get("reserve"),
            "sunk_lifetime": market.get("sunk_lifetime"),
            "tax_effective_pct": market.get("tax_effective_pct"),
            "trade_count": market.get("trade_count"),
            "orders": [
                {"item": o.get("item"), "price": o.get("price")}
                for o in (market.get("orders") or [])[:200]
            ],
            "bids": [
                {"item": b.get("item"), "price": b.get("price")}
                for b in (market.get("bids") or [])[:200]
            ],
            "history": [
                {
                    "buyer": h.get("buyer"),
                    "seller": h.get("seller"),
                    "item": h.get("item"),
                    "price": h.get("price"),
                }
                for h in (market.get("history") or [])[-50:]
            ],
        },
        "players": [
            {
                "name": p.get("name"),
                "level": p.get("level"),
                "deaths": p.get("deaths"),
                "kills": p.get("kills"),
                "wild_deaths": p.get("wild_deaths"),
                "civ_deaths": p.get("civ_deaths"),
                "gold": p.get("gold"),
                "companion": (
                    {
                        "species": (p.get("companion") or {}).get("species"),
                        "level": (p.get("companion") or {}).get("level"),
                    }
                    if p.get("companion")
                    else None
                ),
            }
            for p in (state.get("players") or [])
        ],
        "quests": {"completions": quests.get("completions") or {}},
        "craft_history": [
            {"seq": c.get("seq")} for c in (state.get("craft_history") or [])[-20:]
        ],
        "dungeon_clears": {
            "solo_clears": (state.get("dungeon_clears") or {}).get("solo_clears"),
            "group_clears": (state.get("dungeon_clears") or {}).get("group_clears"),
        },
        "scores": [
            {"name": s.get("name"), "level": s.get("level"), "score": s.get("score")}
            for s in (state.get("scores") or [])
        ],
        "total_score": state.get("total_score"),
    }


def _gm_snapshot(gm_url):
    from ml.conductor.soak import gm_tables_snapshot

    return gm_tables_snapshot(gm_url)


async def _sample(cond, http_url, gm_url, started, state=None):
    if state is None:
        state = await asyncio.to_thread(fetch_state, http_url)
    tables = await _gm_snapshot(gm_url)
    status = cond.status()
    by_type = status.get("by_type") or {}
    episodes = sum(int(cell.get("episodes", 0)) for cell in by_type.values())
    return {
        "t": round(time.time() - started, 1),
        "alive": int(status["registry"]["alive"]),
        "episodes": episodes,
        "state": _trim_state(state),
        "tables": tables,
    }


async def _sample_loop(cond, http_url, gm_url, interval, stop, started):
    samples = []
    while not stop.is_set():
        try:
            samples.append(await _sample(cond, http_url, gm_url, started))
        except (urllib.error.URLError, OSError, ValueError) as exc:
            print(f"[balance] sample failed: {type(exc).__name__}: {exc}", flush=True)
        try:
            await asyncio.wait_for(stop.wait(), timeout=interval)
        except TimeoutError:
            pass
    return samples


async def _guild_sample(args, cond, seed, started):
    """Run the guild probe and turn it into one sample of the run.

    The probe's peak /api/state read lands here, so the guild rows travel the
    same trim -> telemetry -> derive path as every other counter. A probe that
    raises leaves no snapshot behind: the scenario then has no measurement and
    the gate fails it instead of scoring an empty world.
    """
    from ml.guild_probe import run_guild_probe

    try:
        probe = await run_guild_probe(args.url, args.http_url, seed, fetch_state)
    except Exception as exc:  # noqa: BLE001 - a probe crash must fail the gate
        probe = {"error": f"{type(exc).__name__}: {exc}"}
    for problem in probe.get("problems") or []:
        print(f"[balance] guild probe problem: {problem}", flush=True)
    print(
        f"[balance] guild probe: guild={probe.get('guild')} steps={probe.get('steps')} "
        f"gold_conserved={probe.get('gold_conserved')} "
        f"skipped={probe.get('skipped') or '-'} error={probe.get('error') or '-'}",
        flush=True,
    )
    snapshot = probe.pop("state", None)
    if not isinstance(snapshot, dict) or not snapshot:
        snapshot = await asyncio.to_thread(fetch_state, args.http_url)
    snapshot = dict(snapshot)
    snapshot["guild_probe"] = probe
    try:
        return await _sample(cond, args.http_url, args.gm_url, started, state=snapshot)
    except (urllib.error.URLError, OSError, ValueError) as exc:
        print(f"[balance] guild sample failed: {exc}", flush=True)
        return await _sample(cond, args.http_url, args.gm_url, started)


def _slots(profile, args):
    from ml.plugins import parse_slot

    slots = []
    for spec in PROFILES[profile]["slots"]:
        slot = parse_slot(spec)
        slot["env"].setdefault("reward_mode", args.reward_mode)
        slot["env"].setdefault("max_steps", args.max_steps)
        slot.setdefault("step_timeout", args.step_timeout)
        slots.append(slot)
    return slots


async def collect(args, profile, seed):
    """Run one profile/seed against a live server and return measurements."""
    from ml.conductor import Conductor
    from ml.runlog import seed_everything

    spec = PROFILES[profile]
    if args.sample_every <= 0:
        raise HarnessError("--sample-every must be greater than zero")
    try:
        await asyncio.to_thread(fetch_state, args.http_url)
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise HarnessError(
            f"dashboard {args.http_url}/api/state unreachable ({exc}); "
            "start the server first (see README, Balance gate)"
        )

    seed_everything(seed)
    base_dir = os.path.join(args.run_dir, f"{profile}-{seed}-{int(time.time())}")
    cond = Conductor(
        base_dir,
        max_agents=args.agents or spec["agents"],
        arrivals_per_minute=args.arrivals,
        mean_lifetime_episodes=args.lifetime,
        runners=_slots(profile, args),
        url=args.url,
        resume=False,
        reset="all",
    )
    duration = args.duration or spec["duration_s"]
    stop = asyncio.Event()
    started = time.time()
    print(
        f"[balance] {profile} seed={seed} {args.agents or spec['agents']} agents "
        f"for {duration:.0f}s",
        flush=True,
    )
    sampler = asyncio.create_task(
        _sample_loop(cond, args.http_url, args.gm_url, args.sample_every, stop, started)
    )
    try:
        await cond.run(duration_seconds=duration, wave_size=10, wave_delay=1.0)
    finally:
        stop.set()
    samples = await sampler
    if "guilds" in spec["scenarios"]:
        samples.append(await _guild_sample(args, cond, seed, started))
    try:
        samples.append(await _sample(cond, args.http_url, args.gm_url, started))
    except (urllib.error.URLError, OSError, ValueError) as exc:
        print(f"[balance] final sample failed: {exc}", flush=True)

    status = cond.status()
    measurements = {
        "meta": {
            "profile": profile,
            "seed": seed,
            "agents": args.agents or spec["agents"],
            "duration_s": duration,
            "started_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(started)),
            "finished_at_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "slots": list(spec["slots"]),
            "url": args.url,
            "dashboard": args.http_url,
            "gm_url": args.gm_url,
            "seed_money_env": os.environ.get("TEXTMMO_GM_SEED"),
            "commit": args.commit,
            "python": sys.version.split()[0],
        },
        "samples": samples,
        "derived": derive(samples, telemetry=_telemetry(samples)),
        "conductor": {
            "alive": status["registry"]["alive"],
            "by_type": status.get("by_type") or {},
        },
    }
    return measurements


# --- CLI -------------------------------------------------------------------


def _write_json(path, payload):
    directory = os.path.dirname(os.path.abspath(path))
    os.makedirs(directory, exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2, sort_keys=False, default=str)
        fh.write("\n")


def _print_verdict(report):
    summary = report["summary"]
    print(
        f"[balance] {report['profile']} seeds={report['seeds']}: "
        f"{summary['verdict']} "
        f"(pass={summary['pass']} fail={summary['fail']} skip={summary['skip']})",
        flush=True,
    )
    for check in report["checks"]:
        if check["status"] == "fail":
            print(f"[balance]   FAIL {check['id']}: {check.get('detail', '')}")
    for scenario in report["scenarios"]:
        if scenario["status"] == "skipped":
            print(f"[balance]   SKIP {scenario['name']}: {scenario['reason']}")


def _load_json(path):
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def _load_baseline(path):
    baseline = _load_json(path)
    baseline["_path"] = path
    return baseline


def cmd_matrix(args):
    print(f"{'scenario':<14} {'status':<9} detail")
    for name, spec in SCENARIOS.items():
        status = (
            "unlanded"
            if spec.get("unlanded")
            else ("observed" if spec.get("observed_only") else "gate")
        )
        detail = spec.get("unlanded") or spec.get("observed_only") or spec["title"]
        print(f"{name:<14} {status:<9} {detail}")
    for name, spec in PROFILES.items():
        print(
            f"\nprofile {name}: {spec['description']}\n"
            f"  duration={spec['duration_s']:.0f}s agents={spec['agents']} "
            f"min_agents={spec['min_agents']} seeds={spec['seeds']}\n"
            f"  scenarios={', '.join(spec['scenarios'])}"
        )
    print(
        f"\nbaseline: {DEFAULT_BASELINE} (reference commit {DEFAULT_REFERENCE_COMMIT})"
    )
    return 0


def cmd_evaluate(args):
    measurements = _load_json(args.measurements)
    for field in REQUIRED_MEASUREMENT_FIELDS:
        if field not in measurements:
            print(f"[balance] measurement file missing {field!r}", file=sys.stderr)
            return 2
    profile = args.profile or (measurements.get("meta") or {}).get("profile") or "smoke"
    if profile not in PROFILES:
        print(f"[balance] unknown profile {profile!r}", file=sys.stderr)
        return 2
    thresholds = load_thresholds(args.thresholds, profile)
    baseline = _load_baseline(args.baseline) if args.baseline else None
    report = evaluate_profile(
        [
            {
                "seed": (measurements.get("meta") or {}).get("seed"),
                "measurements": measurements,
            }
        ],
        thresholds,
        profile,
        baseline=baseline,
    )
    _print_verdict(report)
    if args.out:
        _write_json(args.out, report)
        print(f"[balance] report: {args.out}")
    return 0 if report["summary"]["verdict"] == "PASS" else 1


async def _run(args, profile, seeds):
    thresholds = load_thresholds(args.thresholds, profile)
    baseline = _load_baseline(args.baseline) if args.baseline else None
    entries = []
    for seed in seeds:
        measurements = await collect(args, profile, seed)
        entries.append({"seed": seed, "measurements": measurements})
        if args.save_measurements:
            path = os.path.join(args.out_dir, f"measurements-{profile}-{seed}.json")
            _write_json(path, measurements)
            print(f"[balance] measurements: {path}")
    report = evaluate_profile(entries, thresholds, profile, baseline=baseline)
    _print_verdict(report)
    out = args.out or os.path.join(args.out_dir, "balance_report.json")
    _write_json(out, report)
    print(f"[balance] report: {out}")
    return 0 if report["summary"]["verdict"] == "PASS" else 1


async def _capture_baseline(args):
    thresholds = load_thresholds(args.thresholds, "baseline")
    measurements = await collect(args, "baseline", args.seed)
    report = evaluate_profile(
        [{"seed": args.seed, "measurements": measurements}], thresholds, "baseline"
    )
    _print_verdict(report)
    if args.save_measurements:
        path = os.path.join(args.out_dir, f"measurements-baseline-{args.seed}.json")
        _write_json(path, measurements)
        print(f"[balance] measurements: {path}")
    meta = dict(measurements["meta"])
    label = args.commit or "unknown"
    meta["name"] = f"v0.11.0 balance baseline ({label})"
    meta["baseline_commit"] = args.commit
    payload = {
        "schema": BASELINE_SCHEMA,
        "meta": meta,
        "thresholds": thresholds,
        "derived": measurements["derived"],
        "verdict": report["summary"],
    }
    _write_json(args.out, payload)
    print(f"[balance] baseline: {args.out}")
    return 0


def cmd_run(args):
    spec = PROFILES[args.profile]
    seeds = tuple(args.seeds.split(",")) if args.seeds else spec["seeds"]
    seeds = [int(seed) for seed in seeds]
    if args.repeats > 1:
        seeds = seeds * args.repeats
    if args.baseline is None and os.path.isfile(DEFAULT_BASELINE):
        args.baseline = DEFAULT_BASELINE
    if args.baseline and not os.path.isfile(args.baseline):
        print(f"[balance] baseline not found: {args.baseline}", file=sys.stderr)
        return 2
    try:
        return asyncio.run(_run(args, args.profile, seeds))
    except HarnessError as exc:
        print(f"[balance] harness error: {exc}", file=sys.stderr)
        return 2


def cmd_baseline(args):
    try:
        return asyncio.run(_capture_baseline(args))
    except HarnessError as exc:
        print(f"[balance] harness error: {exc}", file=sys.stderr)
        return 2


def _add_run_args(parser, profile):
    defaults = PROFILES[profile]
    parser.add_argument("--url", default="ws://127.0.0.1:8765")
    parser.add_argument("--http-url", default="http://127.0.0.1:8766")
    parser.add_argument("--gm-url", default="ws://127.0.0.1:8767")
    parser.add_argument(
        "--duration",
        type=float,
        default=None,
        help=f"seconds (default {defaults['duration_s']:.0f})",
    )
    parser.add_argument(
        "--agents",
        type=int,
        default=None,
        help=f"concurrent agents (default {defaults['agents']})",
    )
    parser.add_argument("--sample-every", type=float, default=30.0)
    parser.add_argument("--arrivals", type=float, default=5.0)
    parser.add_argument("--lifetime", type=int, default=15)
    parser.add_argument(
        "--reward-mode", default="score", choices=("score", "xp", "econ")
    )
    parser.add_argument("--max-steps", type=int, default=500)
    parser.add_argument("--step-timeout", type=float, default=30.0)
    parser.add_argument("--seed", type=int, default=defaults["seeds"][0])
    parser.add_argument(
        "--seeds", default=None, help="comma-separated seeds (default from the profile)"
    )
    parser.add_argument(
        "--repeats",
        type=int,
        default=1,
        help="repeat every seed N times for the determinism check",
    )
    parser.add_argument(
        "--thresholds",
        default=None,
        help="JSON object deep-merged over the provisional defaults",
    )
    parser.add_argument(
        "--baseline",
        default=None,
        help=f"baseline file for the comparison block "
        f"(default {DEFAULT_BASELINE} when present)",
    )
    parser.add_argument(
        "--commit", default=None, help="commit under test (recorded in meta)"
    )
    parser.add_argument(
        "--run-dir",
        default=os.path.join("soak_run", "balance"),
        help="conductor state dir (gitignored)",
    )
    parser.add_argument("--out-dir", default="balance")
    parser.add_argument("--save-measurements", action="store_true")


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description="Balance gate harness (#537): scenario matrix, "
        "provisional thresholds, PASS/FAIL report."
    )
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("matrix", help="list scenarios, profiles and skips")

    evaluate = sub.add_parser("evaluate", help="score a stored measurement file")
    evaluate.add_argument("--measurements", required=True)
    evaluate.add_argument("--profile", default=None)
    evaluate.add_argument("--thresholds", default=None)
    evaluate.add_argument("--baseline", default=None)
    evaluate.add_argument("--out", default=None)

    run = sub.add_parser("run", help="run a profile against a live server")
    run.add_argument(
        "--profile", default="smoke", choices=("smoke", "gate", "stability")
    )
    _add_run_args(run, "smoke")
    run.add_argument(
        "--out",
        default=None,
        help="report path (default <out-dir>/balance_report.json)",
    )

    baseline = sub.add_parser("baseline", help="capture a reference baseline")
    baseline.set_defaults(profile="baseline")
    _add_run_args(baseline, "baseline")
    baseline.add_argument("--out", default=DEFAULT_BASELINE)

    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv)
    if args.command == "matrix":
        return cmd_matrix(args)
    if args.command == "evaluate":
        return cmd_evaluate(args)
    if args.command == "run":
        return cmd_run(args)
    if args.command == "baseline":
        return cmd_baseline(args)
    raise SystemExit(f"unknown command {args.command!r}")


if __name__ == "__main__":
    raise SystemExit(main())
