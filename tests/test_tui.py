"""Terminal UI contract tests (no running server needed).

Run from the repo root:  python tests/test_tui.py
Covers #319: the TUI tab registry still mirrors the dashboard tab
bar (ids, labels, order, default), the snapshot client turns a real HTTP
response into a dict and every failure mode into SnapshotError, the
poller keeps the last good snapshot through an outage, the
Overview/World/Players/Market/Dungeon/Agents/Quests/Crafting panels
(TUI-1 through TUI-3) render the dashboard's fields, and the app shell
routes keys and repaints only when the screen changed.

The rich-dependent half is skipped (not failed) when rich is absent, so
a websockets-only checkout still gets the contract half. One check is
deliberate about the dependency itself: rich may be imported from tui/
and nowhere else, which is what keeps the engine websockets-only.
"""

import contextlib
import http.server
import io
import json
import os
import re
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

# The sys.path insert above pushes every tui import off the top of the file,
# which ruff reads as E402; a try block is the one exemption it honors.
try:
    from tui import (
        DEFAULT_TAB,
        TAB_IDS,
        TABS,
        SnapshotClient,
        SnapshotError,
        SnapshotPoller,
    )
    from tui.tabs import index_of, next_tab, prev_tab
except ImportError as exc:
    raise SystemExit(f"cannot import tui from {ROOT}: {exc}") from None

try:
    from tui.app import KeyReader, TuiApp, build_parser, render_once
    from tui.panels import frame, render_panel, render_tab_bar, sparkline

    HAVE_RICH = True
except ImportError:  # rich is ops-only tooling; the half that needs it skips
    HAVE_RICH = False


