"""Declarative schema for the two operator-editable config files (#63).

Neither loader declares a schema: `server.py::_apply_config` and
`ml/ml_env.py::_apply_ml_config` both coerce with `type(default)(value)`
and validate nothing else, so a negative tax rate or a 99999 dungeon floor
loads silently. The dashboard editor needs bounds and help text to render
and validate a form, and this is the only place those can live without
teaching the loaders something they do not already do.

Bounds are a floor for catching typos, not a balance opinion. Values here
are rejected only where they are structurally impossible (a negative count,
a percentage over 100, a growth rate below 1). Widening them is a tuning
decision and belongs to the operator.

Defaults are deliberately absent for server_config.json. The default for a
key is the module global it overrides, and duplicating those numbers here is
how a schema rots: `server.py` resolves them from `globals()`, so a default
can never drift from the code it came from.

The ML keys are the exception, and only because the server cannot resolve
them: reading ml_env's globals would import the whole env (and its torch
side) into the game server for six numbers. ML_DEFAULTS below is pinned
against ml_env by tests/test_config_editor.py, which fails if the module
moves.
"""

# ---------------------------------------------------------------------------
# Bounds + help. type is "int" or "float"; "list" keys take a bracketed
# list of numbers and are validated elementwise.
# ---------------------------------------------------------------------------


def _i(minimum, maximum, help_text, step=1):
    return {
        "type": "int",
        "min": minimum,
        "max": maximum,
        "step": step,
        "help": help_text,
    }


def _f(minimum, maximum, help_text, step=0.01):
    return {
        "type": "float",
        "min": minimum,
        "max": maximum,
        "step": step,
        "help": help_text,
    }


def _pct(help_text):
    """0-100 percentage; stored as a number, not a 0-1 fraction."""
    return {"type": "float", "min": 0.0, "max": 100.0, "step": 0.5, "help": help_text}


def _ratio(help_text):
    """0-1 fraction (tax rate, share of a pool)."""
    return {"type": "float", "min": 0.0, "max": 1.0, "step": 0.01, "help": help_text}


def _list(count, minimum, maximum, help_text):
    return {
        "type": "list",
        "count": count,
        "min": minimum,
        "max": maximum,
        "step": 1,
        "help": help_text,
    }


# ---------------------------------------------------------------------------
# server_config.json
# ---------------------------------------------------------------------------

