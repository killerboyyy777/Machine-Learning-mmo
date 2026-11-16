"""Arena probe contract (#656).

The probe is the gate's only evidence that a match ran, so the test drives
it twice: once against a server that keeps the arena's promises, and once
against a build that pays a winner and scores a PvP death as a civ death.
The second run has to reach the real check primitives and fail them --
otherwise the new checks would be decoration.
"""

import asyncio
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import ml.arena_probe as probe  # noqa: E402
import ml.balance as bal  # noqa: E402

MATCH_ID = 9001
ROUND_HP = 20
HIT = 10


def _result(cmd, events, error=None, state=None):
    return {
        "ok": error is None,
        "command": cmd,
        "events": events,
        "state": state or {},
        "error": error,
        "truncated": False,
    }


class FakeBridge:
    def __init__(self, server, url, name, token=None):
        self.server = server
        self.url = url
        self.name = name
        self.token = token
        self.cache = {"room": None, "stats": None}

    async def connect(self):
        self.server.attach(self)

    async def close(self):
        self.server.detach(self)

    def _ok(self, events):
        return _result("", events, state={"name": self.name})

    def _error(self, text):
        return _result("", [{"type": "error", "text": text}], error=text)

    async def command(self, cmd, params=None, *, expect=(), timeout=None):
        params = params or {}
        if self.name not in self.server.bridges:
            return self._error("not connected")
        if cmd == "arena":
            return self.server.arena(self, params)
        if cmd == "attack":
            return self.server.attack(self, params.get("target"))
        if cmd == "stats":
            self.server.sync(self.name)
            return self._ok([{"type": "stats", **self.cache["stats"]}])
        return self._error(f"Unknown command: {cmd}")