def snapshot_fixture():
    """A snapshot shaped like the real /api/state, trimmed to what the
    overview panel reads (the full contract lives in
    tests/test_snapshot_contract.py)."""
    return {
        "server": {
            "ws_port": 8765,
            "gm_port": 8767,
            "uptime": 4200,
            "start_ts": 0.0,
            "players_online": 3,
            "connections": 4,
        },
        "rooms": [
            {
                "id": "town_square",
                "name": "Town Square",
                "shelter": True,
                "wild": False,
                "exits": [{"dir": "north", "to": "market", "to_name": "Market"}],
                "players": [{"name": "a", "level": 2}],
                "npcs": [{"name": "Guard", "alive": True, "hp": 8, "max_hp": 10}],
                "items": ["Pine Resin"],
                "gold": 0,
            },
            {
                "id": "wilds",
                "name": "Wilds",
                "shelter": False,
                "wild": True,
                "exits": [
                    {"dir": "south", "to": "town_square", "to_name": "Town Square"}
                ],
                "players": [],
                "npcs": [{"name": "Wolf", "alive": False, "hp": 0, "max_hp": 12}],
                "items": [],
                "gold": 5,
            },
        ],
        "players": [
            {
                "name": "a",
                "level": 2,
                "score": 12.5,
                "kills": 3,
                "deaths": 1,
                "gold": 9,
                "hp": 7,
                "max_hp": 10,
                "room": "Town Square",
                "last_action": "gather",
                "recent_actions": [
                    {
                        "seq": 1,
                        "time": "12:00:01",
                        "cmd": "gather",
                        "detail": "Pine Resin",
                    },
                    {"seq": 2, "time": "12:00:02", "cmd": "travel", "detail": "north"},
                ],
            },
            {
                "name": "b",
                "level": 5,
                "score": 30.0,
                "kills": 9,
                "deaths": 2,
                "gold": 40,
                "hp": 10,
                "max_hp": 20,
                "room": "Wilds",
                "recent_actions": [],
            },
        ],
        "scores": [
            {"name": "b", "level": 5, "score": 30.0},
            {"name": "a", "level": 2, "score": 12.5},
        ],
        "market": {
            "treasury": 1234.5,
            "collected_lifetime": 88.0,
            "trade_count": 77,
            "orders": [{"id": 1, "seller": "a", "item": "Pine Resin", "price": 3}],
            "bids": [{"id": 2, "buyer": "b", "item": "Pine Resin", "price": 2}],
            "spread": {"best_bid": 2, "best_ask": 3},
            "history": [
                {
                    "time": "12:00:03",
                    "ts": 300.0,
                    "buyer": "b",
                    "seller": "a",
                    "item": "Pine Resin",
                    "price": 3,
                    "tax": 0.3,
                    "payout": 2.7,
                    "via": None,
                }
            ],
        },
        "dungeons": [
            {
                "id": "d1",
                "party": ["a", "b"],
                "max_floor_reached": 3,
                "floors": [
                    {"floor": 1, "guards_alive": 0, "guards_total": 2, "cleared": True},
                    {
                        "floor": 2,
                        "guards_alive": 1,
                        "guards_total": 3,
                        "cleared": False,
                    },
                ],
            }
        ],
        "buffs": {"xp": 90, "gold": 0},
        "bosses": [
            {
                "name": "Rat King",
                "room": "Deep Catacombs",
                "hp": 40,
                "max_hp": 100,
                "attack": 7,
            }
        ],
        "dungeon_clears": {
            "solo_clears": 4,
            "group_clears": 2,
            "solo_max_floor": 6,
            "group_max_floor": 3,
            "recent": [],
        },
        "quests": {
            "catalog": [
                {
                    "id": "delver",
                    "brief": "clear dungeon floors",
                    "giver_name": "Guard",
                    "room": "Town Square",
                    "reward_xp": 25,
                    "reward_gold": 10,
                },
                {
                    "id": "remedy",
                    "brief": "bring 3 Healing Herbs",
                    "giver_name": "Healer",
                    "room": "Town Square",
                    "reward_xp": 12,
                    "reward_gold": 5,
                },
            ],
            "active": {"delver": 2, "remedy": 1},
            "completions": {"delver": 7, "remedy": 3},
            "turnins_last_min": 2,
            "recent_turnins": [
                {"seq": 1, "t": "12:04:00", "name": "b", "qid": "delver"},
                {"seq": 2, "t": "12:05:00", "name": "a", "qid": "remedy"},
            ],
        },
        "commissions": [
            {
                "id": 7,
                "poster": "a",
                "target": "Rat",
                "required_kills": 5,
                "reward_gold": 20,
                "reward_xp": 15,
                "status": "open",
                "filled_by": None,
                "created_ts": 0,
                "progress": {"leader": "b", "kills": 2, "required": 5},
            }
        ],
        "commission_history": [
            {
                "seq": 5,
                "t": "12:06:00",
                "id": 7,
                "poster": "a",
                "filler": "b",
                "target": "Rat",
                "gold": 20,
                "xp": 15,
                "status": "completed",
                "refund": None,
                "forfeit": None,
            }
        ],
        "recipes": [
            {
                "id": "charm",
                "result": "Ancient Guardian Charm",
                "result_qty": 1,
                "tier": 2,
                "category": "quest",
                "inputs": [
                    {"item": "Treant Bark", "qty": 1},
                    {"item": "Troll Hide", "qty": 1},
                ],
            }
        ],
        "craft_history": [
            {
                "seq": 1,
                "t": "12:00:00",
                "name": "a",
                "recipe": "charm",
                "result": "Ancient Guardian Charm",
            }
        ],
        "history": [
            {
                "ts": 100.0 + i,
                "players_online": i % 4,
                "market_orders": i,
                "top_scores": [{"name": "a", "score": float(i * 10)}],
            }
            for i in range(50)
        ],
        "activity": [
            {
                "seq": i,
                "time": "12:00:00",
                "name": f"P{i}",
                "cmd": ("look", "gather", "craft", "take", "buy", "sell")[i % 6],
                "detail": "",
                "level": i,
            }
            for i in range(1, 26)
        ],
    }


