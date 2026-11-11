"""Release-gate guild probe (#589): drive the real guild command surface.

The gate cannot score guilds from bot play -- emergent guilds are #591's work
(v0.13.0), not a bot behavior -- so each seed runs one scripted charter: found
a guild, invite and admit a second character, move gold and one gathered item
through the vault in both directions, read the audit log and disband, checking
that gold is conserved between purses and vault after every step.

The probe hands the harness the /api/state snapshot taken while the vault still
held gold and an item; the gate rows are derived from that snapshot, not from
this module's bookkeeping, so the numbers stay the server's own telemetry.
"""

import asyncio
import contextlib
import hashlib
import os
import re
import time

from harness.bridge import GameBridge

START_ROOM = "town_square"
# Fixed routes instead of a pathfinder: the world is small and a moved exit
# must fail loudly rather than silently measure the wrong room.
SMITHY_ROUTE = (
    ("market", "west"),
    ("artisan_row", "west"),
    ("smithy", "north"),
)
HALL_ROUTE = (("artisan_row", "south"), ("guild_hall", "south"))
GATHER_ITEMS = ("iron ore", "scrap iron")
GATHER_TRIES = 12
GATHER_RETRY_S = 2.5
SNAPSHOT_WAIT_S = 1.2
DEPOSIT_GOLD = 6
WITHDRAW_GOLD = 2
AUDIT_SHOW = 20
UNKNOWN_VERB_TEXT = "Unknown command"
VAULT_HOLD_RE = re.compile(r"The vault holds (\d+) gold")
GATHER_TEXT_RE = re.compile(r"You gather \d+x (.+?)\.")

SKIP_REASON = "guilds not in this build: the server does not route the guild verb"


class ProbeError(RuntimeError):
    """One probe step did not produce the state the guild model promises."""


def probe_token(name):
    """Stable per-character token, so a reconnect is not read as a takeover."""
    return hashlib.sha256(f"guild-probe:{name}".encode()).hexdigest()


def probe_identities(seed, stamp=None):
    """Throwaway character and guild names, unique per invocation.

    A fixed name would land on a persisted score entry (so the second run gets
    no starting purse) and on the 30-day disband tombstone, so the stamp is
    what makes the probe repeatable against one server file.
    """
    if stamp is None:
        stamp = f"{os.getpid() % 1000:03d}{int(time.time()) % 10000:04d}"
    suffix = f"{int(seed) % 1000:03d}{stamp}"
    return {
        "leader": f"GProbe{suffix}",
        "member": f"GProbe{suffix}b",
        "guild": f"Probe Guild {suffix}",
    }


def guild_verb_absent(error_text):
    """True when the reply is the router's answer for an unregistered verb."""
    return UNKNOWN_VERB_TEXT.lower() in str(error_text or "").lower()


def vault_drift(gold_in, gold_out, held):
    """vault_gold_in_total - vault_gold_out_total - vault_gold_total."""
    if gold_in is None or gold_out is None or held is None:
        return None
    return round(float(gold_in) - float(gold_out) - float(held), 3)


def vault_holding(text):
    """Gold the vault reports after a vault op, or None when it did not say."""
    match = VAULT_HOLD_RE.search(str(text or ""))
    return int(match.group(1)) if match else None


def _texts(result):
    return [str(event.get("text") or "") for event in result.get("events") or []]


async def _connect(bridge, tries=4):
    for attempt in range(1, tries + 1):
        try:
            await bridge.connect()
            return
        except Exception as exc:
            if attempt >= tries:
                raise ProbeError(f"login {bridge.name}: {exc}") from exc
            # A crowded starting room is the usual cause; the next try lands
            # after a nearby agent moved on.
            await bridge.close()
            await asyncio.sleep(2.0)


async def _command(bridge, step, cmd, params=None, expect=("message",)):
    result = await bridge.command(cmd, params, expect=set(expect))
    if not result.get("ok"):
        raise ProbeError(f"{step}: {result.get('error')}")
    return result


