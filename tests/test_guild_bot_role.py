"""Guild bot role tests (#591), no live server needed.

Run from the repo root:  python tests/test_guild_bot_role.py

Covers the action-space append, the command mapping for every guild action,
the prose the env learns guild state from, and the founder's plan order.
"""

import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import server as srv
from ml.ml_env import (
    ACTIONS,
    GUILD_RESERVE_GOLD,
    GUILD_WITHDRAW_STEP,
    TextMMOEnv,
)
from ml.plugins.scripted import GuildFounderPlugin

GUILD_ACTIONS = [
    "guild_create",
    "guild_invite",
    "guild_join",
    "guild_deposit",
    "guild_withdraw",
    "guild_disband",
    "guild_leave",
    "guild_info",
]


def fresh_env(name="GuildBot"):
    env = TextMMOEnv(name)
    env._state["hp"] = 20
    env._state["max_hp"] = 20
    env._mask_cache = None
    return env


def hostile_name():
    return next(n["name"] for n in srv.WORLD["npcs"].values() if n.get("hostile"))


def selected(policy, env):
    return ACTIONS[policy.select(env)]


# --- the guild verbs are appended, so pre-#591 action indices never shift ---
assert ACTIONS[-len(GUILD_ACTIONS) :] == GUILD_ACTIONS, ACTIONS[-len(GUILD_ACTIONS) :]
print("APPENDED_OK", len(ACTIONS))

# --- deposit banks the surplus and keeps the reserve ---
env = fresh_env()
env._state["room_id"] = srv.GUILD_ROOM
env._state["guild_name"] = "Test Company"
env._state["gold"] = 200
cmd = env._action_to_cmd("guild_deposit")
assert cmd == {
    "cmd": "guild",
    "action": "vault",
    "op": "deposit",
    "gold": 200 - GUILD_RESERVE_GOLD,
}, cmd
print("DEPOSIT_SURPLUS_OK", cmd["gold"])

env._state["gold"] = GUILD_RESERVE_GOLD
assert env._action_to_cmd("guild_deposit") is None
print("NO_SURPLUS_OK")

# --- withdraw only when short, and only when the vault can cover the step ---
env._state["guild_rank"] = "leader"
env._state["gold"] = 10
env._state["guild_vault_gold"] = GUILD_WITHDRAW_STEP
cmd = env._action_to_cmd("guild_withdraw")
assert cmd == {
    "cmd": "guild",
    "action": "vault",
    "op": "withdraw",
    "gold": GUILD_WITHDRAW_STEP,
}, cmd
env._state["guild_vault_gold"] = GUILD_WITHDRAW_STEP - 1
assert env._action_to_cmd("guild_withdraw") is None
env._state["guild_vault_gold"] = 500
env._state["gold"] = GUILD_RESERVE_GOLD + 50
assert env._action_to_cmd("guild_withdraw") is None
print("WITHDRAW_SHORTFALL_OK")

# --- founding: refused twice, unique name per bot, printable and short ---
env._state["guild_name"] = ""
cmd = env._action_to_cmd("guild_create")
assert cmd == {"cmd": "guild", "action": "create", "name": cmd["name"]}
assert 3 <= len(cmd["name"]) <= 24, cmd["name"]
other = fresh_env("OtherBot")
other._state["room_id"] = srv.GUILD_ROOM
other_cmd = other._action_to_cmd("guild_create")
assert other_cmd["name"] != cmd["name"], (cmd["name"], other_cmd["name"])
env._state["guild_name"] = "Test Company"
assert env._action_to_cmd("guild_create") is None
print("CREATE_ONCE_OK", cmd["name"])

# --- join needs a pending invitation, and joins the named guild ---
env._state["guild_name"] = ""
env._state["guild_invite"] = "Wolf Pact"
env._state["guild_invite_at"] = time.time()
assert env._action_to_cmd("guild_join") == {
    "cmd": "guild",
    "action": "join",
    "name": "Wolf Pact",
}
env._state["guild_invite"] = ""
assert env._action_to_cmd("guild_join") is None
env._state["guild_name"] = "Test Company"
env._state["guild_invite"] = "Wolf Pact"
assert env._action_to_cmd("guild_join") is None
print("JOIN_INVITE_ONLY_OK")

