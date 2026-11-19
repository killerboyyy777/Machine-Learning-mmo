"""What the drop-in folder offers, for the dashboard (#177).

The panel has three questions to answer without starting anything: which
plugins can be selected, what config each one takes, and why a file was
refused. It answers them from the same admission code the conductor runs
(`load_plugins`), so the dashboard can never offer a plugin that a real
launch would reject.

Importing a plugin is the expensive part, so the admission result is
computed once per folder and cached: `importlib` will not run a module
twice, and a second `load_plugins` call would therefore report an empty
set of new names.
"""

import os
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PLUGIN_DIR = os.path.join(REPO_ROOT, "plugins")

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


def _admit(plugin_dir):
    folder = os.path.abspath(plugin_dir)
    if folder not in _cache:
        if REPO_ROOT not in sys.path:
            sys.path.insert(0, REPO_ROOT)
        from ml.plugins import load_plugins

        admitted, rejected = load_plugins(folder)
        _cache[folder] = {
            "admitted": list(admitted),
            "rejected": [{"name": name, "reason": why} for name, why in rejected],
        }
    return _cache[folder]


def inventory(plugin_dir=None):
    """Every selectable plugin, its config schema, and every refusal.

    `plugin_dir` defaults to the top-level `plugins/` folder the conductor
    scans by default. A missing folder is not an error: container images
    copy `ml/` and not `plugins/`, so the answer is simply the built-ins.
    """
    if REPO_ROOT not in sys.path:
        sys.path.insert(0, REPO_ROOT)
    from ml.plugins import KNOWN_AGENT_TYPES, REGISTRY

    folder = os.path.abspath(plugin_dir or PLUGIN_DIR)
    admission = _admit(folder)
    dropins = set(admission["admitted"])
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
        "rejected": admission["rejected"],
    }


def reset_cache():
    """Forget the admission results (tests, and a manual re-scan)."""
    _cache.clear()
