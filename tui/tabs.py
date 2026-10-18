"""Tab registry for the terminal UI (#319).

Mirrors the dashboard tab bar one-for-one, in the same order, with the
same ids: the browser dashboard switches views with `data-tab` ids and
the TUI switches panels with these. `tests/test_tui.py` pins the two
lists against each other, so adding a dashboard tab without a TUI tab
(an overview tab dropping out, say) fails CI instead of rotting.

Ids and labels are copied verbatim from dashboard.html; the label is
display-only and may differ from the id (`dungeon` is labelled
"Dungeons"). Rich-free on purpose: the registry is importable (and
testable) without rich installed.
"""

# (tab id, dashboard label) in dashboard order.
TABS = (
    ("overview", "Overview"),
    ("world", "World"),
    ("players", "Players"),
    ("market", "Market"),
    ("dungeon", "Dungeons"),
    ("agents", "Agents"),
    ("quests", "Quests"),
    ("crafting", "Crafting"),
    ("config", "Config"),
    ("runs", "Runs"),
    ("gm", "GM"),
)

TAB_IDS = tuple(tab for tab, _ in TABS)
TAB_LABELS = dict(TABS)

# The dashboard lands on Overview, and so does the TUI.
DEFAULT_TAB = "overview"


def label_for(tab):
    """Display label for a tab id."""
    return TAB_LABELS[tab]


def index_of(tab):
    """Position of `tab` in TABS, or -1 when it is not a known tab."""
    try:
        return TAB_IDS.index(tab)
    except ValueError:
        return -1


def step(tab, delta):
    """Tab `delta` positions from `tab`, wrapping at both ends.

    Wrapping is what a terminal user expects from arrow keys at the
    edges of a finite list; an unknown tab falls back to the default
    rather than raising inside a keystroke handler.
    """
    index = index_of(tab)
    if index < 0:
        return DEFAULT_TAB
    return TAB_IDS[(index + delta) % len(TAB_IDS)]


def next_tab(tab):
    """The tab after `tab` (wraps)."""
    return step(tab, 1)


def prev_tab(tab):
    """The tab before `tab` (wraps)."""
    return step(tab, -1)
