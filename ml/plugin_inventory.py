"""What the drop-in folder offers, for the dashboard (#177).

The panel has three questions to answer without starting anything: which
plugins can be selected, what config each one takes, and why a file was
refused. It answers them from the same admission code the conductor runs
(`load_plugins`), so the dashboard can never offer a plugin a real
launch would reject.

Admission runs in a child interpreter and only serialized metadata comes
back. `load_plugins` probes each file in a child but then imports the
survivors again for real, so calling it here would execute drop-in
top-level code inside the HTTP server. A module that hangs or mutates
state on its second import must not be able to do that to the dashboard.
The result is cached per folder: a re-scan is an explicit cache reset.
"""

import json
import os
import subprocess
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PLUGIN_DIR = os.path.join(REPO_ROOT, "plugins")

# load_plugins probes every file in its own child before importing it here,
# so one folder can take several probe budgets. This is a read route, not a
# launch: a slow inventory degrades to an error, it never blocks a write.
INVENTORY_TIMEOUT_S = 60.0

# A plugin may print at import time. The markers bound our own JSON so its
# output cannot be mistaken for the reply.
_START = "__PLUGIN_INVENTORY_START__"
_END = "__PLUGIN_INVENTORY_END__"

_CHILD_SOURCE = """
import io
import json
import os
import sys

root, folder = sys.argv[1], sys.argv[2]
sys.path.insert(0, root)
sys.path.insert(0, os.path.join(root, "ml"))
import plugin_inventory

# Built-ins print a config line at import time; keep it out of the JSON.
real_stdout = sys.stdout
sys.stdout = io.StringIO()
try:
    payload = plugin_inventory._collect(folder)
finally:
    sys.stdout = real_stdout
print(plugin_inventory._START)
print(json.dumps(payload, default=str))
print(plugin_inventory._END)
"""

# Serialising a schema needs a name for the constructor to coerce with. The
# admission check already guarantees the entry is callable, so anything else
# is reported by its own name rather than being dropped.
_TYPE_NAMES = {int: "int", float: "float", bool: "bool", str: "str"}

_cache = {}


def _type_name(kind):
    return _TYPE_NAMES.get(kind, getattr(kind, "__name__", str(kind)))


def _schema(plugin_cls):
    try:
        raw = plugin_cls.config_schema()
    except Exception:
        return {}
    fields = {}
    for key, spec in raw.items():
        fields[key] = {
            "type": _type_name(spec.get("type")),
            "default": spec.get("default"),
            "help": spec.get("help", ""),
        }
    return fields


def _collect(folder):
    """Run admission in the child and build the JSON-safe reply."""
    from ml.plugins import KNOWN_AGENT_TYPES, REGISTRY, load_plugins

    admitted, rejected = load_plugins(folder)
    dropins = set(admitted)
    plugins = []
    for name in sorted(REGISTRY):
        plugin_cls = REGISTRY[name]
        plugins.append(
            {
                "name": name,
                "agent_type": getattr(plugin_cls, "agent_type", ""),
                "source": "drop-in" if name in dropins else "built-in",
                "config_schema": _schema(plugin_cls),
            }
        )
    return {
        "plugin_dir": folder,
        "plugin_dir_present": os.path.isdir(folder),
        "known_agent_types": list(KNOWN_AGENT_TYPES),
        "plugins": plugins,
        "rejected": [{"name": name, "reason": why} for name, why in rejected],
    }


def _failure(message):
    return {"error": message}


def _admit(plugin_dir):
    folder = os.path.abspath(plugin_dir or PLUGIN_DIR)
    if folder not in _cache:
        try:
            proc = subprocess.run(
                [sys.executable, "-c", _CHILD_SOURCE, REPO_ROOT, folder],
                capture_output=True,
                text=True,
                timeout=INVENTORY_TIMEOUT_S,
                check=False,
            )
        except subprocess.TimeoutExpired:
            _cache[folder] = _failure(
                f"plugin inventory did not finish within {INVENTORY_TIMEOUT_S:g}s"
            )
            return _cache[folder]
        out = proc.stdout or ""
        if proc.returncode != 0 or _START not in out or _END not in out:
            tail = (proc.stderr or out).strip().splitlines()
            reason = tail[-1] if tail else f"exit {proc.returncode}"
            _cache[folder] = _failure(f"plugin inventory failed: {reason}")
            return _cache[folder]
        body = out.split(_START, 1)[1].split(_END, 1)[0]
        try:
            payload = json.loads(body)
        except ValueError as exc:
            _cache[folder] = _failure(f"plugin inventory returned bad JSON: {exc}")
            return _cache[folder]
        _cache[folder] = payload
    return _cache[folder]


def inventory(plugin_dir=None):
    """Every selectable plugin, its config schema, and every refusal.

    `plugin_dir` defaults to the top-level `plugins/` folder the conductor
    scans by default. A missing folder is not an error: container images
    copy `ml/` and not `plugins/`, so the answer is simply the built-ins.
    A child-side failure becomes an `error` key, not an exception that
    would blank the whole panel.
    """
    folder = os.path.abspath(plugin_dir or PLUGIN_DIR)
    payload = _admit(folder)
    if "error" in payload:
        result = _failure(payload["error"])
        result.update(
            {
                "plugin_dir": folder,
                "plugin_dir_present": os.path.isdir(folder),
                "known_agent_types": [],
                "plugins": [],
                "rejected": [],
            }
        )
        return result
    return dict(payload)


def reset_cache():
    """Forget the admission results (tests, and a manual re-scan)."""
    _cache.clear()