# --- tab registry mirrors dashboard.html --------------------------------
def test_tabs_mirror_dashboard():
    html = Path(ROOT, "dashboard.html").read_text(encoding="utf-8")
    entries = re.findall(
        r'<div class="tab(?: active)?" data-tab="([\w-]+)">([^<]+)</div>', html
    )
    assert entries, "no dashboard tabs parsed"
    assert (
        tuple(entries) == TABS
    ), f"tui tabs drifted from dashboard.html: {entries} != {TABS}"
    default = re.search(r'<div class="tab active" data-tab="([\w-]+)"', html)
    assert default and default.group(1) == DEFAULT_TAB, "dashboard default tab moved"
    assert len(set(TAB_IDS)) == len(TAB_IDS), "duplicate tab ids"
    print("TAB_MIRROR_OK")


def test_tab_navigation():
    assert index_of(DEFAULT_TAB) == 0, index_of(DEFAULT_TAB)
    assert index_of("nope") == -1
    assert next_tab("overview") == TAB_IDS[1]
    assert prev_tab("overview") == TAB_IDS[-1], "prev must wrap to the last tab"
    assert next_tab(TAB_IDS[-1]) == TAB_IDS[0], "next must wrap to the first tab"
    assert prev_tab("gm") == TAB_IDS[-2]
    assert next_tab("bogus") == DEFAULT_TAB, "unknown tab falls back to default"
    walk = DEFAULT_TAB
    for _ in range(len(TAB_IDS)):
        walk = next_tab(walk)
    assert walk == DEFAULT_TAB, "cycling must return to the start tab"
    print("TAB_NAV_OK")


# --- snapshot client ----------------------------------------------------
class _StubHandler(http.server.BaseHTTPRequestHandler):
    payload = b"{}"
    status = 200

    def do_GET(self):
        body = type(self).payload
        self.send_response(type(self).status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *args):
        pass


def stub_server():
    """(server, port, handler) serving the fixture snapshot.

    `handler.payload`/`.status` are class attributes, so a test can make
    the next fetch fail without touching the client under test.
    """
    payload = json.dumps(snapshot_fixture()).encode()
    handler = type("Handler", (_StubHandler,), {"payload": payload, "status": 200})
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server, server.server_address[1], handler


def dead_port():
    """A port with nothing behind it, for connection-refused paths."""
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


def test_client_fetches_and_fails_cleanly():
    server, port, handler = stub_server()
    try:
        client = SnapshotClient("127.0.0.1", port, timeout=2.0)
        assert client.url == f"http://127.0.0.1:{port}/api/state", client.url
        snap = client.fetch()
        assert snap["server"]["players_online"] == 3, snap["server"]

        handler.status = 404
        try:
            client.fetch()
            raise AssertionError("404 must raise")
        except SnapshotError as exc:
            assert "404" in str(exc), exc
        handler.status = 200
        handler.payload = b"not json"
        try:
            client.fetch()
            raise AssertionError("garbage must raise")
        except SnapshotError:
            pass
        handler.payload = b"[1, 2, 3]"
        try:
            client.fetch()
            raise AssertionError("a JSON array is not a snapshot")
        except SnapshotError:
            pass
    finally:
        server.shutdown()
        server.server_close()
    try:
        SnapshotClient("127.0.0.1", dead_port(), timeout=1.0).fetch()
        raise AssertionError("refused connection must raise")
    except SnapshotError:
        pass
    print("SNAPSHOT_CLIENT_OK")


