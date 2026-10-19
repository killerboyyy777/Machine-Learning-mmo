"""Panel rendering for the terminal UI (#319).

One function per dashboard tab, dispatched by tab id. Overview is the
TUI-1 proving panel; World, Players and Market land in TUI-2 (#491) and
mirror the matching dashboard renderers -- health/trend/activity for
Overview, rooms/history/activity for World, performance/scores/tracker
for Players, treasury/book/trades/by-item for Market -- reading the same
snapshot fields. The remaining tabs render an explicit placeholder until
their panels land; the dispatch below is the seam the rest of the #319
breakdown fills in.

Every panel takes `(snapshot)` and returns a rich renderable, so panels
compose into one frame and stay testable without a terminal.
"""

import time

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
    if tab == "world":
        return render_world(snapshot, rows)
    if tab == "players":
        return render_players(snapshot, rows)
    if tab == "market":
        return render_market(snapshot, rows)
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


# --- world ---------------------------------------------------------------
def render_world(snapshot, rows=ACTIVITY_ROWS):
    """World tab: rooms, history trends, live activity.

    The browser draws a canvas map; a terminal has no canvas, so the map's
    data (rooms, exits, occupants) renders as the Rooms table instead.
    """
    return Group(
        Text("Rooms", style="bold"),
        _rooms_table(snapshot),
        Text(""),
        Text("History", style="bold"),
        _trends(snapshot),
        Text(""),
        Text("Live activity", style="bold"),
        _activity(snapshot, rows),
    )


def _rooms_table(snapshot):
    rooms = [room for room in (snapshot.get("rooms") or []) if isinstance(room, dict)]
    if not rooms:
        return Text("no rooms in snapshot", style="dim italic")
    table = Table(box=None, show_header=True, header_style="dim", expand=True)
    table.add_column("room", no_wrap=True)
    table.add_column("tags", no_wrap=True, style="dim")
    table.add_column("players", overflow="fold")
    table.add_column("npcs", overflow="fold")
    table.add_column("items", overflow="fold")
    table.add_column("gold", justify="right", no_wrap=True)
    table.add_column("exits", overflow="fold", style="dim")
    for room in sorted(rooms, key=lambda entry: str(entry.get("name") or "")):
        table.add_row(
            str(room.get("name") or room.get("id") or "?"),
            _room_tags(room),
            _room_players(room),
            _room_npcs(room),
            _room_items(room),
            _room_gold(room),
            _room_exits(room),
        )
    return table


def _room_tags(room):
    tags = [
        label
        for key, label in (("wild", "wild"), ("shelter", "shelter"))
        if room.get(key)
    ]
    return ",".join(tags) or "-"


def _room_players(room):
    names = []
    for player in room.get("players") or []:
        if not isinstance(player, dict):
            continue
        level = player.get("level")
        suffix = f"(lv{level})" if level is not None else ""
        names.append(f"{player.get('name', '?')}{suffix}")
    return ", ".join(names) or "-"


def _room_npcs(room):
    names = []
    for npc in room.get("npcs") or []:
        if not isinstance(npc, dict):
            continue
        if not npc.get("alive"):
            names.append(f"{npc.get('name', '?')} (dead)")
            continue
        hp = ""
        if npc.get("max_hp"):
            hp = f" ({_as_float(npc.get('hp')):.0f}/{_as_float(npc.get('max_hp')):.0f})"
        names.append(f"{npc.get('name', '?')}{hp}")
    return ", ".join(names) or "-"


def _room_items(room):
    return ", ".join(str(item) for item in (room.get("items") or [])) or "-"


def _room_gold(room):
    gold = _as_float(room.get("gold"))
    return f"{gold:.0f}" if gold else "-"


def _room_exits(room):
    parts = []
    for link in room.get("exits") or []:
        if not isinstance(link, dict):
            continue
        target = link.get("to_name") or link.get("to")
        parts.append(f"{link.get('dir', '?')}->{target}")
    return " ".join(parts) or "-"


# --- players -------------------------------------------------------------
def render_players(snapshot, rows=ACTIVITY_ROWS):
    """Players tab: performance, scores, and the top player's tracker."""
    return Group(
        Text("Performance", style="bold"),
        _performance_table(snapshot),
        Text(""),
        Text("Scores", style="bold"),
        _scores_table(snapshot),
        Text(""),
        Text("Character tracker", style="bold"),
        _tracker(snapshot, rows),
    )


