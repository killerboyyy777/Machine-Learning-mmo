"""Metrics route contract tests (#21): flag-gated, read-only, aggregate only.

Run from the repo root:  python tests/test_metrics_route.py
No server is started and no socket is opened: the real do_GET is compiled out
of server.py and driven with a stub handler over a stub cached snapshot.
"""

import ast
import json
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
import server as srv  # noqa: E402

SRC = os.path.join(ROOT, "server.py")
SAMPLE_RE = re.compile(r"^[a-zA-Z_:][a-zA-Z0-9_:]*\s+-?\d+(?:\.\d+)?(?:[eE][+-]?\d+)?$")


def real_do_GET():
    with open(SRC, encoding="utf-8") as fh:
        tree = ast.parse(fh.read())
    start = next(
        n
        for n in tree.body
        if isinstance(n, ast.FunctionDef) and n.name == "start_dashboard"
    )
    handler = next(
        n for n in start.body if isinstance(n, ast.ClassDef) and n.name == "Handler"
    )
    method = next(
        n for n in handler.body if isinstance(n, ast.FunctionDef) and n.name == "do_GET"
    )
    module = ast.fix_missing_locations(ast.Module(body=[method], type_ignores=[]))
    ns = srv.__dict__
    exec(compile(module, SRC, "exec"), ns)
    return ns["do_GET"]


class Stub:
    def __init__(self, path):
        self.path = path
        self.headers = {}
        self.client_address = ("127.0.0.1", 12345)
        self.calls = []

    def _send(self, body, content_type, status=200):
        self.calls.append(("send", body, content_type, status))

    def _send_json(self, obj, status=200):
        self.calls.append(("json", obj, status))

    def send_error(self, code, *args, **kwargs):
        self.calls.append(("error", code))

    def _serve_activity_stream(self):
        self.calls.append(("stream",))


def snapshot_fixture():
    return {
        "server": {"players_online": 3, "connections": 4, "uptime": 1234},
        "rooms": [
            {
                "npcs": [
                    {"name": "RoomNpcAlpha", "alive": True},
                    {"name": "RoomNpcBeta", "alive": False},
                ]
            },
            {"npcs": [{"name": "RoomNpcGamma", "alive": True}]},
        ],
        "bosses": [{"name": "WorldBossOne"}],
        "dungeons": [{"id": 1, "party": ["PartyLeaderName"]}],
        "market": {
            "trade_count": 7,
            "orders": [{"seller": "SellerIdentity"}],
            "bids": [{"buyer": "BuyerIdentity"}],
        },
        "freight": {"delivered": 9},
        "combat": {"player_damage": 100},
    }


IDENTITIES = (
    "RoomNpcAlpha",
    "RoomNpcBeta",
    "RoomNpcGamma",
    "WorldBossOne",
    "PartyLeaderName",
    "SellerIdentity",
    "BuyerIdentity",
)


def test_flag_off_404():
    do_GET = real_do_GET()
    saved = (srv.METRICS_ENABLED, srv.world_snapshot_json)
    try:
        srv.METRICS_ENABLED = False
        srv.world_snapshot_json = json.dumps(snapshot_fixture())
        stub = Stub("/metrics")
        do_GET(stub)
        assert stub.calls == [("error", 404)], stub.calls
    finally:
        srv.METRICS_ENABLED, srv.world_snapshot_json = saved
    print("METRICS_OFF_404_OK")