def test_poller_keeps_last_good_snapshot():
    server, port, handler = stub_server()
    try:
        poller = SnapshotPoller(SnapshotClient("127.0.0.1", port), interval=0.05)
        assert poller.status() == "connecting", poller.status()
        assert poller.snapshot() is None
        assert poller.refresh() is True
        assert poller.snapshot()["server"]["uptime"] == 4200
        assert poller.status() == "live", poller.status()
        assert poller.version() == 1, poller.version()
        assert poller.age() < 5.0

        handler.status = 500
        seen = poller.version()
        assert poller.refresh() is False
        assert poller.snapshot() is not None, "outage must not drop the last snapshot"
        assert poller.version() > seen, "failures still count as new state"
        assert "stale" in poller.status(), poller.status()

        poller.set_paused(True)
        assert poller.paused() and poller.status() == "paused"
        poller.stop()  # safe before start()
    finally:
        server.shutdown()
        server.server_close()

    poller = SnapshotPoller(None, interval=0.01)
    poller.client = _StubClient()
    poller.start()
    poller.start()  # idempotent
    time.sleep(0.2)
    poller.stop()
    assert poller.client.calls > 1, f"poller thread did not poll: {poller.client.calls}"
    print("SNAPSHOT_POLLER_OK")


class _StubClient:
    """Minimal client stand-in for the poller's threaded path."""

    def __init__(self):
        self.calls = 0

    def fetch(self):
        self.calls += 1
        return snapshot_fixture()


# --- rich-dependent half ------------------------------------------------
def test_panels():
    snap = snapshot_fixture()

    bar = render_tab_bar("market")
    bar_text = bar.plain
    for _, label in TABS:
        assert label in bar_text, f"tab label {label} missing from strip"
    assert ">Market" in bar_text, bar_text
    assert bar_text.count(">") == 1, "exactly one active tab"

    waiting = render_panel("overview", None)
    assert "waiting" in waiting.plain.lower(), waiting.plain

    placeholder = _plain(render_panel("config", snap))
    assert "Config" in placeholder and "not ported" in placeholder, placeholder

    panel = _plain(render_panel("overview", snap, rows=10))
    assert "1234.5" in panel, "treasury card missing"
    assert "players" in panel and "connections" in panel and "uptime" in panel
    assert "4200s" in panel, "uptime value missing"
    assert "healthy" in panel, "health card missing with players online"
    # Newest activity first, oldest past the row budget dropped (dashboard
    # shows the last ten, reversed).
    assert _names(panel) == [f"P{n}" for n in range(25, 15, -1)], _names(panel)

    short = _plain(render_panel("overview", snap, rows=3))
    assert _names(short) == ["P25", "P24", "P23"], _names(short)

    idle = _plain(render_panel("overview", snapshot_with(players_online=0), 5))
    assert "idle" in idle, "empty world should read idle"

    partial = _plain(render_panel("overview", {}, rows=5))
    assert "no activity yet" in partial and "no history samples yet" in partial

    assert sparkline([]) == ""
    assert sparkline([5, 5, 5]) == "+++", sparkline([5, 5, 5])
    ramp = sparkline(list(range(100)))
    assert len(ramp) == 40 and ramp[0] == SPARK_FIRST and ramp[-1] == SPARK_LAST, ramp
    assert len(sparkline([1, 2])) == 2

    frame_text = _plain(frame("gm", snap, "live", 0.5, height=24))
    assert (
        ">GM" in frame_text
        and "live" in frame_text
        and "snapshot 0.5s old" in frame_text
    )
    print("PANELS_OK")


def test_panels_world():
    """World panel (TUI-2): rooms/exits/occupants + history + activity."""
    snap = snapshot_fixture()
    text = _plain(render_panel("world", snap, rows=5))
    assert "not ported" not in text, "world panel should be implemented"
    for heading in ("Rooms", "History", "Live activity"):
        assert heading in text, heading
    assert "Town Square" in text and "Wilds" in text
    assert "Guard (8/10)" in text, "npc hp missing"
    assert "Wolf (dead)" in text, "dead npc marker missing"
    assert "Pine Resin" in text and "north->Market" in text
    assert "wild" in text and "shelter" in text
    assert "players online" in text and "top score" in text
    # Activity is newest-first and capped to the row budget.
    assert _names(text) == [f"P{n}" for n in range(25, 20, -1)], _names(text)
    empty = _plain(render_panel("world", {}, rows=3))
    assert "no rooms in snapshot" in empty and "no activity yet" in empty
    print("WORLD_OK")


