"""Panel rendering for the terminal UI (#319).

One function per dashboard tab, dispatched by tab id. Overview is the
proving panel (TUI-1): it mirrors `renderOverview()` in dashboard.html
-- health cards, the three history trends, recent activity -- reading the
same snapshot fields. The other tabs render an explicit placeholder
until their panels land; the dispatch table below is the seam the rest
of the #319 breakdown fills in.

Every panel takes `(snapshot)` and returns a rich renderable, so panels
compose into one frame and stay testable without a terminal.
"""

from rich.console import Group
from rich.table import Table
from rich.text import Text

from .tabs import TABS, label_for

# Panels that are implemented; anything else renders a placeholder.
# Panels import rich, so this module does too.
PLACEHOLDER_HINT = "panel lands with the rest of the #319 TUI breakdown"

ACTIVITY_ROWS = 10
CARD_COLUMNS = 4  # health cards per row: 8 cards, two rows of four
SPARK_CHARS = ".:-=+*#%@"
# Everything in an overview frame except the activity rows: tab bar, status
# and keybinds, section headings, health cards, trend rows, spacers. Live
# crops a frame to the terminal height, so the activity list is the part
# that has to give way on a short ssh window.
CHROME_ROWS = 18


def frame(tab, snapshot, status, age=None, height=None):
    """The whole TUI frame: tab bar, status line, active panel.

    Status sits above the panel, not below: Live crops overflow off the
    bottom, so a footer is the first thing an 80x24 terminal loses.
    """
    return Group(
        render_tab_bar(tab),
        render_status(status, age),
        Text(""),
        render_panel(tab, snapshot, activity_rows(height)),
    )


def activity_rows(height):
    """How many activity lines fit in a terminal `height` rows tall."""
    if not height:
        return ACTIVITY_ROWS
    return max(3, min(ACTIVITY_ROWS, height - CHROME_ROWS))


def render_tab_bar(active):
    """Single-line tab strip, active tab highlighted.

    Styling is color-only, never padding: an 80-column ssh window has to
    fit all eleven labels, and a background-colored cell would also make
    the strip jump around as the active tab changes.
    """
    bar = Text()
    for tab, label in TABS:
        if tab == active:
            bar.append(f">{label}", style="bold bright_cyan")
        else:
            bar.append(f" {label}", style="dim")
    return bar


def render_panel(tab, snapshot, rows=ACTIVITY_ROWS):
    """Renderable for `tab`; a placeholder for unimplemented tabs."""
    if snapshot is None:
        return Text("waiting for the first snapshot...", style="dim italic")
    if tab == "overview":
        return render_overview(snapshot, rows)
    return _placeholder(tab)


def _placeholder(tab):
    body = Table.grid(padding=(0, 1))
    body.add_column()
    body.add_row(
        Text(f"{label_for(tab)} is not ported to the terminal yet.", style="bold")
    )
    body.add_row(Text(PLACEHOLDER_HINT, style="dim"))
    return body


def _as_float(value, default=0.0):
    """Snapshot fields are numbers by contract, but a bad one must not
    take the whole frame down: the dashboard renders `-` instead."""
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def render_overview(snapshot, rows=ACTIVITY_ROWS):
    """Overview tab: health cards, history trends, recent activity."""
    return Group(
        Text("Health", style="bold"),
        _health_cards(snapshot),
        Text(""),
        Text("Trends", style="bold"),
        _trends(snapshot),
        Text(""),
        Text("Recent activity", style="bold"),
        _activity(snapshot, rows),
    )


