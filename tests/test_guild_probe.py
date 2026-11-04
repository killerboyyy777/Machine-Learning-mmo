"""Guild probe tests for issue #589 (no live server, no agent farm).

Run from the repo root:  python tests/test_guild_probe.py
Covers the probe's pure helpers (names, conservation math, reply parsing),
the router check that decides the clean #588 skip, and the guard that turns
that skip into a gate FAILURE while this build ships the guild verb.
"""

import asyncio
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import ml.balance as bal
import ml.guild_probe as probe

ROOMS = {
    "town_square": {"west": "market"},
    "market": {"west": "artisan_row"},
    "artisan_row": {"north": "smithy", "south": "guild_hall"},
    "smithy": {"south": "artisan_row"},
    "guild_hall": {"north": "artisan_row"},
}


class FakeServer:
    """Just enough of the guild surface for the probe's script."""

    def __init__(self, leak=False):
        self.leak = leak
        self.gold = {}
        self.room = {}
        self.vault = 0
        self.vault_items = []
        self.audit = []
        self.members = 0
        self.in_total = 0
        self.out_total = 0
        self.calls = []

    def state(self, http_url=None):
        return {
            "guilds": {
                "count": 1 if self.members else 0,
                "members_total": self.members,
                "vault_gold_total": self.vault,
                "vault_items_total": len(self.vault_items),
                "audit_entries": len(self.audit),
                "audit_entries_written": len(self.audit),
                "vault_gold_in_total": self.in_total,
                "vault_gold_out_total": self.out_total,
            }
        }

    def bridge(self, url, name, token=None):
        self.gold.setdefault(name, 10)
        self.room.setdefault(name, "town_square")
        return FakeBridge(self, name)


class FakeBridge:
    def __init__(self, server, name):
        self.server = server
        self.name = name
        self.cache = {"room": None, "stats": None}

    async def connect(self):
        self.cache["room"] = {"id": self.server.room[self.name]}
        self.cache["stats"] = {"gold": self.server.gold[self.name]}

    async def close(self):
        return None

    def _message(self, text):
        return {
            "ok": True,
            "events": [{"type": "message", "text": text}],
            "state": {
                "room": self.server.room[self.name],
                "gold": self.server.gold[self.name],
            },
        }

    def _error(self, text):
        return {
            "ok": False,
            "error": text,
            "events": [{"type": "error", "text": text}],
            "state": {
                "room": self.server.room[self.name],
                "gold": self.server.gold[self.name],
            },
        }

    async def command(self, cmd, params=None, *, expect=(), timeout=None):
        params = params or {}
        server = self.server
        server.calls.append((cmd, params.get("action") or params.get("dir") or ""))
        if cmd == "move":
            dest = ROOMS[server.room[self.name]].get(params.get("dir"))
            if dest is None:
                return self._error("You can't go that way.")
            server.room[self.name] = dest
            return {
                "ok": True,
                "events": [{"type": "room", "id": dest}],
                "state": {"room": dest, "gold": server.gold[self.name]},
            }
        if cmd == "gather":
            if server.room[self.name] != "smithy":
                return self._error("No available gathering node matches that here.")
            return self._message("You gather 2x Iron Ore.")
        if cmd == "stats":
            return {
                "ok": True,
                "events": [{"type": "stats"}],
                "state": {
                    "room": server.room[self.name],
                    "gold": server.gold[self.name],
                },
            }
        if cmd != "guild":
            return self._error(f"Unknown command '{cmd}'.")
        action = params.get("action")
        if action == "info":
            return self._error("You are not in a guild.")
        if action == "create":
            if server.room[self.name] != "guild_hall":
                return self._error("Guild charters are signed at the hall.")
            server.members = 1
            server.audit.append(f"{self.name} founded the guild.")
            return self._message(f"Guild {params['name']} founded. You are its leader.")
        if action == "invite":
            server.audit.append(f"{self.name} invited {params['target']}.")
            return self._message(f"Invitation sent to {params['target']}.")
        if action == "join":
            server.members = 2
            server.audit.append(f"{self.name} joined the guild.")
            return self._message("You join the guild as a member.")
        if action == "vault":
            op = params.get("op")
            if "gold" in params:
                amount = int(params["gold"])
                if op == "deposit":
                    if not server.leak:
                        server.gold[self.name] -= amount
                    server.vault += amount
                    server.in_total += amount
                    server.audit.append(f"{self.name} deposited {amount} gold.")
                else:
                    server.vault -= amount
                    server.gold[self.name] += amount
                    server.out_total += amount
                    server.audit.append(f"{self.name} withdrew {amount} gold.")
                return self._message(
                    f"You {op} {amount} gold. The vault holds {server.vault} gold."
                )
            item = params["item"]
            if op == "deposit":
                server.vault_items.append(item)
                server.audit.append(f"{self.name} deposited {item}.")
            else:
                server.vault_items.remove(item)
                server.audit.append(f"{self.name} withdrew {item}.")
            return self._message(
                f"You {op} {item}. The vault holds {len(server.vault_items)} item(s)."
            )
        if action == "audit":
            return self._message(
                "Guild audit, newest first:\n" + "\n".join(reversed(server.audit))
            )
        if action == "disband":
            server.members = 0
            server.audit.append(f"{self.name} disbanded the guild (0 gold, 0 item(s)).")
            return self._message("Probe Guild is disbanded.")
        return self._error(f"Unknown guild action '{action}'.")