def test_panels_players():
    """Players panel (TUI-2): performance, scores, top-player tracker."""
    snap = snapshot_fixture()
    text = _plain(render_panel("players", snap, rows=5))
    assert "not ported" not in text, "players panel should be implemented"
    for heading in ("Performance", "Scores", "Character tracker"):
        assert heading in text, heading
    assert "9" in text and "40" in text, "performance numbers missing"
    assert "30.0" in text and "12.5" in text
    # The tracker follows the top scorer (b, no actions recorded).
    assert "b (lv5)" in text, "tracker did not pick the top scorer"
    assert "no tracked actions yet" in text
    empty = _plain(render_panel("players", {}, rows=3))
    assert "no players online" in empty and "no scores recorded yet" in empty
    print("PLAYERS_OK")


def test_panels_market():
    """Market panel (TUI-2): treasury, two-sided book, trades, by-item."""
    snap = snapshot_fixture()
    text = _plain(render_panel("market", snap, rows=5))
    assert "not ported" not in text, "market panel should be implemented"
    for heading in ("Treasury", "Orders (sell)", "Bids (buy)", "Trade history"):
        assert heading in text, heading
    assert "By item" in text
    assert "1234.5" in text and "88.0" in text and "77" in text
    assert "best bid" in text and "best ask" in text
    assert "Pine Resin" in text
    assert "0.3" in text and "2.7" in text, "tax/payout columns missing"
    # One ask at 3g, one bid at 2g -> low ask 3g, high bid 2g, spread 1g.
    assert "1g" in text, "per-item spread missing"
    empty = _plain(render_panel("market", {}, rows=3))
    assert "no trades yet" in empty and "no market items yet" in empty
    print("MARKET_OK")


def test_panels_dungeon():
    """Dungeon panel (TUI-3): buffs, bosses, clears, open instances."""
    snap = snapshot_fixture()
    text = _plain(render_panel("dungeon", snap, rows=5))
    assert "not ported" not in text, "dungeon panel should be implemented"
    for heading in ("Buffs", "Bosses", "Dungeon clears", "Dungeons"):
        assert heading in text, heading
    assert "XPx2 1:30 left" in text and "GOLDx2 inactive" in text
    assert "Rat King" in text and "40/100" in text and "7" in text
    assert "solo clears" in text and "solo max floor" in text
    assert "d1" in text and "a, b" in text
    assert "F1 0/2 cleared; F2 1/3 sealed" in text, text
    empty = _plain(render_panel("dungeon", {}, rows=3))
    assert "no active buffs" in empty and "no bosses alive" in empty
    assert "no dungeons opened yet" in empty
    print("DUNGEON_OK")


def test_panels_agents():
    """Agents panel (TUI-3): roster with last action + merged feed."""
    snap = snapshot_fixture()
    text = _plain(render_panel("agents", snap, rows=5))
    assert "not ported" not in text, "agents panel should be implemented"
    for heading in ("Agents", "Agent activity"):
        assert heading in text, heading
    assert "30.0" in text and "12.5" in text
    assert "gather" in text and "travel" in text
    assert "a" in text and "b" in text
    feed = text.split("Agent activity", 1)[1]
    assert feed.index("travel") < feed.index("gather"), feed
    empty = _plain(render_panel("agents", {}, rows=3))
    assert "no agents online" in empty and "no tracked actions yet" in empty
    print("AGENTS_OK")


