"""Seedable floor plans for the dungeon (#67).

The server owns the dungeon state machine: room ids, the Warden, clear
rewards and respawn. This module only decides what goes on a formula floor --
guard count, names, hp/attack, gold, loot and respawn -- as a pure function
of a config and a seed. Nothing here imports the game server.

A plan is a plain JSON-safe dict. A caller may turn one into a callable with
floor_factory() and install it as server.DUNGEON_FLOOR_FACTORY. With no
factory installed the server keeps its built-in formula unchanged.
"""

import hashlib
import json
import os
import random

SCHEMA_VERSION = 1

# Floor MAX_FLOOR is the Warden and is deliberately off-formula. Generated
# plans only describe formula floors, so this is the highest one we emit.
MAX_FORMULA_FLOORS = 49

DEFAULT_LAYOUT = "single"

DEFAULT_CONFIG = {
    "floors": 9,
    "base_hp": 20,
    "base_atk": 4,
    "hp_growth": 0.35,
    "atk_growth": 0.5,
    "guard_base": 1,
    "guard_step": 2,
    "guard_cap": 4,
    "jitter_pct": 10,
    "gold_base": 3,
    "loot_table": [],
    "guard_names": ["Dungeon Guard {n}-{k}"],
    "respawn_base": 20,
    "respawn_step": 10,
    "layout": DEFAULT_LAYOUT,
    "seed": 0,
}

# Keys a factory may override on the server's built-in params dict.
OVERRIDABLE = (
    "hp",
    "atk",
    "count",
    "gold",
    "loot",
    "respawn_seconds",
    "name",
)


def normalize_config(raw=None):
    """Validate a config dict and return a fresh, fully populated copy."""
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise ValueError("config must be a JSON object")
    unknown = sorted(set(raw) - set(DEFAULT_CONFIG))
    if unknown:
        raise ValueError("unknown config keys: " + ", ".join(unknown))

    cfg = dict(DEFAULT_CONFIG)
    cfg.update(raw)

    def number(key, kind, low, high):
        try:
            value = kind(cfg[key])
        except (TypeError, ValueError):
            raise ValueError(f"{key} must be a {kind.__name__}")
        if value < low or value > high:
            raise ValueError(f"{key} must be between {low} and {high}")
        return value

    cfg["floors"] = number("floors", int, 1, MAX_FORMULA_FLOORS)
    cfg["base_hp"] = number("base_hp", int, 1, 100000)
    cfg["base_atk"] = number("base_atk", int, 1, 100000)
    cfg["hp_growth"] = number("hp_growth", float, 0.0, 10.0)
    cfg["atk_growth"] = number("atk_growth", float, 0.0, 10.0)
    cfg["guard_base"] = number("guard_base", int, 1, 8)
    cfg["guard_step"] = number("guard_step", int, 1, 20)
    cfg["guard_cap"] = number("guard_cap", int, 1, 8)
    cfg["jitter_pct"] = number("jitter_pct", int, 0, 50)
    cfg["gold_base"] = number("gold_base", int, 0, 100000)
    cfg["respawn_base"] = number("respawn_base", int, 1, 100000)
    cfg["respawn_step"] = number("respawn_step", int, 0, 100000)

    table = cfg["loot_table"]
    if table is None:
        table = []
    if not isinstance(table, (list, tuple)):
        raise ValueError("loot_table must be a list of item ids")
    table = [str(iid).strip() for iid in table if str(iid).strip()]
    cfg["loot_table"] = table

    names = cfg["guard_names"]
    if not isinstance(names, (list, tuple)) or not names:
        raise ValueError("guard_names must be a non-empty list")
    names = [str(name).strip() for name in names if str(name).strip()]
    if not names:
        raise ValueError("guard_names must contain a non-empty name")
    cfg["guard_names"] = names

    # Multi-room layouts cannot work: the server parses d_<id>_f<int> in
    # several hot paths and a second room per floor breaks NPC lookup,
    # ground loot and clear detection. Refuse rather than imply support.
    if cfg["layout"] != DEFAULT_LAYOUT:
        raise ValueError(
            "only single-room layouts are supported: multi-room ids break "
            "the server's dungeon room parser"
        )

    seed = cfg["seed"]
    if seed is not None:
        try:
            seed = int(seed)
        except (TypeError, ValueError):
            raise ValueError("seed must be an integer or null")
    cfg["seed"] = seed
    return cfg


def _floor_rng(config, floor_no):
    """One independent RNG per floor, so plan order cannot change a floor."""
    seed = normalize_config(config)["seed"]
    return random.Random(f"{seed}:{floor_no}")


def _jitter(rng, value, pct):
    if not pct or value <= 0:
        return value
    return max(1, round(value * (100 + rng.randint(-pct, pct)) / 100.0))


