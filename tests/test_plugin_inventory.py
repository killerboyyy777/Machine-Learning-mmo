"""Dashboard plugin-control unit tests (no running server needed).

Run from the repo root:  python tests/test_plugin_inventory.py
Covers #177: the inventory the Plugins tab reads, the slot string the launch
route builds, and the validation that happens before any child is spawned.
"""

import json
import os
import sys
import tempfile
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import server  # noqa: E402
from ml import plugin_inventory  # noqa: E402
from ml.plugins import REGISTRY, parse_slot  # noqa: E402

# --- inventory: built-ins, schemas, and a JSON-safe reply -----------------
plugin_inventory.reset_cache()
data = plugin_inventory.inventory()
by_name = {row["name"]: row for row in data["plugins"]}
assert data["plugin_dir"] == plugin_inventory.PLUGIN_DIR
assert data["plugin_dir_present"] is True
assert {"linear", "torch", "gather", "arena"} <= set(by_name), "built-ins missing"
assert by_name["linear"]["source"] == "built-in"
assert by_name["linear"]["agent_type"] == "linear"
eps = by_name["linear"]["config_schema"]["epsilon"]
assert eps["type"] == "float" and isinstance(eps["default"], float)
assert eps["help"], "schema help text missing"
assert isinstance(by_name["gather"]["config_schema"], dict)
assert data["known_agent_types"] and data["rejected"] == []
json.dumps(data)  # the read route must be able to serialise every schema value
print("INVENTORY_OK")

# --- a refused drop-in carries its reason, not a silent omission ----------
with tempfile.TemporaryDirectory() as tmp:
    with open(os.path.join(tmp, "bad_dropin.py"), "w", encoding="utf-8") as fh:
        fh.write(
            "from ml.plugins import AgentPlugin, register\n"
            "@register\n"
            "class BadDropin(AgentPlugin):\n"
            "    name = 'bad_dropin'\n"
            "    agent_type = 'not_a_kind'\n"
            "    def act(self, obs, aid, mask=None):\n"
            "        return 0\n"
        )
    before = set(REGISTRY)
    try:
        refused = plugin_inventory.inventory(tmp)
        assert "bad_dropin" not in {p["name"] for p in refused["plugins"]}
        assert len(refused["rejected"]) == 1
        assert refused["rejected"][0]["name"] == "bad_dropin"
        assert "agent_type" in refused["rejected"][0]["reason"]
        # The cache is per folder: the default folder's result is untouched.
        assert plugin_inventory.inventory()["rejected"] == []
    finally:
        for name in set(REGISTRY) - before:
            REGISTRY.pop(name, None)
        plugin_inventory.reset_cache()
print("INVENTORY_REJECT_OK")

# --- slot strings use the form parse_slot reads ---------------------------
slot = server._agent_slot(
    "linear", {"checkpoint": "X.pt", "epsilon": 0.2, "verbose": True}
)
assert slot == "linear:checkpoint=X.pt,epsilon=0.2,verbose=true", slot
parsed = parse_slot(slot)
assert parsed["plugin"] == "linear"
assert parsed["config"]["checkpoint"] == "X.pt"
assert parsed["config"]["epsilon"] == 0.2
assert server._agent_slot("gather", {}) == "gather"
print("AGENT_SLOT_OK")

# --- validation refuses before a child is spawned -------------------------
cases = (
    ({"plugin": ""}, "plugin is required"),
    ({"plugin": "  "}, "plugin is required"),
    ({"plugin": "linear", "agents": "many"}, "must be numbers"),
    ({"plugin": "linear", "agents": 0}, "between 1 and"),
    ({"plugin": "linear", "agents": 201}, "between 1 and"),
    ({"plugin": "linear", "duration": -1}, "between 0 and"),
    ({"plugin": "linear", "duration": 86401}, "between 0 and"),
    ({"plugin": "linear", "config": [1, 2]}, "config must be a JSON object"),
    ({"plugin": "not-a-plugin"}, "is not admitted"),
)
for body, expected in cases:
    status, payload = server._agents_start(body)
    assert status == 400 and expected in payload["error"], (body, status, payload)
assert server._agent_proc is None, "a validation case spawned a child"
print("AGENT_START_VALIDATION_OK")

status, payload = server._agents_stop()
assert status == 409 and payload["ok"] is False
assert "no agent run" in payload["error"]
print("AGENT_STOP_EMPTY_OK")


class FakeProc:
    """Popen-shaped: poll() reaps, returncode is what Popen exposes."""

    def __init__(self):
        self.code = None
        self.returncode = None
        self.signalled = False

    def poll(self):
        if self.code is not None:
            self.returncode = self.code
        return self.code

    def wait(self, timeout=None):
        self.returncode = self.code
        return self.code

    def send_signal(self, sig):
        self.signalled = True

    def kill(self):
        self.code = -9


# --- status reaps the exit code once and reports elapsed time -------------
saved_proc, saved_meta = server._agent_proc, server._agent_meta
try:
    proc = FakeProc()
    server._agent_proc = proc
    server._agent_meta = {
        "plugin": "linear",
        "agents": 5,
        "duration_s": 300,
        "pid": 4242,
        "label": "linear x5 for 300s",
        "started_at": time.time() - 6,
        "returncode": None,
    }
    run = server._agent_payload()
    assert run["running"] is True and run["returncode"] is None
    assert run["elapsed_s"] >= 5 and run["pid"] == 4242
    proc.code = 3
    done = server._agent_payload()
    assert done["running"] is False and done["returncode"] == 3
    assert server._agent_payload()["returncode"] == 3  # stable after reaping
    status, payload = server._agents_stop()
    assert status == 409, "a finished child must not be signalled"
    assert proc.signalled is False
finally:
    server._agent_proc, server._agent_meta = saved_proc, saved_meta
print("AGENT_PAYLOAD_OK")

# --- stopping a live child signals it and records the exit code -----------
saved_proc, saved_meta = server._agent_proc, server._agent_meta
try:
    proc = FakeProc()
    server._agent_proc = proc
    server._agent_meta = {"started_at": time.time(), "returncode": None}
    status, payload = server._agents_stop()
    assert status == 200 and payload["ok"] is True, payload
    assert proc.signalled is True
    assert payload["status"]["returncode"] is None  # FakeProc.wait returns None
finally:
    server._agent_proc, server._agent_meta = saved_proc, saved_meta
print("AGENT_STOP_OK")

# --- read route: LAN-visible, with a per-peer edit verdict ----------------
local = server._plugins_payload("127.0.0.1")
remote = server._plugins_payload("10.0.0.5")
assert local["editable"] is True and remote["editable"] is False
assert not local.get("error"), local.get("error")
assert "plugins" in local and "rejected" in local
print("PLUGINS_PAYLOAD_OK")

print("ALL_PLUGIN_INVENTORY_OK")