def test_panels_quests():
    """Quests panel (TUI-3): catalog, turn-ins, commissions, history."""
    snap = snapshot_fixture()
    text = _plain(render_panel("quests", snap, rows=5))
    assert "not ported" not in text, "quests panel should be implemented"
    for heading in ("Quests", "Recent turn-ins", "Commissions", "Commission history"):
        assert heading in text, heading
    assert "delver" in text and "remedy" in text
    assert "Guard" in text and "Healer" in text
    assert "25xp + 10g" in text and "12xp + 5g" in text
    assert "#7" in text and "2/5" in text and "20g + 15xp" in text
    assert "open" in text and "completed" in text
    assert "refund" not in text, "completed row must not show a refund split"
    empty = _plain(render_panel("quests", {}, rows=3))
    assert "no quests in catalog" in empty and "no turn-ins recorded yet" in empty
    assert "no commissions posted" in empty and "no commission history yet" in empty
    print("QUESTS_OK")


def test_panels_crafting():
    """Crafting panel (TUI-3): recipes, history, supply, flow."""
    snap = snapshot_fixture()
    text = _plain(render_panel("crafting", snap, rows=5))
    assert "not ported" not in text, "crafting panel should be implemented"
    for heading in (
        "Recipes",
        "Craft history",
        "Material supply",
        "Listed supply",
        "Command flow",
    ):
        assert heading in text, heading
    assert "Ancient Guardian Charm" in text and "Treant Bark" in text
    assert "Troll Hide" in text and "charm" in text
    assert "Pine Resin" in text and "3g" in text
    for label in ("gather", "craft", "take", "buy", "sell"):
        assert label in text, label
    empty = _plain(render_panel("crafting", {}, rows=3))
    assert "no recipes known" in empty and "no crafts yet" in empty
    assert "no loose materials on the ground" in empty
    assert "no listed supply" in empty
    print("CRAFTING_OK")


SPARK_FIRST = "."
SPARK_LAST = "@"


def _names(text):
    """Activity player names in render order."""
    return re.findall(r"(\w+) \(lv\d+\)", text)


def snapshot_with(**server_fields):
    snap = snapshot_fixture()
    snap["server"].update(server_fields)
    return snap


def _plain(renderable, width=100):
    """Flatten any renderable to text the way a terminal would see it."""
    from rich.console import Console

    console = Console(file=io.StringIO(), width=width, height=50, no_color=True)
    console.print(renderable)
    return console.file.getvalue()


def test_app_shell():
    server, port, handler = stub_server()
    try:
        poller = SnapshotPoller(SnapshotClient("127.0.0.1", port), interval=5.0)
        app = TuiApp(poller, "overview", _recording_console(), _ScriptedKeys(["r"]))
        assert app.frame() is not None

        assert app.handle("right") is True
        assert app.tab == "world"
        assert app.handle("left") is True and app.tab == "overview"
        assert app.handle("tab") is True and app.tab == "world"
        assert app.handle("shift+tab") is True and app.tab == "overview"
        assert app.handle("r") is True
        assert poller.snapshot() is not None, "r must force a fetch"
        assert app.handle("p") is True and poller.paused()
        assert app.handle("p") is True and not poller.paused()
        assert app.handle("z") is True, "unknown keys are ignored"
        assert app.handle("q") is False, "q must quit"

        live = _FakeLive()
        app._tick(live)
        assert live.updates == 1
        app._tick(live)
        assert live.updates == 1, "no change must not repaint"
        app.handle("right")
        app._tick(live)
        assert live.updates == 2, "tab switch must repaint immediately"
        assert ">World" in live.last

        assert render_once(poller, "overview", 90, _recording_console()) == 0
    finally:
        handler.payload = b"{}"
        server.shutdown()
        server.server_close()

    # No server: --once still renders a frame and exits non-zero.
    quiet = SnapshotPoller(
        SnapshotClient("127.0.0.1", dead_port(), timeout=1.0), interval=5.0
    )
    assert render_once(quiet, "overview", 90, _recording_console()) == 1

    parser = build_parser()
    args = parser.parse_args([])
    assert (args.host, args.port, args.interval, args.tab, args.once) == (
        "127.0.0.1",
        8766,
        2.0,
        "overview",
        False,
    ), vars(args)
    assert parser.parse_args(["--once", "--port", "9999", "--tab", "gm"]).port == 9999
    with contextlib.redirect_stderr(io.StringIO()):
        try:
            parser.parse_args(["--tab", "nope"])
            raise AssertionError("unknown tab must be rejected by argparse")
        except SystemExit:
            pass

    # A key name is decoded from a char already consumed off the stream,
    # with the escape sequence read from where that char left it.
    stream = io.StringIO("q\x1b[Cx\x1b")
    reader = KeyReader(stream)
    for expected in ("q", "right", "x", "esc"):
        char = stream.read(1)
        assert reader._key_name(char) == expected, (char, expected)
    reader = KeyReader(io.StringIO("q\x1b[Cx"))
    keys = []
    reader.start()
    deadline = time.time() + 2.0
    while time.time() < deadline:
        key = reader.poll()
        if key is not None:
            keys.append(key)
        elif keys:
            break
    reader.stop()
    assert keys == ["q", "right", "x", "quit"], keys
    print("APP_SHELL_OK")


