"""Security regressions (no live server needed).

Run from the repo root:  python tests/test_security.py

Pins the login name-takeover fix: a name that already served a starting
purse counts as an existing account and must not be re-claimed by a
login without a valid token. Also pins the login name-length / charset
bound so a tokenless login cannot mint unbounded SCORES keys pre-auth.
"""

import asyncio
import contextlib
import io
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import server as srv

INBOX = []


async def fake_send(player, payload):
    INBOX.append(payload if isinstance(payload, dict) else json.loads(payload))


class FakeWS:
    remote_address = ("127.0.0.1", 1234)


def texts():
    return [m.get("text", "") for m in INBOX if isinstance(m, dict)]


def unplayer(p):
    srv.remove_member(p)
    srv.players.pop(p.id, None)


def drop_entry(name):
    srv.SCORES.pop(name.lower(), None)
    srv.name_owners.pop(name.lower(), None)


async def test_json_hardening():
    class HostileWS:
        remote_address = ("127.0.0.1", 1234)

        def __init__(self, payloads):
            self.payloads = payloads

        def __aiter__(self):
            async def frames():
                for raw in self.payloads:
                    yield raw

            return frames()

        async def send(self, payload):
            raise AssertionError("outbound writer must stay disabled")

    async def noop_writer(player):
        return None

    huge = b'{"cmd":"noop","n":' + b"9" * 5000 + b"}"
    deep = b'{"cmd":"noop","n":' + b"[" * 100 + b"0" + b"]" * 100 + b"}"
    deeper = b'{"cmd":"noop","n":' + b"[" * 2000 + b"0" + b"]" * 2000 + b"}"
    hostile = [
        huge,
        deep,
        deeper,
        b'{"cmd":"noop","n":NaN}',
        b'{"cmd":"noop","n":Infinity}',
        b'{"cmd":"noop","n":-Infinity}',
        b'{"cmd":"noop","n":1e9999}',
    ]
    payloads = hostile + [b'{"cmd":"noop"}']
    ws = HostileWS(payloads)
    real_writer = srv._outbound_writer
    INBOX.clear()
    output = io.StringIO()
    try:
        srv._outbound_writer = noop_writer
        with contextlib.redirect_stdout(output):
            await srv.handle_connection(ws)
    except Exception as exc:
        raise AssertionError(f"hostile JSON escaped: {exc!r}") from exc
    finally:
        srv._outbound_writer = real_writer
    messages = list(INBOX)
    assert len(messages) == len(payloads), messages
    for raw, msg in zip(hostile, messages):
        assert msg.get("type") == "error" and msg.get("text") == "invalid JSON", (
            raw[:60],
            msg,
        )
    assert messages[-1].get("text") == "You must 'login' first.", messages[-1]
    assert "Traceback" not in output.getvalue(), output.getvalue()