def test_flag_on_text():
    do_GET = real_do_GET()
    cache = json.dumps(snapshot_fixture())
    saved = (srv.METRICS_ENABLED, srv.world_snapshot_json, srv._memory_mb)
    try:
        srv.METRICS_ENABLED = True
        srv.world_snapshot_json = cache
        srv._memory_mb = lambda: 42.5
        stub = Stub("/metrics?scrape=1")
        do_GET(stub)
        assert len(stub.calls) == 1, stub.calls
        kind, body, content_type, status = stub.calls[0]
        assert kind == "send" and status == 200, stub.calls
        assert content_type == "text/plain; version=0.0.4; charset=utf-8"
        assert isinstance(body, bytes)
        text = body.decode("utf-8")
        samples = {}
        for line in text.splitlines():
            if not line or line.startswith("#"):
                continue
            assert SAMPLE_RE.match(line), line
            name, value = line.split()
            assert name not in samples, name
            samples[name] = float(value)
        expected = {
            "mmo_uptime_seconds": 1234.0,
            "mmo_players_online": 3.0,
            "mmo_connections": 4.0,
            "mmo_rooms": 2.0,
            "mmo_npcs_alive": 2.0,
            "mmo_bosses_alive": 1.0,
            "mmo_dungeons_active": 1.0,
            "mmo_trade_count": 7.0,
            "mmo_market_orders": 1.0,
            "mmo_market_bids": 1.0,
            "mmo_freight_delivered": 9.0,
            "mmo_memory_mb": 42.5,
        }
        assert samples == expected, samples
        for identity in IDENTITIES:
            assert identity not in text, identity
        assert "{" not in text and "}" not in text, "labels must stay static"
        assert srv.world_snapshot_json == cache, "cached snapshot was mutated"
    finally:
        srv.METRICS_ENABLED, srv.world_snapshot_json, srv._memory_mb = saved
    print("METRICS_ON_TEXT_OK")


def test_missing_cache_safe():
    do_GET = real_do_GET()
    saved = (srv.METRICS_ENABLED, srv.world_snapshot_json, srv._memory_mb)
    try:
        srv.METRICS_ENABLED = True
        srv._memory_mb = lambda: None
        for cache in (None, "not json"):
            srv.world_snapshot_json = cache
            stub = Stub("/metrics")
            do_GET(stub)
            assert len(stub.calls) == 1 and stub.calls[0][0] == "send", stub.calls
            assert "mmo_players_online 0" in stub.calls[0][1].decode("utf-8")
    finally:
        srv.METRICS_ENABLED, srv.world_snapshot_json, srv._memory_mb = saved
    print("METRICS_CACHE_OK")


def test_cli_and_source_gate():
    saved = srv.METRICS_ENABLED
    with open(SRC, encoding="utf-8") as fh:
        src = fh.read()
    try:
        srv.METRICS_ENABLED = False
        assert srv.parse_args([]).metrics is False
        assert srv.parse_args(["--metrics"]).metrics is True
        srv.METRICS_ENABLED = True
        assert srv.parse_args([]).metrics is True
    finally:
        srv.METRICS_ENABLED = saved
    assert "TEXTMMO_METRICS" in src
    assert 'elif path == "/metrics" and METRICS_ENABLED:' in src
    assert 'elif path == "/metrics":' not in src
    print("METRICS_GATE_OK")


def test_activity_stream_guard():
    with open(SRC, encoding="utf-8") as fh:
        src = fh.read()
    tree = ast.parse(src)
    start = next(
        n
        for n in tree.body
        if isinstance(n, ast.FunctionDef) and n.name == "start_dashboard"
    )
    handler = next(
        n for n in start.body if isinstance(n, ast.ClassDef) and n.name == "Handler"
    )
    methods = {
        n.name: ast.get_source_segment(src, n)
        for n in handler.body
        if isinstance(n, ast.FunctionDef)
    }
    route = methods["do_GET"].split('elif path == "/api/activity/stream":', 1)[1]
    route = route.split("elif path ==", 1)[0]
    assert "_loopback_peer" in route, route
    assert "_loopback_origin" in route, route
    assert "send_error" in route, route
    assert "_serve_activity_stream" in route, route
    stream = methods["_serve_activity_stream"]
    assert "ACTIVITY_STREAM_MAX_SUBSCRIBERS" in stream, stream
    assert "ACTIVITY_STREAM_MAX_SECONDS" in stream, stream
    assert "len(activity_subscribers)" in stream, stream
    assert "_activity_subscribers_lock" in stream, stream
    assert "time.monotonic" in stream, stream
    assert srv.ACTIVITY_STREAM_MAX_SUBSCRIBERS > 0
    assert srv.ACTIVITY_STREAM_MAX_SECONDS > 0
    print("ACTIVITY_STREAM_GUARD_OK")


if __name__ == "__main__":
    test_flag_off_404()
    test_flag_on_text()
    test_missing_cache_safe()
    test_cli_and_source_gate()
    test_activity_stream_guard()
    print("ALL_METRICS_OK")