def _health_cards(snapshot):
    server = snapshot.get("server") or {}
    market = snapshot.get("market") or {}
    players_online = server.get("players_online") or 0
    cards = (
        ("players", str(server.get("players_online", "-"))),
        ("connections", str(server.get("connections", "-"))),
        ("treasury", f"{_as_float(market.get('treasury')):.1f}"),
        ("open orders", str(len(market.get("orders") or []))),
        ("trades", str(market.get("trade_count") or 0)),
        ("dungeons", str(len(snapshot.get("dungeons") or []))),
        ("uptime", f"{_as_float(server.get('uptime')):.0f}s"),
        (
            "health",
            "healthy" if players_online > 0 else "idle",
        ),
    )
    grid = Table.grid(padding=(0, 2))
    for _ in range(CARD_COLUMNS):
        grid.add_column(justify="right")
    for start in range(0, len(cards), CARD_COLUMNS):
        chunk = cards[start : start + CARD_COLUMNS]
        grid.add_row(*[Text(value, style="bold") for _, value in chunk])
        grid.add_row(*[Text(label, style="dim") for label, _ in chunk])
    return grid


def _trends(snapshot, width=40):
    history = snapshot.get("history") or []
    top_scores = [
        (entry.get("top_scores") or [{}])[0].get("score", 0) for entry in history
    ]
    series = (
        ("players online", [entry.get("players_online") or 0 for entry in history]),
        ("top score", top_scores),
        ("market orders", [entry.get("market_orders") or 0 for entry in history]),
    )
    grid = Table.grid(padding=(0, 2))
    grid.add_column(justify="right", style="dim")
    grid.add_column(no_wrap=True)
    grid.add_column(justify="right", style="bold")
    if not history:
        grid.add_row(Text("no history samples yet"), Text(""), Text(""))
        return grid
    for label, values in series:
        latest = f"{values[-1]:g}" if values else "-"
        grid.add_row(Text(label), Text(sparkline(values, width)), Text(latest))
    return grid


def sparkline(values, width=40):
    """ASCII sparkline, bucketed down to `width` columns."""
    if not values:
        return ""
    buckets = _bucket(values, width)
    low = min(buckets)
    span = max(buckets) - low
    if span <= 0:
        return SPARK_CHARS[len(SPARK_CHARS) // 2] * len(buckets)
    last = len(SPARK_CHARS) - 1
    return "".join(SPARK_CHARS[round((value - low) / span * last)] for value in buckets)


def _bucket(values, width):
    """Average `values` down to at most `width` points, oldest first."""
    if len(values) <= width:
        return list(values)
    buckets = []
    for index in range(width):
        start = index * len(values) // width
        end = (index + 1) * len(values) // width
        chunk = values[start:end]
        buckets.append(sum(chunk) / len(chunk) if chunk else 0)
    return buckets


def _activity(snapshot, rows=ACTIVITY_ROWS):
    entries = [
        entry
        for entry in (snapshot.get("activity") or [])[-rows:]
        if isinstance(entry, dict)
    ]
    if not entries:
        return Text("no activity yet", style="dim italic")
    table = Table(box=None, show_header=True, header_style="dim", expand=True)
    table.add_column("time", no_wrap=True)
    table.add_column("player", no_wrap=True)
    table.add_column("cmd", no_wrap=True)
    table.add_column("detail", overflow="ellipsis")
    for entry in reversed(entries):  # newest first, like the dashboard
        level = entry.get("level") or 0
        name = entry.get("name") or "-"
        player = f"{name} (lv{level})" if level else name
        table.add_row(
            str(entry.get("time") or "-"),
            player,
            str(entry.get("cmd") or "-"),
            Text(str(entry.get("detail") or ""), style="dim"),
        )
    return table


def render_status(status, age=None):
    """Bottom line: link state, snapshot age, keybindings."""
    style = "green" if status == "live" else "yellow"
    if age is None:
        stamp = "no data"
    else:
        stamp = f"snapshot {age:.1f}s old"
    return Text.assemble(
        (status, f"bold {style}"),
        (" | ", "dim"),
        (stamp, "dim"),
        ("\n", ""),
        ("left/right or tab switch", "dim"),
        (" | ", "dim"),
        ("r refresh", "dim"),
        (" | ", "dim"),
        ("p pause", "dim"),
        (" | ", "dim"),
        ("q quit", "dim"),
    )
