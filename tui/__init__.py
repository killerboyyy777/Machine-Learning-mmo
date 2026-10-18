"""Terminal UI for server and GM ops (#319).

`python -m tui` gives ops the same read-only view the browser dashboard
gets, over ssh, with no browser: the tab strip mirrors the dashboard
tabs and every panel is rendered from `GET /api/state`, the endpoint the
dashboard already polls. Nothing here is on the engine's import graph --
`server.py`, `ml/` and `torch_agents/` stay websockets-only -- and the
TUI itself speaks plain HTTP through the stdlib, so the only new
dependency is rich, and only for rendering.

This module deliberately re-exports just the rich-free pieces so the
snapshot/tab layer (and its tests) import without rich installed; the
app shell lives in `tui.app` and pulls rich in.
"""

from .snapshot import SnapshotClient, SnapshotError, SnapshotPoller
from .tabs import DEFAULT_TAB, TAB_IDS, TAB_LABELS, TABS, label_for, next_tab, prev_tab

__all__ = [
    "DEFAULT_TAB",
    "TABS",
    "TAB_IDS",
    "TAB_LABELS",
    "SnapshotClient",
    "SnapshotError",
    "SnapshotPoller",
    "label_for",
    "next_tab",
    "prev_tab",
]