# --- disband is leader-only ---
env._state["guild_name"] = "Test Company"
env._state["room_id"] = srv.GUILD_ROOM
env._state["guild_rank"] = "member"
assert env._action_to_cmd("guild_disband") is None
env._state["guild_rank"] = "leader"
assert env._action_to_cmd("guild_disband") == {"cmd": "guild", "action": "disband"}
print("DISBAND_LEADER_OK")

# --- the env learns guild state from the server's prose ---
env = fresh_env()
env._apply_event(
    {"type": "message", "text": "Guild Iron Pact founded. You are its leader."}
)
assert env._state["guild_name"] == "Iron Pact", env._state["guild_name"]
assert env._state["guild_rank"] == "leader"
env._apply_event(
    {
        "type": "message",
        "text": "WolfPaw invites you to join Iron Pact. Send guild join to accept.",
    }
)
assert env._state["guild_invite"] == "Iron Pact"
env._apply_event({"type": "message", "text": "You join Iron Pact as a member."})
assert env._state["guild_rank"] == "member"
assert env._state["guild_invite"] == ""
env._apply_event({"type": "message", "text": "Iron Pact vault: 90 gold, 2 item(s)."})
assert env._state["guild_vault_gold"] == 90, env._state["guild_vault_gold"]
env._apply_event(
    {"type": "message", "text": "You deposit 40 gold. The vault holds 130 gold."}
)
assert env._state["guild_vault_gold"] == 130, env._state["guild_vault_gold"]
env._apply_event({"type": "message", "text": "Iron Pact is disbanded."})
assert env._state["guild_name"] == ""
print("GUILD_PROSE_OK")

# --- structured guild event carries membership and the vault ---
env._apply_event(
    {
        "type": "guild",
        "name": "Iron Pact",
        "leader": env.name,
        "member_count": 2,
        "vault_gold": 55,
    }
)
assert env._state["guild_rank"] == "leader"
assert env._state["guild_members"] == 2
assert env._state["guild_vault_gold"] == 55
print("GUILD_EVENT_OK")

# --- plan order: poor bots earn, rich bots walk to the hall ---
env = fresh_env()
env._state["npc_names"] = [hostile_name()]
env._state["gold"] = 0
assert selected(GuildFounderPlugin(), env) == "attack"
print("EARN_FIRST_OK")

env = fresh_env()
env._state["gold"] = GuildFounderPlugin.FOUND_GOLD + 50
env._state["room_id"] = "market"
env._state["exits"] = ["west", "south"]
act = selected(GuildFounderPlugin(), env)
assert act.startswith("move_"), act
print("NAVIGATES_TO_HALL_OK", act)

# --- in the hall it founds, then invites a peer ---
env = fresh_env()
env._state["gold"] = GuildFounderPlugin.FOUND_GOLD + 50
env._state["room_id"] = "guild_hall"
env._state["exits"] = ["artisan_row"]
assert selected(GuildFounderPlugin(), env) == "guild_create"
print("FOUNDS_IN_HALL_OK")

env._state["guild_name"] = "GuildBot Company"
env._state["guild_rank"] = "leader"
env._state["guild_members"] = 1
env._state["player_names"] = ["GuildBot", "WolfPaw"]
env._mask_cache = None
assert selected(GuildFounderPlugin(), env) == "guild_invite"
print("INVITES_PEER_OK")

# --- with a peer in the roster it banks instead of inviting ---
env._state["guild_members"] = 2
env._mask_cache = None
assert selected(GuildFounderPlugin(), env) == "guild_deposit"
print("BANKS_SURPLUS_OK")

