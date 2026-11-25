"""Deterministic arena probe for the release gate (#656).

The gate cannot tell a working arena from one that never pairs, so this
drives a real 2v2 through the ordinary verbs and reads the result from
both sides: each fighter's own frames for the lifecycle, and the server
snapshot for the ledger. Two matches run back to back with fresh
fighters -- the second has to agree with the first, which is what makes
``arena.determinism`` a measurement instead of an assumption.

A match moves no value (server.py ARENA_BLOCKED_COMMANDS), so the probe
promises gold and items are where they started and no death counter
moved. That promise is the point: a build that pays a winner or scores a
PvP kill as a civ death fails the gate here, not in a soak.

When the build does not route the arena verb the probe skips with a
reason the gate prints, as the guild probe does.
"""

import asyncio
import hashlib
import json
import time
import uuid

from harness.bridge import GameBridge

START_ROOM = "town_square"
SNAPSHOT_WAIT_S = 1.2
MATCH_START_TIMEOUT_S = 40.0
MATCH_TIMEOUT_S = 150.0
HISTORY_WAIT_S = 6.0
POLL_S = 0.5
FIGHT_POLL_S = 0.3
SECOND_SNAPSHOT_TRIES = 3
UNKNOWN_VERB_TEXT = "unknown command"
MATCHES = 2

SKIP_REASON = "arena not in this build: the server does not route the arena verb"


class ProbeError(RuntimeError):
    """A promise the arena makes about a match, broken."""


def probe_token(name):
    """Stable per-name credential, as the guild probe uses."""
    return hashlib.sha256(f"arena-probe:{name}".encode()).hexdigest()


def probe_identities(seed, stamp=None, tag=""):
    """Four fresh fighter names, unique per run.

    Fixed names would land on a persisted rating row and make the delta
    ledger depend on earlier runs; the stamp keeps every probe's fighters
    provisional for their first match, which is what the conservation
    arithmetic below assumes.
    """
    # A uuid4 stamp keeps two runs in the same second from sharing rating rows.
    stamp = uuid.uuid4().hex[:8] if stamp is None else stamp
    suffix = f"{int(seed)}{stamp}"
    return [f"Arena{tag}{i}{suffix}" for i in range(1, 5)]


def arena_verb_absent(text):
    return UNKNOWN_VERB_TEXT in (text or "").lower()


def match_fingerprint(row):
    """Structural identity of a completed match.

    Damage rolls come from the global RNG, so HP and round length vary.
    What must not vary is the outcome the server records: the size, the
    winner, the score and the landed rating deltas.
    """
    row = row or {}
    deltas = sorted(round(float(v), 2) for v in (row.get("deltas") or {}).values())
    payload = {
        "team_size": row.get("team_size"),
        "winner": row.get("winner"),
        "scores": row.get("scores"),
        "deltas": deltas,
    }
    blob = json.dumps(payload, sort_keys=True, default=str)
    return hashlib.sha256(blob.encode()).hexdigest()[:16]


def rating_conserved(deltas, tolerance=0.05):
    """A rating change is a transfer: the deltas must sum to zero.

    A floor clamp is the only legitimate exception, and it cannot happen
    to fresh fighters at 1000; the probe reports it as a problem instead.
    """
    if not deltas:
        return False
    return abs(sum(float(v) for v in deltas.values())) <= tolerance


def _frame(result, ftype):
    for event in result.get("events") or []:
        if event.get("type") == ftype:
            return event
    return {}


def _teams(payload):
    teams = payload.get("teams") or {}
    one = teams.get("team1") or teams.get(1) or teams.get("1") or []
    two = teams.get("team2") or teams.get(2) or teams.get("2") or []
    return list(one), list(two)


def _player_rows(snapshot):
    rows = (snapshot or {}).get("players") or []
    return {row.get("name"): row for row in rows if isinstance(row, dict)}


def _counter(row, key):
    try:
        return int((row or {}).get(key) or 0)
    except (TypeError, ValueError):
        return 0


def _gold(bridge):
    stats = bridge.cache.get("stats") or {}
    try:
        return float(stats.get("gold") or 0.0)
    except (TypeError, ValueError):
        return 0.0


def _items(bridge):
    stats = bridge.cache.get("stats") or {}
    return len(stats.get("inv") or [])


def _history_row(snapshot, names):
    """The history row for a match whose teams are exactly these names."""
    wanted = set(names)
    for row in reversed((snapshot.get("arena") or {}).get("recent") or []):
        teams = row.get("teams") or {}
        listed = set(teams.get("team1") or []) | set(teams.get("team2") or [])
        if listed == wanted:
            return row
    return None


async def _connect(bridge, tries=4):
    last = None
    for _ in range(tries):
        try:
            await bridge.connect()
            return
        except Exception as exc:  # noqa: BLE001 - retried, then surfaced
            last = exc
            with _quiet():
                await bridge.close()
            await asyncio.sleep(2.0)
    raise ProbeError(f"connect {bridge.name}: {last}")


class _quiet:
    """Swallow a best-effort close, as the guild probe does."""

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return True