def _performance_table(snapshot):
    players = [p for p in (snapshot.get("players") or []) if isinstance(p, dict)]
    if not players:
        return Text("no players online", style="dim italic")
    table = Table(box=None, show_header=True, header_style="dim", expand=True)
    for label, kwargs in (
        ("#", {"justify": "right", "no_wrap": True}),
        ("name", {"no_wrap": True}),
        ("lv", {"justify": "right", "no_wrap": True}),
        ("score", {"justify": "right", "no_wrap": True}),
        ("kills", {"justify": "right", "no_wrap": True}),
        ("deaths", {"justify": "right", "no_wrap": True}),
        ("gold", {"justify": "right", "no_wrap": True}),
        ("hp", {"justify": "right", "no_wrap": True}),
        ("room", {"overflow": "ellipsis", "style": "dim"}),
    ):
        table.add_column(label, **kwargs)
    ordered = sorted(players, key=lambda p: _as_float(p.get("score")), reverse=True)
    for rank, player in enumerate(ordered, start=1):
        table.add_row(
            str(rank),
            str(player.get("name") or "?"),
            str(player.get("level") or 1),
            f"{_as_float(player.get('score')):.1f}",
            str(player.get("kills") or 0),
            str(player.get("deaths") or 0),
            f"{_as_float(player.get('gold')):.0f}",
            f"{_as_float(player.get('hp')):.0f}/{_as_float(player.get('max_hp')):.0f}",
            str(player.get("room") or "-"),
        )
    return table


def _scores_table(snapshot):
    scores = [s for s in (snapshot.get("scores") or []) if isinstance(s, dict)][:10]
    if not scores:
        return Text("no scores recorded yet", style="dim italic")
    table = Table(box=None, show_header=True, header_style="dim", expand=True)
    table.add_column("#", justify="right", no_wrap=True)
    table.add_column("name", no_wrap=True)
    table.add_column("lv", justify="right", no_wrap=True)
    table.add_column("score", justify="right", no_wrap=True)
    for rank, score in enumerate(scores, start=1):
        table.add_row(
            str(rank),
            str(score.get("name") or "?"),
            str(score.get("level") or 1),
            f"{_as_float(score.get('score')):.1f}",
        )
    return table


def _tracker(snapshot, rows=ACTIVITY_ROWS):
    players = [p for p in (snapshot.get("players") or []) if isinstance(p, dict)]
    if not players:
        return Text("no online players to track", style="dim italic")
    top = max(players, key=lambda p: _as_float(p.get("score")))
    actions = [a for a in (top.get("recent_actions") or []) if isinstance(a, dict)]
    meta_text = " ".join(
        [
            f" (lv{top.get('level') or 1}) |",
            f"{top.get('room') or '-'} |",
            f"score {_as_float(top.get('score')):.1f} |",
            f"{_as_float(top.get('hp')):.0f}/{_as_float(top.get('max_hp')):.0f} hp |",
            f"{_as_float(top.get('gold')):.0f} gold |",
            f"{len(actions)} recent actions",
        ]
    )
    meta = Text.assemble((str(top.get("name") or "?"), "bold"), (meta_text, "dim"))
    if not actions:
        return Group(meta, Text("no tracked actions yet", style="dim italic"))
    table = Table(box=None, show_header=True, header_style="dim", expand=True)
    table.add_column("time", no_wrap=True)
    table.add_column("cmd", no_wrap=True, style="magenta")
    table.add_column("detail", overflow="ellipsis", style="dim")
    for action in actions[-rows:]:
        table.add_row(
            str(action.get("time") or "-"),
            str(action.get("cmd") or "-"),
            str(action.get("detail") or ""),
        )
    return Group(meta, table)


# --- market --------------------------------------------------------------
def render_market(snapshot, rows=ACTIVITY_ROWS):
    """Market tab: treasury, the two-sided book, trades, per-item stats."""
    market = snapshot.get("market") or {}
    return Group(
        Text("Treasury", style="bold"),
        _market_summary(market),
        Text(""),
        Text("Orders (sell)", style="bold"),
        _book_table(market.get("orders"), "seller"),
        Text(""),
        Text("Bids (buy)", style="bold"),
        _book_table(market.get("bids"), "buyer"),
        Text(""),
        Text("Trade history", style="bold"),
        _trades_table(market.get("history"), rows),
        Text(""),
        Text("By item", style="bold"),
        _by_item_table(market),
    )