SERVER_SCHEMA = {
    "scoring": {
        "ACTION_WINDOW": _i(
            1, 500, "Actions scored per decision window; the agent's move horizon."
        ),
        "MIN_HISTORY_FOR_VARIETY": _i(
            0, 100, "Actions required before variety bonus counts."
        ),
        "MIN_VARIETY": _ratio("Minimum action diversity for the variety bonus."),
        "DIFFICULTY_K": _f(
            0.0, 1000.0, "Difficulty scaling constant for spawned mobs."
        ),
        "DISCOVERY_POINTS": _i(0, 1000, "Score points for finding a new area."),
        "DISCOVERY_XP": _i(0, 1000, "XP for finding a new area."),
        "DEATH_PENALTY": _f(0.0, 1000.0, "Flat score penalty floor on death."),
        "DEATH_GOLD_DROP_PCT": _pct(
            "Share of carried gold dropped as a floor pile on death."
        ),
        "DEATH_GOLD_LOST_PCT": _pct(
            "Share of carried gold destroyed permanently on death."
        ),
        "DEATH_SCORE_PER_GOLD_LOST": _f(
            0.0, 100.0, "Extra score penalty per gold permanently lost."
        ),
        "DEATH_ITEM_DROP_PCT": _pct("Share of carried items dropped on death."),
        "SCORE_ENTRY_MAX": _i(1, 1000000, "Leaderboard entries retained."),
        "SCORE_ENTRY_TTL_SECONDS": _i(
            60, 31536000, "Seconds before a leaderboard entry expires."
        ),
    },
    "economy": {
        "TAX_RATE": _ratio("Market tax share of each sale."),
        "TAX_MINIMUM": _i(0, 10000, "Flat minimum tax per sale, in gold."),
        "TREASURY_RESERVE": _f(
            0, 1000000, "Gold the treasury banks before sinking the rest."
        ),
        "MARKET_ORDER_SLOTS_BASE": _i(
            1, 200, "Base sell order slots available to every character."
        ),
        "MARKET_SLOT_PRICE_BASE": _i(
            1, 100000, "Gold cost of one extra market slot at base."
        ),
        "MARKET_ORDER_TTL_SECONDS": _i(
            60, 31536000, "Seconds a sell order stays listed."
        ),
        "BROKER_FEE_PCT": _pct("Agent-to-agent listing fee, percent."),
        "RELIST_FEE_PCT": _pct("Fee to relist an expired order, percent."),
        "INVENTORY_CAP": _i(1, 500, "Item slots carried before the bag is full."),
        "AMMO_EXEMPT_COUNT": _i(
            0, 500, "Ammo stacks that never count against the inventory cap."
        ),
        "STARTING_GOLD": _i(0, 1000000, "Gold every new character begins with."),
        "MOB_EXTRA_SPAWNS": _i(
            0, 20, "Extra hostile instances of each mob template (startup-only)."
        ),
    },
    "commissions": {
        "COMMISSION_MAX_XP": _i(
            0, 100000, "Largest XP reward a single commission may offer."
        ),
        "COMMISSION_MAX_KILLS": _i(
            0, 10000, "Largest kill count a single commission may require."
        ),
        "COMMISSION_MAX_OPEN_PER_POSTER": _i(
            1, 100, "Open commissions per poster, per quest type."
        ),
        "COMMISSION_COLLAB_CAP": _i(
            0, 10000, "Contributors allowed on one shared commission."
        ),
        "COMMISSION_TTL_SECONDS": _i(
            60, 31536000, "Seconds before an unfinished commission expires."
        ),
    },
    "leveling": {
        "XP_BASE": _i(1, 1000000, "XP required for level 2."),
        "XP_GROWTH": _f(1.0, 10.0, "XP curve multiplier per level."),
        "LEVEL_HP_PER_LEVEL": _i(0, 1000, "Max HP gained per level."),
        "LEVEL_ATK_PER_LEVEL": _i(0, 1000, "Attack gained per level."),
        "XP_LOSS_PCT": _pct("Share of level progress lost on death."),
    },
    "dungeon": {
        "DUNGEON_BASE_HP": _i(1, 100000, "Floor-1 monster HP."),
        "DUNGEON_BASE_ATK": _i(0, 10000, "Floor-1 monster attack."),
        "DUNGEON_HP_GROWTH": _f(0.0, 10.0, "HP multiplier per dungeon floor."),
        "DUNGEON_ATK_GROWTH": _f(0.0, 10.0, "Attack multiplier per dungeon floor."),
        "DUNGEON_MAX_FLOOR": _i(1, 1000, "Deepest floor a character may enter."),
        "WARDEN_HP": _i(1, 1000000, "Floor-boss HP."),
        "WARDEN_ATK": _i(0, 100000, "Floor-boss attack."),
        "WARDEN_GOLD": _i(0, 1000000, "Gold the floor boss drops."),
        "WARDEN_RESPAWN_SECONDS": _i(
            1, 86400, "Seconds before a slain floor boss returns."
        ),
        "PARTY_MAX_MEMBERS": _i(1, 100, "Characters allowed in one party."),
        "PARTY_INVITE_TTL_SECONDS": _i(
            10, 86400, "Seconds a party invite stays valid."
        ),
        "DUNGEON_REENTER_DELAY_SECONDS": _i(
            0, 86400, "Cooldown before re-entering the dungeon after a clear."
        ),
        "DUNGEON_REENTER_DELAY_GROWTH_SECONDS": _i(
            0, 86400, "Added re-entry cooldown per floor cleared."
        ),
        "DUNGEON_REENTER_DELAY_MAX_SECONDS": _i(
            0, 604800, "Ceiling on the re-entry cooldown."
        ),
        "DUNGEON_CLEAR_BASE_PTS": _i(0, 100000, "Score for clearing any floor."),
        "DUNGEON_CLEAR_PTS_PER_FLOOR": _i(0, 10000, "Extra score per floor cleared."),
        "DUNGEON_CLEAR_BASE_XP": _i(0, 100000, "XP for clearing any floor."),
        "DUNGEON_CLEAR_XP_PER_FLOOR": _i(0, 10000, "Extra XP per floor cleared."),
    },
    "quests": {
        "QUEST_GUARD_XP": _i(0, 100000, "Guard quest XP reward."),
        "QUEST_GUARD_GOLD": _i(0, 1000000, "Guard quest gold reward."),
        "QUEST_GUARD_POINTS": _i(0, 10000, "Guard quest score reward."),
        "QUEST_DELVER_XP": _i(0, 100000, "Delver quest XP reward."),
        "QUEST_DELVER_GOLD": _i(0, 1000000, "Delver quest gold reward."),
        "QUEST_DELVER_POINTS": _i(0, 10000, "Delver quest score reward."),
        "QUEST_DELVER_FLOORS": _i(1, 1000, "Dungeon floors a delver must clear."),
        "QUEST_REMEDY_XP": _i(0, 100000, "Remedy quest XP reward."),
        "QUEST_REMEDY_GOLD": _i(0, 1000000, "Remedy quest gold reward."),
        "QUEST_REMEDY_POINTS": _i(0, 10000, "Remedy quest score reward."),
        "QUEST_TONIC_XP": _i(0, 100000, "Tonic quest XP reward."),
        "QUEST_TONIC_GOLD": _i(0, 1000000, "Tonic quest gold reward."),
        "QUEST_TONIC_POINTS": _i(0, 10000, "Tonic quest score reward."),
    },
}