class _ScriptedKeys:
    def __init__(self, keys=()):
        self.keys = list(keys)

    def start(self):
        pass

    def stop(self):
        pass

    def poll(self):
        return self.keys.pop(0) if self.keys else None


class _FakeLive:
    def __init__(self):
        self.updates = 0
        self.last = ""

    def update(self, renderable, refresh=False):
        self.updates += 1
        self.last = _plain(renderable)


def _recording_console():
    from rich.console import Console

    return Console(file=io.StringIO(), width=90, height=40, no_color=True)


# --- dependency containment --------------------------------------------
def test_engine_never_imports_rich():
    """rich is ops tooling: importing tui/ must not drag it in, and no
    engine, ML or test module may import it either."""
    out = subprocess.run(
        [sys.executable, "-c", "import sys, tui; print('rich' in sys.modules)"],
        capture_output=True,
        text=True,
        cwd=ROOT,
        check=True,
    )
    assert out.stdout.strip() == "False", out.stdout

    engine_dirs = ("ml", "torch_agents", "tests")
    engine_files = [
        os.path.join(ROOT, name)
        for name in ("server.py", "start.py", "config_write.py", "config_schema.py")
    ]
    for name in engine_dirs:
        base = os.path.join(ROOT, name)
        for dirpath, _, filenames in os.walk(base):
            engine_files.extend(
                os.path.join(dirpath, f) for f in filenames if f.endswith(".py")
            )
    pattern = re.compile(r"^\s*(import rich|from rich)", re.MULTILINE)
    offenders = [
        os.path.relpath(path, ROOT)
        for path in engine_files
        if os.path.abspath(path) != os.path.abspath(__file__)
        and pattern.search(Path(path).read_text(encoding="utf-8", errors="replace"))
    ]
    assert not offenders, f"rich leaked outside tui/: {offenders}"
    print("ENGINE_DEP_OK")


def main():
    test_tabs_mirror_dashboard()
    test_tab_navigation()
    test_client_fetches_and_fails_cleanly()
    test_poller_keeps_last_good_snapshot()
    test_engine_never_imports_rich()
    if HAVE_RICH:
        test_panels()
        test_panels_world()
        test_panels_players()
        test_panels_market()
        test_panels_dungeon()
        test_panels_agents()
        test_panels_quests()
        test_panels_crafting()
        test_app_shell()
    else:
        print("PANELS_OK skipped (rich not installed)")
        print("WORLD_OK skipped (rich not installed)")
        print("PLAYERS_OK skipped (rich not installed)")
        print("MARKET_OK skipped (rich not installed)")
        print("DUNGEON_OK skipped (rich not installed)")
        print("AGENTS_OK skipped (rich not installed)")
        print("QUESTS_OK skipped (rich not installed)")
        print("CRAFTING_OK skipped (rich not installed)")
        print("APP_SHELL_OK skipped (rich not installed)")
    print("ALL_TUI_OK")


if __name__ == "__main__":
    main()