def _run(server, seed=700):
    old_wait = probe.SNAPSHOT_WAIT_S
    probe.SNAPSHOT_WAIT_S = 0.0
    try:
        return asyncio.run(
            probe.run_guild_probe(
                "ws://fake",
                "http://fake",
                seed,
                server.state,
                stamp="1234567",
                bridge_factory=server.bridge,
            )
        )
    finally:
        probe.SNAPSHOT_WAIT_S = old_wait


def test_probe_identities():
    names = probe.probe_identities(700, "1234567")
    assert names["leader"].startswith("GProbe700")
    # Character names cap at MAX_NAME_LEN (24) and guild names at 3-24.
    for key in ("leader", "member", "guild"):
        assert 3 <= len(names[key]) <= 24, (key, names[key])
        assert names[key] == names[key].strip(), names[key]
        assert all(32 <= ord(ch) < 127 for ch in names[key]), names[key]
    assert names["leader"] != names["member"]
    assert names["guild"] not in (names["leader"], names["member"])
    # A repeated run must not reuse a name: a score entry would withhold the
    # starting purse and the disband tombstone would refuse the charter.
    assert probe.probe_identities(700, "1234567") == names
    assert probe.probe_identities(701, "1234567") != names
    assert probe.probe_identities(700, "7654321") != names
    print("PROBE_IDENTITIES_OK")


def test_conservation_helpers():
    assert probe.vault_drift(6, 2, 4) == 0
    assert probe.vault_drift(6, 2, 3) == 1
    assert probe.vault_drift(6, 2, 5) == -1
    assert probe.vault_drift(None, 2, 4) is None
    assert probe.vault_holding("You deposit 6 gold. The vault holds 6 gold.") == 6
    assert probe.vault_holding("You withdraw 2 gold. The vault holds 4 gold.") == 4
    assert (
        probe.vault_holding("You deposit Iron Ore. The vault holds 1 item(s).") is None
    )
    print("PROBE_CONSERVATION_OK")


def test_router_gate():
    # The one text the probe treats as "guild verb absent".
    assert probe.guild_verb_absent("Unknown command 'guild'.")
    assert not probe.guild_verb_absent(
        "You are not in a guild. Use guild info <name> to look one up."
    )
    assert not probe.guild_verb_absent("")
    print("PROBE_ROUTER_GATE_OK")


def test_guild_verb_ships_so_the_skip_must_fail():
    import server as srv

    assert srv.HANDLERS.get("guild") is srv.cmd_guild

    inbox = []

    async def fake_send(player, payload):
        inbox.append(payload)

    async def ask():
        old_send = srv.send
        srv.send = fake_send
        try:
            await srv.cmd_guild(srv.Player(), {"action": "info"})
        finally:
            srv.send = old_send

    asyncio.run(ask())
    text = inbox[-1].get("text", "")
    # The verb is routed, so the probe's absent-verb branch cannot fire and
    # the #588 skip stays unreachable against this build.
    assert not probe.guild_verb_absent(text), text
    assert "not in a guild" in text, text

    # And if a probe skipped anyway, the covered scenario fails the gate.
    skip_reason = f"{bal.GUILD_SURFACE_ISSUE} {probe.SKIP_REASON}"
    derived = {"guilds": {"count": None, "skip_reason": skip_reason}}
    report = bal.evaluate_run(
        {"meta": {}, "samples": [], "derived": derived},
        bal.load_thresholds(profile="gate"),
        "gate",
        700,
    )
    assert "coverage.guilds" in report["summary"]["failing"], report["summary"]
    row = next(r for r in report["scenarios"] if r["name"] == "guilds")
    assert row["status"] == "skipped", row
    assert "#588" in row["reason"], row
    print("GUILD_SKIP_GUARD_OK")


def test_probe_script_exercises_the_vault():
    server = FakeServer()
    report = _run(server)
    actions = [action for cmd, action in server.calls if cmd == "guild"]
    # The charter script is fixed: a changed step count means a changed gate.
    assert actions == [
        "info",
        "create",
        "invite",
        "join",
        "vault",
        "vault",
        "vault",
        "vault",
        "audit",
        "vault",
        "disband",
    ], actions
    assert ("gather", "") in server.calls
    assert report["steps"] == 15
    assert report["skipped"] == "" and report["error"] == ""
    assert report["problems"] == [], report["problems"]
    assert report["gold_conserved"] is True
    assert report["item"] == "Iron Ore"
    # Gold went in, came back out, and the leader's purse is whole again.
    assert server.gold[report["leader"]] == 10
    assert server.in_total == 6 and server.out_total == 6
    assert server.vault == 0 and server.vault_items == []
    # The snapshot the harness derives the rows from: taken while the vault
    # still held gold and the item, never after the disband.
    peak = report["state"]["guilds"]
    assert peak["count"] == 1 and peak["members_total"] == 2
    assert peak["vault_items_total"] == 1
    assert (
        peak["vault_gold_in_total"] - peak["vault_gold_out_total"]
        == peak["vault_gold_total"]
    )
    assert peak["audit_entries"] == 6
    print("PROBE_SCRIPT_OK")


def test_probe_flags_a_purse_leak():
    # A vault deposit that leaves the purse untouched breaks purse+vault
    # conservation; the probe must report it rather than call the run clean.
    server = FakeServer(leak=True)
    report = _run(server)
    assert report["gold_conserved"] is False
    assert any("gold in play" in problem for problem in report["problems"]), report[
        "problems"
    ]
    print("PROBE_LEAK_OK")


def main():
    test_probe_identities()
    test_conservation_helpers()
    test_router_gate()
    test_probe_script_exercises_the_vault()
    test_probe_flags_a_purse_leak()
    test_guild_verb_ships_so_the_skip_must_fail()
    print("GUILD_PROBE_OK")


if __name__ == "__main__":
    main()