def floor_params(config, floor_no, rng=None):
    """Tunable parameters for one formula floor.

    No aggro/leash/flee fields are produced: a guard with any of them gets
    the full NPC state machine on the server, which is a different feature.
    Guard variety here is cosmetic (name) plus hp/attack/count/loot tuning.
    """
    cfg = normalize_config(config)
    n = int(floor_no)
    if n < 1 or n > cfg["floors"]:
        raise ValueError(f"floor must be between 1 and {cfg['floors']}")
    chooser = rng if rng is not None else _floor_rng(cfg, n)

    hp = round(cfg["base_hp"] * (1 + cfg["hp_growth"]) ** (n - 1))
    atk = round(cfg["base_atk"] * (1 + cfg["atk_growth"]) ** (n - 1))
    gold = round(cfg["gold_base"] * (1 + cfg["hp_growth"]) ** (n - 1))
    hp = _jitter(chooser, hp, cfg["jitter_pct"])
    atk = _jitter(chooser, atk, cfg["jitter_pct"])

    count = min(cfg["guard_base"] + (n - 1) // cfg["guard_step"], cfg["guard_cap"])
    if cfg["jitter_pct"]:
        count = max(1, min(cfg["guard_cap"], count + chooser.choice((-1, 0, 1))))

    if cfg["loot_table"]:
        loot = [chooser.choice(cfg["loot_table"])]
    else:
        # Same fallback the built-in generator registers on demand.
        loot = [f"dungeon_shard_{n}"]

    return {
        "hp": hp,
        "atk": atk,
        "count": count,
        "gold": gold,
        "loot": loot,
        "respawn_seconds": cfg["respawn_base"] + n * cfg["respawn_step"],
        "name": chooser.choice(cfg["guard_names"]),
    }


def _guard_name(template, n, k):
    try:
        return str(template).format(n=n, k=k)
    except (KeyError, ValueError, IndexError):
        return f"Dungeon Guard {n}-{k}"


def generate_plan(raw_config=None, dungeon_id=0):
    """Build a complete, reproducible plan for formula floors 1..N."""
    cfg = normalize_config(raw_config)
    floors = []
    for n in range(1, cfg["floors"] + 1):
        params = floor_params(cfg, n)
        guards = []
        for k in range(params["count"]):
            guards.append(
                {
                    "id": f"dg_{int(dungeon_id)}_{n}_{k}",
                    "name": _guard_name(params["name"], n, k),
                    "hp": params["hp"],
                    "atk": params["atk"],
                    "loot": list(params["loot"]),
                    "gold": params["gold"],
                    "respawn_seconds": params["respawn_seconds"],
                }
            )
        floors.append({"floor": n, "params": params, "guards": guards})
    body = {"config": cfg, "floors": floors}
    fingerprint = hashlib.sha1(
        json.dumps(body, sort_keys=True).encode("utf-8")
    ).hexdigest()[:12]
    return {
        "schema_version": SCHEMA_VERSION,
        "fingerprint": fingerprint,
        "dungeon_id": int(dungeon_id),
        **body,
    }


def floor_factory(plan):
    """Adapter from a plan to server.DUNGEON_FLOOR_FACTORY.

    The callable receives (dungeon_id, floor_no, builtin_params) and returns
    builtin_params with any planned overrides applied. Floors the plan does
    not describe are returned untouched.
    """
    if not isinstance(plan, dict) or plan.get("schema_version") != SCHEMA_VERSION:
        raise ValueError("plan has an unsupported schema_version")
    overrides = {}
    for row in plan.get("floors", []):
        if not isinstance(row, dict):
            continue
        n = row.get("floor")
        params = row.get("params")
        if isinstance(n, int) and isinstance(params, dict):
            overrides[n] = params

    def factory(dungeon_id, floor_no, builtin_params):
        params = dict(builtin_params)
        override = overrides.get(int(floor_no))
        if override is None:
            return params
        for key in OVERRIDABLE:
            if key in override:
                params[key] = override[key]
        return params

    return factory


def _atomic_write_json(path, obj):
    target = os.path.abspath(path)
    parent = os.path.dirname(target)
    if parent:
        os.makedirs(parent, exist_ok=True)
    tmp = target + ".tmp"
    with open(tmp, "w", encoding="utf-8") as handle:
        json.dump(obj, handle, indent=2, sort_keys=True)
        handle.write("\n")
    os.replace(tmp, target)
    return target


def write_plan(plan, path):
    """Persist a plan under an explicit path, returning its absolute name."""
    return _atomic_write_json(path, plan)


def read_plan(path):
    """Read a plan written by write_plan; missing files give an empty plan."""
    if not os.path.exists(path):
        return {}
    with open(path, encoding="utf-8") as handle:
        return json.load(handle)