def _market_summary(market):
    spread = market.get("spread") or {}
    best_bid = spread.get("best_bid")
    best_ask = spread.get("best_ask")
    gap = None
    if best_bid is not None and best_ask is not None:
        gap = _as_float(best_ask) - _as_float(best_bid)
    cards = (
        ("treasury", f"{_as_float(market.get('treasury')):.1f}"),
        ("lifetime", f"{_as_float(market.get('collected_lifetime')):.1f}"),
        ("trades", str(market.get("trade_count") or 0)),
        ("best bid", _gold(best_bid)),
        ("best ask", _gold(best_ask)),
        ("spread", _gold(gap)),
    )
    grid = Table.grid(padding=(0, 2))
    for _ in range(3):
        grid.add_column(justify="right")
    for start in range(0, len(cards), 3):
        chunk = cards[start : start + 3]
        grid.add_row(*[Text(value, style="bold") for _, value in chunk])
        grid.add_row(*[Text(label, style="dim") for label, _ in chunk])
    return grid


def _book_table(entries, who):
    rows = [entry for entry in (entries or []) if isinstance(entry, dict)]
    if not rows:
        return Text("none", style="dim italic")
    table = Table(box=None, show_header=True, header_style="dim", expand=True)
    table.add_column("id", no_wrap=True)
    table.add_column("item", overflow="ellipsis")
    table.add_column("price", justify="right", no_wrap=True)
    table.add_column(who, no_wrap=True)
    table.add_column("time", no_wrap=True, style="dim")
    for row in sorted(rows, key=lambda entry: _as_float(entry.get("price"))):
        table.add_row(
            str(row.get("id", "?")),
            str(row.get("item") or "?"),
            str(row.get("price", "-")),
            str(row.get(who) or "?"),
            _clock(row.get("ts")),
        )
    return table


def _trades_table(history, rows=ACTIVITY_ROWS):
    trades = [trade for trade in (history or []) if isinstance(trade, dict)]
    if not trades:
        return Text("no trades yet", style="dim italic")
    table = Table(box=None, show_header=True, header_style="dim", expand=True)
    table.add_column("time", no_wrap=True)
    table.add_column("buyer", no_wrap=True)
    table.add_column("seller", no_wrap=True)
    table.add_column("item", overflow="ellipsis")
    table.add_column("price", justify="right", no_wrap=True)
    table.add_column("tax", justify="right", no_wrap=True)
    table.add_column("payout", justify="right", no_wrap=True)
    ordered = sorted(trades, key=lambda trade: _as_float(trade.get("ts")), reverse=True)
    for trade in ordered[:rows]:
        table.add_row(
            str(trade.get("time") or _clock(trade.get("ts"))),
            str(trade.get("buyer") or "?"),
            str(trade.get("seller") or "?"),
            str(trade.get("item") or "?"),
            str(trade.get("price", "-")),
            str(trade.get("tax", "-")),
            str(trade.get("payout", "-")),
        )
    return table


def _by_item_table(market):
    orders = [o for o in (market.get("orders") or []) if isinstance(o, dict)]
    bids = [b for b in (market.get("bids") or []) if isinstance(b, dict)]
    fills = [f for f in (market.get("history") or []) if isinstance(f, dict)]
    items = sorted(
        {str(entry.get("item")) for entry in orders + bids + fills if entry.get("item")}
    )
    if not items:
        return Text("no market items yet", style="dim italic")
    table = Table(box=None, show_header=True, header_style="dim", expand=True)
    table.add_column("item", overflow="ellipsis")
    table.add_column("low ask", justify="right", no_wrap=True)
    table.add_column("high bid", justify="right", no_wrap=True)
    table.add_column("spread", justify="right", no_wrap=True)
    table.add_column("median", justify="right", no_wrap=True)
    table.add_column("volume", justify="right", no_wrap=True)
    for item in items:
        asks = [_as_float(o.get("price")) for o in orders if str(o.get("item")) == item]
        bid_prices = [
            _as_float(b.get("price")) for b in bids if str(b.get("item")) == item
        ]
        prices = [
            _as_float(f.get("price")) for f in fills if str(f.get("item")) == item
        ]
        low_ask = min(asks) if asks else None
        high_bid = max(bid_prices) if bid_prices else None
        gap = None
        if low_ask is not None and high_bid is not None:
            gap = low_ask - high_bid
        table.add_row(
            item,
            _gold(low_ask),
            _gold(high_bid),
            _gold(gap),
            _gold(_median(prices)),
            str(len(prices)),
        )
    return table


def _median(values):
    if not values:
        return None
    ordered = sorted(values)
    middle = len(ordered) // 2
    if len(ordered) % 2:
        return ordered[middle]
    return (ordered[middle - 1] + ordered[middle]) / 2


def _gold(value):
    return "-" if value is None else f"{_as_float(value):.0f}g"


def _clock(ts):
    value = _as_float(ts)
    if not value:
        return "-"
    try:
        return time.strftime("%H:%M:%S", time.localtime(value))
    except (OSError, OverflowError, ValueError):
        return "-"


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