# ---------------------------------------------------------------------------
# ml/ml_config.json
# ---------------------------------------------------------------------------

ML_SCHEMA = {
    "reward_shaping": {
        "SOCIAL_PER_ALLY": _f(
            0.0,
            100.0,
            "Per-step reward per ally beyond self; reward for staying in a party.",
        ),
        "FORMATION_BONUS": _f(
            0.0, 100.0, "One-time bonus for joining or forming a party."
        ),
        "FORMATION_COOLDOWN_STEPS": _i(
            1,
            1000000,
            "Env steps between formation bonuses, so cycling cannot farm it.",
        ),
        "XP_LEVEL_BONUS": _f(
            0.0, 100.0, "Extra reward per level-up in xp reward mode."
        ),
        "ECON_INV_LAMBDA": _f(
            0.0, 100.0, "Weight of inventory-value delta in econ reward mode."
        ),
    },
    "curriculum": {
        "CURRICULUM_THRESHOLDS": _list(
            4, 0, 100000, "Min score to enter rats, dungeons, crafting, full."
        ),
    },
}

# File id -> (schema, label, relative path for the operator's benefit).
FILES = {
    "server": {
        "schema": SERVER_SCHEMA,
        "label": "server_config.json",
        "path": "server_config.json",
        "restart": "Restart the server to apply these values.",
    },
    "ml": {
        "schema": ML_SCHEMA,
        "label": "ml/ml_config.json",
        "path": "ml/ml_config.json",
        "restart": "Restart training to apply these values.",
    },
}

# ---------------------------------------------------------------------------
# Presets. Each is a value set, not a rule change: applying one stages values
# into the form for review. Nothing is written until the operator saves.
# ---------------------------------------------------------------------------

PRESETS = [
    {
        "name": "Balanced",
        "help": "Shipped defaults for the training-facing keys. Restores them to code defaults.",
        "values": {
            "server": {
                "scoring.DISCOVERY_POINTS": None,
                "scoring.DISCOVERY_XP": None,
                "scoring.DEATH_PENALTY": None,
            },
            "ml": {
                "reward_shaping.SOCIAL_PER_ALLY": None,
                "reward_shaping.FORMATION_BONUS": None,
                "reward_shaping.XP_LEVEL_BONUS": None,
                "curriculum.CURRICULUM_THRESHOLDS": None,
            },
        },
    },
    {
        "name": "Fast Training",
        "help": "Reward staying alive and grouped, and open the curriculum earlier, "
        "so an agent reaches the full world in fewer steps.",
        "values": {
            "server": {
                "scoring.DEATH_PENALTY": 2.0,
                "scoring.DEATH_GOLD_LOST_PCT": 0.0,
                "leveling.XP_LOSS_PCT": 2.0,
            },
            "ml": {
                "reward_shaping.SOCIAL_PER_ALLY": 0.15,
                "reward_shaping.FORMATION_BONUS": 1.0,
                "reward_shaping.FORMATION_COOLDOWN_STEPS": 200,
                "curriculum.CURRICULUM_THRESHOLDS": [0, 5, 15, 30],
            },
        },
    },
    {
        "name": "Economy Focus",
        "help": "Weight inventory value over raw score, and widen the market so "
        "agents can actually trade the goods they farm.",
        "values": {
            "server": {
                "economy.MARKET_ORDER_SLOTS_BASE": 6,
                "economy.MARKET_SLOT_PRICE_BASE": 25,
                "economy.BROKER_FEE_PCT": 2.0,
                "economy.RELIST_FEE_PCT": 2.0,
                "economy.INVENTORY_CAP": 40,
            },
            "ml": {
                "reward_shaping.ECON_INV_LAMBDA": 3.0,
            },
        },
    },
]