async def _status(bridge):
    result = await bridge.command(
        "arena", {"action": "status"}, expect=("arena", "error")
    )
    if not result.get("ok"):
        raise ProbeError(f"status {bridge.name}: {result.get('error')}")
    return _frame(result, "arena")


async def _snapshot(http_url, fetch_state):
    await asyncio.sleep(SNAPSHOT_WAIT_S)
    return await asyncio.to_thread(fetch_state, http_url)


async def _wait_paired(bridges, report):
    deadline = time.monotonic() + MATCH_START_TIMEOUT_S
    while time.monotonic() < deadline:
        ids = []
        for bridge in bridges:
            status = await _status(bridge)
            match_id = status.get("match_id")
            if not match_id:
                room = (bridge.cache.get("room") or {}).get("id") or ""
                match_id = room if room.startswith("a_") else None
            ids.append(match_id)
        if all(ids) and len(set(ids)) == 1:
            report["match_id"] = ids[0]
            return True
        await asyncio.sleep(POLL_S)
    return False


async def _fight(bridges, names, report):
    """Team 1 attacks, team 2 stands still: the outcome is then fixed."""
    deadline = time.monotonic() + MATCH_TIMEOUT_S
    attackers = [b for b in bridges if b.name in names[:2]]
    while time.monotonic() < deadline:
        status = await _status(bridges[0])
        room = (bridges[0].cache.get("room") or {}).get("id") or ""
        if (
            not status.get("in_match")
            and not status.get("state")
            and not room.startswith("a_")
        ):
            return True
        if status.get("state") == "fighting":
            one, two = _teams(status)
            enemies = two or names[2:]
            for index, bridge in enumerate(attackers):
                if one and bridge.name not in one:
                    continue
                target = enemies[index % len(enemies)]
                await bridge.command(
                    "attack", {"target": target}, expect=("combat", "message", "error")
                )
                report["steps"] += 1
        await asyncio.sleep(FIGHT_POLL_S)
    return False


async def _run_one_match(factory, url, http_url, names, fetch_state, report):
    """One scripted 2v2. Returns the ledger entry the caller aggregates."""
    entry = {
        "paired": False,
        "completed": False,
        "winner": None,
        "scores": None,
        "deltas": {},
        "fingerprint": None,
        "gold_delta": 0.0,
        "items_delta": 0,
        "deaths_delta": 0,
        "civ_deaths_delta": 0,
        "wild_deaths_delta": 0,
        "problems": [],
    }
    bridges = [factory(url, name, probe_token(name)) for name in names]
    before_rows = {}
    try:
        for bridge in bridges:
            await _connect(bridge)
            report["steps"] += 1
        snapshot = await asyncio.to_thread(fetch_state, http_url)
        before_rows = _player_rows(snapshot)
        gold_before = {b.name: _gold(b) for b in bridges}
        items_before = {b.name: _items(b) for b in bridges}

        for bridge in bridges:
            result = await bridge.command(
                "arena", {"action": "enter", "mode": "2v2"}, expect=("message", "error")
            )
            if not result.get("ok"):
                raise ProbeError(f"enter {bridge.name}: {result.get('error')}")
            report["steps"] += 1

        entry["paired"] = await _wait_paired(bridges, report)
        if not entry["paired"]:
            entry["problems"].append(f"{len(names)} fighters never met in one match")
            return entry

        if not await _fight(bridges, names, report):
            entry["problems"].append(f"match {report.get('match_id')} never finished")
            return entry

        await asyncio.sleep(SNAPSHOT_WAIT_S)
        snapshot = {}
        row = None
        deadline = time.monotonic() + HISTORY_WAIT_S
        while True:
            snapshot = await asyncio.to_thread(fetch_state, http_url)
            row = _history_row(snapshot, names)
            if row or time.monotonic() >= deadline:
                break
            await asyncio.sleep(POLL_S)

        for bridge in bridges:
            await bridge.command("stats", expect=("stats",))
        after_rows = _player_rows(snapshot)

        for bridge in bridges:
            gold_after = _gold(bridge)
            moved_gold = abs(gold_after - gold_before[bridge.name])
            # Movement, not net: two fighters swapping gold is the same broken
            # promise as one of them gaining it, and a signed sum would hide it.
            entry["gold_delta"] = round(entry["gold_delta"] + moved_gold, 2)
            if moved_gold > 0.001:
                entry["problems"].append(
                    f"{bridge.name}: gold {gold_before[bridge.name]:.2f} -> "
                    f"{gold_after:.2f} across a match"
                )
            items_after = _items(bridge)
            entry["items_delta"] += abs(items_after - items_before[bridge.name])
            if items_after != items_before[bridge.name]:
                entry["problems"].append(
                    f"{bridge.name}: pack {items_before[bridge.name]} -> {items_after}"
                )
            for key, field in (
                ("deaths", "deaths_delta"),
                ("civ_deaths", "civ_deaths_delta"),
                ("wild_deaths", "wild_deaths_delta"),
            ):
                moved = _counter(after_rows.get(bridge.name), key) - _counter(
                    before_rows.get(bridge.name), key
                )
                entry[field] += moved
                if moved:
                    entry["problems"].append(
                        f"{bridge.name}: {key} +{moved} across a match"
                    )

        if not row:
            entry["problems"].append("no history row for the match (nothing to rate)")
            return entry

        entry["completed"] = True
        entry["winner"] = row.get("winner")
        entry["scores"] = row.get("scores")
        entry["deltas"] = dict(row.get("deltas") or {})
        entry["fingerprint"] = match_fingerprint(row)
        return entry
    finally:
        for bridge in bridges:
            with _quiet():
                await bridge.command(
                    "arena", {"action": "leave"}, expect=("message", "error")
                )
            with _quiet():
                await bridge.close()