async def main():
    real_send = srv.send
    srv.send = fake_send
    try:
        # --- 1. claimed name cannot be taken over without a token -----------
        # Owner took a token-less first login on an open server, served the
        # starting purse, then went away. Attacker relogs with the same name and
        # no token: the login must be rejected, not treated as unowned.
        victim = srv.Player(ws=FakeWS(), id=92001, name="", logged_in=False)
        await srv.cmd_login(victim, {"name": "TakeoverVictim"})
        assert (
            srv.get_score_entry("TakeoverVictim").get("starting_purse_claimed") is True
        )
        assert victim.gold == srv.STARTING_GOLD
        srv.remove_member(victim)
        srv.players.pop(92001, None)
        srv.name_owners.pop("takeovervictim", None)

        attacker = srv.Player(ws=FakeWS(), id=92002, name="", logged_in=False)
        INBOX.clear()
        await srv.cmd_login(attacker, {"name": "TakeoverVictim"})
        assert not attacker.logged_in, attacker.logged_in
        assert any(m.get("type") == "error" for m in INBOX), INBOX
        assert "protected by a token" in texts()[-1], texts()
        unplayer(attacker)
        drop_entry("TakeoverVictim")

        # --- 2. tokened owner relog and wrong-token probe -------------------
        owner = srv.Player(ws=FakeWS(), id=92003, name="", logged_in=False)
        await srv.cmd_login(owner, {"name": "Tokened", "token": "owner-secret"})
        assert owner.gold == srv.STARTING_GOLD
        srv.remove_member(owner)
        srv.players.pop(92003, None)
        srv.name_owners.pop("tokened", None)

        relogin = srv.Player(ws=FakeWS(), id=92004, name="", logged_in=False)
        await srv.cmd_login(relogin, {"name": "Tokened", "token": "owner-secret"})
        assert relogin.logged_in, "tokened relogin must stay valid"
        assert relogin.gold == 0, relogin.gold
        srv.remove_member(relogin)
        srv.players.pop(92004, None)
        srv.name_owners.pop("tokened", None)

        bad = srv.Player(ws=FakeWS(), id=92005, name="", logged_in=False)
        INBOX.clear()
        await srv.cmd_login(bad, {"name": "Tokened", "token": "evil"})
        assert not bad.logged_in, bad.logged_in
        assert any(m.get("type") == "error" for m in INBOX), INBOX
        unplayer(bad)
        drop_entry("Tokened")

        # --- 3. fresh tokenless login (casual / bot-farm) still works --------
        casual = srv.Player(ws=FakeWS(), id=92006, name="", logged_in=False)
        INBOX.clear()
        await srv.cmd_login(casual, {"name": "CasualBot"})
        assert casual.logged_in, "fresh tokenless login must stay allowed"
        assert casual.gold == srv.STARTING_GOLD, casual.gold
        unplayer(casual)
        drop_entry("CasualBot")

        # --- 4. login name bounds --------------------------------------------
        # Bot-style handles stay valid; oversized or control-char names are
        # rejected before get_score_entry()/log_command can mint SCORES keys,
        # so a tokenless login cannot disk-fill scores.json with attacker
        # chosen keys.
        mlbot = srv.Player(ws=FakeWS(), id=92007, name="", logged_in=False)
        INBOX.clear()
        await srv.cmd_login(mlbot, {"name": "MLAgent"})
        assert mlbot.logged_in, "MLAgent must pass login validation"
        unplayer(mlbot)
        drop_entry("MLAgent")

        drop_entry("role_0")
        rolebot = srv.Player(ws=FakeWS(), id=92008, name="", logged_in=False)
        await srv.cmd_login(rolebot, {"name": "role_0"})
        assert rolebot.logged_in, "role_0 must pass login validation"
        unplayer(rolebot)
        drop_entry("role_0")

        long_name = "A" * (srv.MAX_NAME_LEN + 1)
        bad_long = srv.Player(ws=FakeWS(), id=92009, name="", logged_in=False)
        INBOX.clear()
        await srv.cmd_login(bad_long, {"name": long_name})
        assert not bad_long.logged_in, bad_long.logged_in
        assert any(m.get("type") == "error" for m in INBOX), INBOX
        assert (
            long_name.lower() not in srv.SCORES
        ), "rejected name must not mint a score entry"

        bad_ctrl = srv.Player(ws=FakeWS(), id=92010, name="", logged_in=False)
        INBOX.clear()
        await srv.cmd_login(bad_ctrl, {"name": "Bad\x01Ctrl"})
        assert not bad_ctrl.logged_in, bad_ctrl.logged_in
        assert any(m.get("type") == "error" for m in INBOX), INBOX
        assert (
            "bad\x01ctrl" not in srv.SCORES
        ), "control-char name must not mint a score entry"

        # --- 5. rejected fresh names must not mint or refresh accounts ----
        # With token auth enabled, a stream of valid-looking but unauthenticated
        # names must not grow SCORES or make dormant accounts eligible for LRU
        # eviction.
        old_required = srv.AUTH_TOKEN_REQUIRED
        old_prune = srv._last_score_prune
        victim_key = "dormantloginvictim"
        flood_names = [f"RejectedFlood{i}" for i in range(25)]
        try:
            srv.AUTH_TOKEN_REQUIRED = True
            victim_entry = srv.get_score_entry("DormantLoginVictim")
            victim_entry["auth_token"] = srv.hashlib.sha256(
                b"victim-secret"
            ).hexdigest()
            victim_entry["starting_purse_claimed"] = True
            victim_entry["last_seen"] = 1
            srv.mark_scores_dirty()
            for i, flood_name in enumerate(flood_names):
                rejected = srv.Player(
                    ws=FakeWS(), id=92100 + i, name="", logged_in=False
                )
                INBOX.clear()
                await srv.cmd_login(rejected, {"name": flood_name})
                assert not rejected.logged_in
                assert flood_name.lower() not in srv.SCORES, (
                    "rejected login created a score entry",
                    flood_name,
                )
                unplayer(rejected)
            assert (
                srv.SCORES[victim_key]["auth_token"]
                == srv.hashlib.sha256(b"victim-secret").hexdigest()
            )
        finally:
            srv.AUTH_TOKEN_REQUIRED = old_required
            srv._last_score_prune = old_prune
            srv.SCORES.pop(victim_key, None)
            for flood_name in flood_names:
                srv.SCORES.pop(flood_name.lower(), None)

        # --- 6. a pre-token account cannot be claimed with a new token -----
        # #647: progress but no stored hash means an offered token has nothing
        # to match, so accepting it would make the caller the owner. The login
        # must fail closed and must not bind the token it was handed.
        legacy = srv.Player(ws=FakeWS(), id=92200, name="", logged_in=False)
        await srv.cmd_login(legacy, {"name": "LegacyOwner"})
        assert legacy.logged_in, "fresh tokenless login must still work"
        legacy_entry = srv.get_score_entry("LegacyOwner")
        assert legacy_entry.get("starting_purse_claimed") is True
        assert not legacy_entry.get("auth_token")
        unplayer(legacy)
        srv.name_owners.pop("legacyowner", None)

        claimer = srv.Player(ws=FakeWS(), id=92201, name="", logged_in=False)
        INBOX.clear()
        await srv.cmd_login(claimer, {"name": "LegacyOwner", "token": "attacker-owned"})
        assert not claimer.logged_in, "legacy account was claimed by a new token"
        assert any(m.get("type") == "error" for m in INBOX), INBOX
        assert not srv.SCORES["legacyowner"].get(
            "auth_token"
        ), "rejected login bound the offered token"
        unplayer(claimer)

        # The operator lever restores the old claim path deliberately.
        old_claim = srv.ALLOW_LEGACY_TOKEN_CLAIM
        try:
            srv.ALLOW_LEGACY_TOKEN_CLAIM = True
            operator_claim = srv.Player(ws=FakeWS(), id=92202, name="", logged_in=False)
            INBOX.clear()
            await srv.cmd_login(
                operator_claim, {"name": "LegacyOwner", "token": "operator-approved"}
            )
            assert operator_claim.logged_in, "operator opt-in must allow the claim"
            assert (
                srv.SCORES["legacyowner"]["auth_token"]
                == srv.hashlib.sha256(b"operator-approved").hexdigest()
            )
            unplayer(operator_claim)
        finally:
            srv.ALLOW_LEGACY_TOKEN_CLAIM = old_claim
            srv.name_owners.pop("legacyowner", None)
            drop_entry("LegacyOwner")

        await test_json_hardening()
        print("SECURITY_NAME_BOUNDS_OK")
    finally:
        srv.send = real_send


asyncio.run(main())