async def _gold(bridge):
    result = await _command(bridge, "stats", "stats", expect=("stats",))
    gold = (result.get("state") or {}).get("gold")
    if gold is None:
        raise ProbeError("stats: the stats frame carried no gold")
    return int(gold)


async def _walk(bridge, route):
    for room, direction in route:
        result = await _command(
            bridge, f"move {direction}", "move", {"dir": direction}, ("room",)
        )
        landed = (result.get("state") or {}).get("room")
        if landed != room:
            raise ProbeError(f"move {direction} landed in {landed!r}, wanted {room!r}")


async def _gather(bridge):
    for _ in range(GATHER_TRIES):
        for item in GATHER_ITEMS:
            result = await bridge.command("gather", {"node": item}, expect={"stats"})
            if not result.get("ok"):
                continue
            for text in _texts(result):
                match = GATHER_TEXT_RE.search(text)
                if match:
                    return match.group(1)
            raise ProbeError("gather: the reply named no item")
        # Both nodes were taken by agents; they respawn in about half a minute.
        await asyncio.sleep(GATHER_RETRY_S)
    raise ProbeError("gather: no smithy node freed up")


async def _snapshot(http_url, fetch_state):
    # /api/state is served from a snapshot rebuilt once a second, so a read
    # taken right after a mutation can predate it.
    await asyncio.sleep(SNAPSHOT_WAIT_S)
    return await asyncio.to_thread(fetch_state, http_url)


async def _disband_quietly(bridge):
    # Best effort: a live charter left behind would show up as a second guild
    # in the next seed's snapshot.
    with contextlib.suppress(Exception):
        await bridge.command("guild", {"action": "disband"}, expect={"message"})