def _decided(entry):
    """A finished match only counts when it produced a winner.

    Two idle sides can spend three rounds and draw; that is the shape a
    broken attack path leaves behind, and it must not score as a match.
    """
    return bool(entry.get("completed")) and entry.get("winner") in (1, 2)


async def run_arena_probe(
    url, http_url, seed, fetch_state, stamp=None, bridge_factory=None
):
    """Run two scripted matches and report what the arena promised.

    Crash-free on purpose: a broken arena must show up as failed checks
    with a reason, not as a traceback that hides the rest of the gate.
    """
    factory = bridge_factory or GameBridge
    report = {
        "fighters": [],
        "match_id": None,
        "steps": 0,
        "pairs_matched": False,
        "matches_completed": 0,
        "winner": None,
        "scores": None,
        "rating_delta_sum": 0.0,
        "rating_conserved": False,
        "gold_delta": 0.0,
        "items_delta": 0,
        "deaths_delta": 0,
        "civ_deaths_delta": 0,
        "wild_deaths_delta": 0,
        "determinism": None,
        "fingerprint": None,
        "fingerprint_second": None,
        "problems": [],
        "skipped": False,
        "skip_reason": None,
        "error": None,
        "state": None,
    }
    names = probe_identities(seed, stamp)
    second = probe_identities(seed, stamp, tag="B")
    report["fighters"] = names + second
    probe_bridge = factory(url, names[0], probe_token(names[0]))
    try:
        await _connect(probe_bridge)
        status = await probe_bridge.command(
            "arena", {"action": "status"}, expect=("arena", "error")
        )
        if not status.get("ok") and arena_verb_absent(status.get("error")):
            report["skipped"] = True
            report["skip_reason"] = SKIP_REASON
            report["state"] = await _snapshot(http_url, fetch_state)
            return report
        report["steps"] += 1
    finally:
        with _quiet():
            await probe_bridge.close()

    first = await _run_one_match(factory, url, http_url, names, fetch_state, report)
    report["problems"].extend(first["problems"])
    report["pairs_matched"] = bool(first["paired"])
    report["matches_completed"] = 1 if _decided(first) else 0
    report["winner"] = first["winner"]
    report["scores"] = first["scores"]
    report["fingerprint"] = first["fingerprint"]
    report["rating_delta_sum"] = round(sum(first["deltas"].values()), 2)
    report["rating_conserved"] = rating_conserved(first["deltas"]) and bool(
        first["deltas"]
    )

    second_entry = await _run_one_match(
        factory, url, http_url, second, fetch_state, report
    )
    report["problems"].extend(second_entry["problems"])
    report["pairs_matched"] = report["pairs_matched"] and bool(second_entry["paired"])
    report["matches_completed"] += 1 if _decided(second_entry) else 0
    report["fingerprint_second"] = second_entry["fingerprint"]
    if first["fingerprint"] and second_entry["fingerprint"]:
        report["determinism"] = first["fingerprint"] == second_entry["fingerprint"]
        report["rating_delta_sum"] = round(
            report["rating_delta_sum"] + sum(second_entry["deltas"].values()), 2
        )
        report["rating_conserved"] = report["rating_conserved"] and rating_conserved(
            second_entry["deltas"]
        )
    if not report["determinism"]:
        report["problems"].append(
            "two identical scripted matches rated differently "
            f"({report['fingerprint']} vs {report['fingerprint_second']})"
        )
    for entry in (first, second_entry):
        if not entry["completed"]:
            continue
        if not _decided(entry):
            report["problems"].append(
                f"match {report.get('match_id')} ended without a winner "
                f"({entry['scores']}); only team 1 attacked"
            )
        elif entry["winner"] != 1:
            report["problems"].append(
                f"match {report.get('match_id')} was won by team {entry['winner']} "
                "while team 2 never attacked"
            )

    for entry in (first, second_entry):
        report["gold_delta"] = round(report["gold_delta"] + entry["gold_delta"], 2)
        report["items_delta"] += entry["items_delta"]
        report["deaths_delta"] += entry["deaths_delta"]
        report["civ_deaths_delta"] += entry["civ_deaths_delta"]
        report["wild_deaths_delta"] += entry["wild_deaths_delta"]

    report["state"] = await _snapshot(http_url, fetch_state)
    return report