# --- a one-member guild disbands once it has been alone too long ---
policy = GuildFounderPlugin()
env = fresh_env()
env._state["gold"] = GuildFounderPlugin.FOUND_GOLD
env._state["room_id"] = "guild_hall"
env._state["exits"] = ["artisan_row"]
env._state["guild_name"] = "Lonely Company"
env._state["guild_rank"] = "leader"
env._state["guild_members"] = 1
env._state["gold"] = GUILD_RESERVE_GOLD
policy._busy_at = 0
policy._idle_guild = "Lonely Company"
policy._invite_at = GuildFounderPlugin.IDLE_STEPS + 1
env._step_count = GuildFounderPlugin.IDLE_STEPS + 1
assert selected(policy, env) == "guild_disband"
print("IDLE_DISBAND_OK")


# --- inviting and withdrawing need officer rank or better ---
env = fresh_env()
env._state["guild_name"] = "Test Company"
env._state["room_id"] = srv.GUILD_ROOM
env._state["guild_rank"] = "member"
env._state["player_names"] = ["GuildBot", "WolfPaw"]
env._state["gold"] = 10
env._state["guild_vault_gold"] = 200
assert env._action_to_cmd("guild_invite") is None
assert env._action_to_cmd("guild_withdraw") is None
env._state["guild_rank"] = "officer"
assert env._action_to_cmd("guild_invite") == {
    "cmd": "guild",
    "action": "invite",
    "target": "WolfPaw",
}
assert env._action_to_cmd("guild_withdraw")["gold"] == GUILD_WITHDRAW_STEP
print("RANK_GATE_OK")

# --- an expired invitation is not offered ---
env = fresh_env()
env._state["guild_invite"] = "Wolf Pact"
env._state["guild_invite_at"] = time.time() - 301
assert env._action_to_cmd("guild_join") is None
print("INVITE_EXPIRY_OK")

# --- a disbanded name is tombstoned, so the next founding moves on ---
env = fresh_env()
env._state["room_id"] = srv.GUILD_ROOM
first = env._action_to_cmd("guild_create")["name"]
env._apply_event({"type": "message", "text": "Guild X founded. You are its leader."})
env._apply_event({"type": "message", "text": "X is disbanded."})
env._state["room_id"] = srv.GUILD_ROOM
second = env._action_to_cmd("guild_create")["name"]
assert first != second, (first, second)
print("NAME_CYCLE_OK", first, second)

# --- guild_info is always available; it is how a member keeps up ---
env = fresh_env()
assert env._action_to_cmd("guild_info") == {"cmd": "guild", "action": "info"}
print("INFO_ALWAYS_OK")

# --- a disband that told nobody is caught by the next info refusal ---
env = fresh_env()
env._state["guild_name"] = "Ghost Pact"
env._apply_event({"type": "error", "text": "You are not in a guild."})
assert env._state["guild_name"] == ""
print("SILENT_DISBAND_OK")


# --- the other ways a guild ends, and the refusal that names it ---
env = fresh_env()
env._state["guild_name"] = "Iron Pact"
env._apply_event(
    {"type": "message", "text": "Iron Pact had no members left and is dissolved."}
)
assert env._state["guild_name"] == ""
env._apply_event({"type": "message", "text": "WolfPaw removes you from Iron Pact."})
assert env._state["guild_name"] == ""
env._apply_event(
    {
        "type": "error",
        "text": "The name Iron Pact was disbanded recently. Pick another.",
    }
)
before = env._state["guild_cycle"]
assert before == 3, before  # two clears above, plus the refusal
env._apply_event(
    {"type": "error", "text": "You are already in Iron Pact. Leave it first."}
)
assert env._state["guild_name"] == "Iron Pact", env._state["guild_name"]
print("GUILD_ENDS_OK")

# --- the info poll is periodic, not every step ---
policy = GuildFounderPlugin()
env = fresh_env()
env._state["gold"] = 200
env._state["room_id"] = "guild_hall"
env._state["exits"] = ["artisan_row"]
env._state["guild_name"] = "Poll Co"
env._state["guild_rank"] = "leader"
env._state["guild_members"] = 2
policy._invite_at = 10**6
env._step_count = 1
assert selected(policy, env) == "guild_deposit"
env._step_count = 1 + GuildFounderPlugin.INFO_EVERY
assert selected(policy, env) == "guild_info"
print("INFO_POLL_OK")

print("GUILD_BOT_ROLE_OK")