async def run_guild_probe(
    url, http_url, seed, fetch_state, stamp=None, bridge_factory=None
):
    """Run one charter and return the peak snapshot plus the probe's ledger."""
    names = probe_identities(seed, stamp)
    factory = bridge_factory or GameBridge
    leader = factory(url, names["leader"], token=probe_token(names["leader"]))
    member = factory(url, names["member"], token=probe_token(names["member"]))
    report = {
        "guild": names["guild"],
        "leader": names["leader"],
        "member": names["member"],
        "item": "",
        "steps": 0,
        "gold_conserved": False,
        "problems": [],
        "skipped": "",
        "error": "",
        "state": None,
    }
    problems = report["problems"]
    created = False
    disbanded = False
    try:
        await _connect(leader)
        await _connect(member)
        opened = await leader.command("guild", {"action": "info"}, expect={"guild"})
        if not opened.get("ok") and guild_verb_absent(opened.get("error")):
            report["skipped"] = SKIP_REASON
            report["state"] = await _snapshot(http_url, fetch_state)
            return report

        start = (leader.cache.get("room") or {}).get("id")
        if start != START_ROOM:
            raise ProbeError(f"probe logged in at {start!r}, wanted {START_ROOM!r}")

        report["steps"] += 1
        await _walk(leader, SMITHY_ROUTE)
        report["steps"] += 1
        item = await _gather(leader)
        report["item"] = item
        report["steps"] += 1
        await _walk(leader, HALL_ROUTE)

        report["steps"] += 1
        purse = await _gold(leader)
        member_gold = await _gold(member)
        if purse <= DEPOSIT_GOLD:
            raise ProbeError(f"purse {purse} cannot fund a {DEPOSIT_GOLD} gold deposit")
        total = purse + member_gold

        report["steps"] += 1
        founded = await _command(
            leader, "create", "guild", {"action": "create", "name": names["guild"]}
        )
        created = True
        if "founded" not in "\n".join(_texts(founded)):
            raise ProbeError("create: no confirmation in the reply")

        report["steps"] += 1
        await _command(
            leader, "invite", "guild", {"action": "invite", "target": names["member"]}
        )

        report["steps"] += 1
        joined = await _command(member, "join", "guild", {"action": "join"})
        if "You join" not in "\n".join(_texts(joined)):
            raise ProbeError("join: no confirmation in the reply")

        async def move_gold(step, op, amount, want_purse, want_vault):
            result = await _command(
                leader, step, "guild", {"action": "vault", "op": op, "gold": amount}
            )
            vault = vault_holding("\n".join(_texts(result)))
            after = await _gold(leader)
            if vault is None:
                problems.append(f"{step}: the reply did not report the vault total")
                vault = want_vault
            if after + vault + member_gold != total:
                problems.append(
                    f"{step}: purse {after} + vault {vault} + member "
                    f"{member_gold} != {total} gold in play"
                )
            if (after, vault) != (want_purse, want_vault):
                problems.append(
                    f"{step}: purse/vault {after}/{vault} != {want_purse}/{want_vault}"
                )

        report["steps"] += 1
        await move_gold(
            "deposit gold", "deposit", DEPOSIT_GOLD, purse - DEPOSIT_GOLD, DEPOSIT_GOLD
        )
        report["steps"] += 1
        await move_gold(
            "withdraw gold",
            "withdraw",
            WITHDRAW_GOLD,
            purse - DEPOSIT_GOLD + WITHDRAW_GOLD,
            DEPOSIT_GOLD - WITHDRAW_GOLD,
        )

        report["steps"] += 1
        deposited = await _command(
            leader,
            "deposit item",
            "guild",
            {"action": "vault", "op": "deposit", "item": item},
        )
        if "item(s)" not in "\n".join(_texts(deposited)):
            problems.append("deposit item: no vault total in the reply")

        # Read the peak while the vault still holds gold and the item: the
        # gate rows for members, holdings and movement come from this sample.
        report["state"] = await _snapshot(http_url, fetch_state)

        report["steps"] += 1
        withdrawn = await _command(
            leader,
            "withdraw item",
            "guild",
            {"action": "vault", "op": "withdraw", "item": item},
        )
        if "item(s)" not in "\n".join(_texts(withdrawn)):
            problems.append("withdraw item: no vault total in the reply")

        report["steps"] += 1
        log = await _command(
            leader, "audit", "guild", {"action": "audit", "n": AUDIT_SHOW}
        )
        audit_text = "\n".join(_texts(log))
        for phrase in (
            "Guild audit, newest first:",
            "founded the guild",
            f"invited {names['member']}",
            "joined the guild",
            f"deposited {DEPOSIT_GOLD} gold",
            f"withdrew {WITHDRAW_GOLD} gold",
            f"deposited {item}",
            f"withdrew {item}",
        ):
            if phrase not in audit_text:
                problems.append(f"audit: missing {phrase!r}")

        report["steps"] += 1
        await move_gold(
            "withdraw rest", "withdraw", DEPOSIT_GOLD - WITHDRAW_GOLD, purse, 0
        )

        report["steps"] += 1
        closed = await _command(leader, "disband", "guild", {"action": "disband"})
        disbanded = True
        if "disbanded" not in "\n".join(_texts(closed)):
            raise ProbeError("disband: no confirmation in the reply")
        if await _gold(leader) != purse:
            problems.append("disband: the leader's purse did not match its start")

        # The identity must survive the teardown, or every later run reads a
        # vault that leaked gold into (or out of) nothing.
        final = await _snapshot(http_url, fetch_state)
        guilds = final.get("guilds") or {}
        drift = vault_drift(
            guilds.get("vault_gold_in_total"),
            guilds.get("vault_gold_out_total"),
            guilds.get("vault_gold_total"),
        )
        if drift is None:
            problems.append("after disband: the snapshot carried no vault totals")
        elif drift != 0:
            problems.append(f"after disband: vault identity drifted by {drift}")
        if guilds.get("count") or guilds.get("vault_items_total"):
            problems.append(f"after disband: guild world not empty ({guilds})")
        report["steps"] += 1
    finally:
        if created and not disbanded:
            await _disband_quietly(leader)
        for bridge in (member, leader):
            with contextlib.suppress(Exception):
                await bridge.close()
    report["gold_conserved"] = not problems
    return report