class FakeServer:
    """Just enough arena to exercise the probe, including the promises."""

    def __init__(self, pay_gold=0.0, civ_death=False, rate_variance=False, draw=False):
        self.pay_gold = pay_gold
        self.civ_death = civ_death
        self.rate_variance = rate_variance
        self.draw = draw
        self.verb_absent = False
        self.bridges = {}
        self.queue = []
        self.match = None
        self.match_count = 0
        self.history = []
        self.rooms = {}
        self.gold = {}
        self.deaths = {}
        self.ratings = {}

    # --- plumbing ------------------------------------------------------
    def bridge(self, url, name, token=None):
        return FakeBridge(self, url, name, token)

    def attach(self, bridge):
        self.bridges[bridge.name] = bridge
        self.rooms.setdefault(bridge.name, probe.START_ROOM)
        self.gold.setdefault(bridge.name, 0)
        self.deaths.setdefault(
            bridge.name, {"deaths": 0, "civ_deaths": 0, "wild_deaths": 0}
        )
        self.sync(bridge.name)

    def detach(self, bridge):
        self.bridges.pop(bridge.name, None)
        if bridge.name in self.queue:
            self.queue.remove(bridge.name)

    def sync(self, name):
        bridge = self.bridges.get(name)
        if bridge is None:
            return
        bridge.cache["room"] = {"id": self.rooms.get(name, probe.START_ROOM)}
        bridge.cache["stats"] = {
            "gold": self.gold.get(name, 0),
            "inv": [],
            "hp": ROUND_HP,
            "max_hp": ROUND_HP,
        }

    # --- arena ---------------------------------------------------------
    def _teams(self):
        return (self.match or {}).get("teams") or {"team1": [], "team2": []}

    def _in_match(self, name):
        return name in self._teams()["team1"] + self._teams()["team2"]

    def status(self, name):
        match = self.match
        teams = self._teams()
        team = 1 if name in teams["team1"] else 2 if name in teams["team2"] else 0
        return {
            "type": "arena",
            "in_match": bool(match) and self._in_match(name),
            "queued": "2v2" if name in self.queue else None,
            "match_id": match["id"] if match and self._in_match(name) else None,
            "state": match["state"] if match and self._in_match(name) else None,
            "round": match["round"] if match else 0,
            "scores": dict(match["scores"]) if match else None,
            "team": team,
            "teams": {"team1": list(teams["team1"]), "team2": list(teams["team2"])},
            "rating": self.ratings.get(name, 1000.0),
        }

    def arena(self, bridge, params):
        action = params.get("action")
        if self.verb_absent:
            return bridge._error("Unknown command: arena")
        if action == "status":
            return _result("", [self.status(bridge.name)], state={})
        if action == "enter":
            if bridge.name in self.queue or self._in_match(bridge.name):
                return bridge._error("You are already queued.")
            self.queue.append(bridge.name)
            if len(self.queue) >= 4:
                self.start_match()
            return _result(
                "",
                [{"type": "message", "text": "You queue for 2v2 (1 waiting)."}],
                state={},
            )
        if action == "leave":
            if bridge.name in self.queue:
                self.queue.remove(bridge.name)
            return _result(
                "", [{"type": "message", "text": "You leave the arena."}], state={}
            )
        return bridge._error("Unknown arena action.")

    def start_match(self):
        names = list(self.queue)
        del self.queue[:4]
        self.match_count += 1
        self.match = {
            "id": MATCH_ID,
            "teams": {"team1": names[:2], "team2": names[2:]},
            "scores": {"team1": 0, "team2": 0},
            "state": "fighting",
            "round": 1,
            "hp": {name: ROUND_HP for name in names},
        }
        for name in names:
            self.rooms[name] = f"a_{MATCH_ID}"
            self.sync(name)

    def attack(self, bridge, target):
        if not self.match or not self._in_match(bridge.name):
            return bridge._error("You are not in a fight.")
        if target not in self.match["teams"]["team2"]:
            return bridge._error("There is nobody by that name here.")
        if self.draw:
            self.finish(draw=True)
            return _result("", [{"type": "combat", "text": "Time expires."}], state={})
        self.match["hp"][target] -= HIT
        both_down = all(
            self.match["hp"][name] <= 0 for name in self.match["teams"]["team2"]
        )
        if both_down:
            self.end_round()
        return _result(
            "", [{"type": "combat", "text": f"You hit {target} for {HIT}."}], state={}
        )

    def end_round(self):
        self.match["scores"]["team1"] += 1
        if self.match["scores"]["team1"] >= 2:
            self.finish()
            return
        self.match["round"] += 1
        for name in self.match["hp"]:
            self.match["hp"][name] = ROUND_HP

    def finish(self, draw=False):
        match = self.match
        delta = 16.0 if not (self.rate_variance and self.match_count > 1) else 17.0
        if draw:
            delta = 0.0
        deltas = {}
        for name in match["teams"]["team1"]:
            deltas[name] = delta
            self.ratings[name] = round(self.ratings.get(name, 1000.0) + delta, 2)
            if self.pay_gold:
                self.gold[name] = self.gold.get(name, 0) + self.pay_gold
        for name in match["teams"]["team2"]:
            deltas[name] = -delta
            self.ratings[name] = round(self.ratings.get(name, 1000.0) - delta, 2)
            if self.civ_death:
                self.deaths[name]["civ_deaths"] += 1
        self.history.append(
            {
                "match_id": match["id"],
                "team_size": 2,
                "winner": 0 if draw else 1,
                "scores": {"team1": 0, "team2": 0} if draw else dict(match["scores"]),
                "teams": {
                    "team1": list(match["teams"]["team1"]),
                    "team2": list(match["teams"]["team2"]),
                },
                "deltas": deltas,
            }
        )
        for name in list(match["hp"]):
            self.rooms[name] = probe.START_ROOM
            self.sync(name)
        self.match = None

    def state(self, http_url=None):
        names = sorted(set(self.rooms) | set(self.gold))
        return {
            "players": [
                {
                    "name": name,
                    "level": 1,
                    "deaths": self.deaths.get(name, {}).get("deaths", 0),
                    "civ_deaths": self.deaths.get(name, {}).get("civ_deaths", 0),
                    "wild_deaths": self.deaths.get(name, {}).get("wild_deaths", 0),
                    "gold": self.gold.get(name, 0),
                    "companion": None,
                }
                for name in names
            ],
            "arena": {
                "version": 1,
                "queues": {"1v1": 0, "2v2": len(self.queue)},
                "live": [],
                "ladder": [],
                "ladder_size": len(self.ratings),
                "ranked_size": 0,
                "provisional_size": len(self.ratings),
                "min_matches": 5,
                "recent": [dict(row) for row in self.history[-10:]],
                "rating_clamps": 0,
            },
        }


def _run(server, seed=700):
    saved = probe.SNAPSHOT_WAIT_S
    probe.SNAPSHOT_WAIT_S = 0.0
    try:
        return asyncio.run(
            probe.run_arena_probe(
                "ws://fake",
                "http://fake",
                seed,
                server.state,
                stamp="7654321",
                bridge_factory=server.bridge,
            )
        )
    finally:
        probe.SNAPSHOT_WAIT_S = saved


def _measurements(report):
    clean = dict(report)
    clean.pop("state", None)
    sample = {
        "t": 1.0,
        "alive": 4,
        "episodes": 0,
        "state": {
            "arena": (report.get("state") or {}).get("arena") or {},
            "arena_probe": clean,
        },
        "tables": {},
    }
    return {"derived": bal.derive([sample], telemetry=bal._telemetry([sample]))}


def test_identities():
    names = probe.probe_identities(700, stamp="1234567")
    assert len(names) == 4 and len(set(names)) == 4
    assert all(3 <= len(n) <= 24 and n.isascii() for n in names)
    assert names != probe.probe_identities(701, stamp="1234567")
    assert names != probe.probe_identities(700, stamp="1234568")
    assert "--" not in "".join(names)
    tokens = {probe.probe_token(n) for n in names}
    assert len(tokens) == 4 and all(len(t) == 64 for t in tokens)
    print("ARENA_IDENTITIES_OK")