# Code defaults for ml_config.json keys, copied from ml/ml_env.py. Pinned by
# test, not by import: the game server must not import the ML env.
ML_DEFAULTS = {
    "SOCIAL_PER_ALLY": 0.05,
    "FORMATION_BONUS": 0.5,
    "FORMATION_COOLDOWN_STEPS": 500,
    "XP_LEVEL_BONUS": 5.0,
    "ECON_INV_LAMBDA": 1.0,
    "CURRICULUM_THRESHOLDS": [0, 10, 30, 60],
}


def field_spec(file_id, section, key):
    """Spec for one key, or None when the key is not in the schema."""
    entry = FILES.get(file_id, {}).get("schema", {}).get(section, {})
    return entry.get(key)


def sections(file_id):
    """Ordered [(section, [key, ...])] for a file."""
    return [
        (section, list(keys))
        for section, keys in FILES.get(file_id, {}).get("schema", {}).items()
    ]


def validate(file_id, section, key, raw):
    """Coerce and bounds-check one submitted value.

    Returns (value, None) or (None, error). Deliberately mirrors what the
    loaders can actually accept: an int-typed global rejects 2.5 rather than
    silently truncating it through `type(default)(value)`, because a form that
    shows 2.5 in the box and saves 2 in the file is worse than a refusal.
    """
    spec = field_spec(file_id, section, key)
    if spec is None:
        return None, f"unknown key {section}.{key}"
    kind = spec["type"]

    if kind == "list":
        text = str(raw).strip()
        if text.startswith("[") and text.endswith("]"):
            text = text[1:-1]
        parts = [p.strip() for p in text.split(",") if p.strip()]
        if len(parts) != spec["count"]:
            return None, f"{key} needs exactly {spec['count']} numbers"
        out = []
        for part in parts:
            # float() first, then narrow: int("1e2") is a ValueError, so
            # testing for a decimal point and otherwise calling int() would
            # reject a number JSON itself accepts.
            try:
                number = float(part)
            except ValueError:
                return None, f"{key}: {part!r} is not a number"
            out.append(int(number) if number.is_integer() else number)
        for item in out:
            if not spec["min"] <= item <= spec["max"]:
                return None, f"{key}: {item} outside {spec['min']}-{spec['max']}"
        return out, None

    text = str(raw).strip()
    if not text:
        return None, f"{key} is empty"
    if kind == "int":
        try:
            value = int(text, 10)
        except ValueError:
            return None, f"{key} must be a whole number"
    else:
        try:
            value = float(text)
        except ValueError:
            return None, f"{key} must be a number"
    if not spec["min"] <= value <= spec["max"]:
        return None, f"{key}: {value} outside {spec['min']}-{spec['max']}"
    return value, None


def format_value(spec, value):
    """Render a value the way the file should spell it.

    Floats keep a decimal point (2.0, not 2) because the loader coerces with
    `type(default)(value)` and an int-typed default given 2.0 stays 2.0, but a
    float-typed default given a bare 2 would round-trip through JSON as an int
    and make the file disagree with its own type.
    """
    if spec["type"] == "list":
        return (
            "["
            + ", ".join((f"{v:g}" if isinstance(v, float) else str(v)) for v in value)
            + "]"
        )
    if spec["type"] == "float":
        # %g rather than repr: repr(2.0) is "2.0" but repr(1e-07) is
        # "1e-07", which json.load reads back fine and json.dump would not
        # have written. %g keeps small values in the 0.0000001 spelling a
        # human editing the file would use, and json.load accepts either.
        text = f"{float(value):g}"
        return text if ("." in text or "e" in text) else text + ".0"
    return str(int(value))