def test_pure_checks():
    assert probe.rating_conserved({"a": 16.0, "b": -16.0})
    assert not probe.rating_conserved({"a": 16.0, "b": -15.0})
    assert not probe.rating_conserved({})
    assert not probe.arena_verb_absent("You are already queued.")
    assert probe.arena_verb_absent("Unknown command: arena")
    same = {
        "team_size": 2,
        "winner": 1,
        "scores": {"team1": 2, "team2": 0},
        "deltas": {"a": 16.0, "b": 16.0, "c": -16.0, "d": -16.0},
    }
    other = dict(same, deltas={"a": 17.0, "b": 17.0, "c": -17.0, "d": -17.0})
    assert probe.match_fingerprint(same) == probe.match_fingerprint(same)
    assert probe.match_fingerprint(same) != probe.match_fingerprint(other)
    print("ARENA_PURE_OK")


def test_honest_server():
    server = FakeServer()
    report = _run(server)
    assert report.get("error") is None, report
    assert report["skipped"] is False
    assert report["pairs_matched"] is True, report
    assert report["matches_completed"] == 2, report
    assert report["rating_conserved"] is True, report
    assert report["rating_delta_sum"] == 0.0, report
    assert report["gold_delta"] == 0.0, report
    assert report["items_delta"] == 0
    assert report["civ_deaths_delta"] == 0 and report["wild_deaths_delta"] == 0
    assert report["deaths_delta"] == 0
    assert report["determinism"] is True, report
    assert report["fingerprint"] == report["fingerprint_second"]
    assert report["steps"] > 0
    assert not report["problems"], report["problems"]
    assert server.queue == [] and server.match is None

    measurements = _measurements(report)
    thresholds = bal.DEFAULT_THRESHOLDS
    for name, expected in (
        ("arena.pairs_matched", True),
        ("arena.rating_conserved", True),
        ("arena.determinism", True),
        ("arena.gold_delta", 0.0),
        ("arena.items_delta", 0),
        ("arena.civ_deaths_delta", 0),
    ):
        assert bal._metric(measurements, thresholds, name) == expected, name
    assert bal._metric(measurements, thresholds, "arena.matches_completed") == 2
    print("ARENA_HONEST_OK")


def test_leaky_server_fails_the_checks():
    server = FakeServer(pay_gold=5.0, civ_death=True)
    report = _run(server)
    assert report["matches_completed"] == 2, report
    # Movement, per fighter: two winners paid 5 each, twice over.
    assert report["gold_delta"] == 20.0, report
    assert report["civ_deaths_delta"] == 4, report
    assert any("gold" in p for p in report["problems"]), report["problems"]
    assert any("civ_deaths" in p for p in report["problems"]), report["problems"]

    measurements = _measurements(report)
    thresholds = bal.DEFAULT_THRESHOLDS
    gold = bal._metric(measurements, thresholds, "arena.gold_delta")
    civ = bal._metric(measurements, thresholds, "arena.civ_deaths_delta")
    assert gold == 20.0 and civ == 4, (gold, civ)
    assert bal._passes(gold, "<=", thresholds["arena"]["max_gold_delta"]) is False
    assert bal._passes(civ, "<=", thresholds["arena"]["max_deaths"]) is False
    print("ARENA_LEAK_FAILS_OK")


def test_rating_drift_fails_determinism():
    server = FakeServer(rate_variance=True)
    report = _run(server)
    assert report["matches_completed"] == 2, report
    assert report["determinism"] is False, report
    assert report["fingerprint"] != report["fingerprint_second"]
    assert any("rated differently" in p for p in report["problems"])
    print("ARENA_DRIFT_OK")


def test_draw_does_not_count_as_a_match():
    server = FakeServer(draw=True)
    report = _run(server)
    assert report["pairs_matched"] is True, report
    assert report["matches_completed"] == 0, report
    assert report["winner"] == 0
    assert any("without a winner" in p for p in report["problems"]), report["problems"]
    measurements = _measurements(report)
    assert (
        bal._metric(measurements, bal.DEFAULT_THRESHOLDS, "arena.matches_completed")
        == 0
    )
    assert bal._passes(0, ">=", 2) is False
    print("ARENA_DRAW_OK")


def test_absent_verb_skips_with_reason():
    server = FakeServer()
    server.verb_absent = True
    report = _run(server)
    assert report["skipped"] is True, report
    assert report["skip_reason"] == probe.SKIP_REASON
    assert report["matches_completed"] == 0
    assert report.get("error") is None
    assert report["state"], "a skip still leaves a snapshot for the gate"
    measurements = _measurements(report)
    assert (
        bal._metric(measurements, bal.DEFAULT_THRESHOLDS, "arena.pairs_matched")
        is False
    )
    assert (
        bal._passes(False, "is", True) is False
    ), "an unmeasured arena must fail, not pass"
    print("ARENA_SKIP_OK")


def main():
    test_identities()
    test_pure_checks()
    test_honest_server()
    test_leaky_server_fails_the_checks()
    test_rating_drift_fails_determinism()
    test_draw_does_not_count_as_a_match()
    test_absent_verb_skips_with_reason()
    print("ALL_ARENA_PROBE_OK")


if __name__ == "__main__":
    main()
