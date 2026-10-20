"""Panel rendering for the terminal UI (#319).

One function per dashboard tab, dispatched by tab id. Overview is the
TUI-1 proving panel; World, Players and Market land in TUI-2 (#491);
Dungeon, Agents, Quests and Crafting land in TUI-3 (#492). Each mirrors
the matching dashboard renderers -- health/trend/activity for Overview,
rooms/history/activity for World, performance/scores/tracker for Players,
treasury/book/trades/by-item for Market, buffs/bosses/clears/instances for
Dungeon, roster/action feed for Agents, catalog/commissions for Quests,
recipes/history/supply/flow for Crafting -- reading the same snapshot
fields. The remaining tabs render an explicit placeholder until their
panels land; the dispatch below is the seam the rest of the #319
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
    if tab == "dungeon":
        return render_dungeon(snapshot, rows)
    if tab == "agents":
        return render_agents(snapshot, rows)
    if tab == "quests":
        return render_quests(snapshot, rows)
    if tab == "crafting":
        return render_crafting(snapshot, rows)
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


def _table(columns):
    """Header-only rich table: no box, dim headers, expanded to width."""
    table = Table(box=None, show_header=True, header_style="dim", expand=True)
    for label, kwargs in columns:
        table.add_column(label, **kwargs)
    return table


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


# --- dungeon -------------------------------------------------------------
def render_dungeon(snapshot, rows=ACTIVITY_ROWS):
    """Dungeon tab: active buffs, bosses, lifetime clears, open instances."""
    return Group(
        Text("Buffs", style="bold"),
        _buffs(snapshot),
        Text(""),
        Text("Bosses", style="bold"),
        _bosses(snapshot),
        Text(""),
        Text("Dungeon clears", style="bold"),
        _dungeon_clears(snapshot),
        Text(""),
        Text("Dungeons", style="bold"),
        _dungeons(snapshot),
    )


def _mmss(seconds):
    total = int(_as_float(seconds))
    minutes, secs = divmod(total, 60)
    return f"{minutes}:{secs:02d}"


def _buffs(snapshot):
    buffs = snapshot.get("buffs") or {}
    if not buffs:
        return Text("no active buffs", style="dim italic")
    parts = []
    for key in ("xp", "gold"):
        remaining = _as_float(buffs.get(key))
        label = f"{key.upper()}x2"
        if remaining > 0:
            parts.append(f"{label} {_mmss(remaining)} left")
        else:
            parts.append(f"{label} inactive")
    return Text(" | ".join(parts))


def _bosses(snapshot):
    bosses = [b for b in (snapshot.get("bosses") or []) if isinstance(b, dict)]
    if not bosses:
        return Text("no bosses alive", style="dim italic")
    table = _table(
        [
            ("name", {"no_wrap": True}),
            ("room", {"overflow": "ellipsis", "style": "dim"}),
            ("hp", {"justify": "right", "no_wrap": True}),
            ("attack", {"justify": "right", "no_wrap": True}),
        ]
    )
    for boss in sorted(bosses, key=_boss_fraction, reverse=True):
        table.add_row(
            str(boss.get("name") or "?"),
            str(boss.get("room") or "-"),
            f"{_as_float(boss.get('hp')):.0f}/{_as_float(boss.get('max_hp')):.0f}",
            str(int(_as_float(boss.get("attack")))),
        )
    return table


def _boss_fraction(boss):
    max_hp = _as_float(boss.get("max_hp"))
    return _as_float(boss.get("hp")) / max_hp if max_hp else 0.0


def _dungeon_clears(snapshot):
    clears = snapshot.get("dungeon_clears") or {}
    cards = (
        ("solo clears", str(int(_as_float(clears.get("solo_clears"))))),
        ("group clears", str(int(_as_float(clears.get("group_clears"))))),
        ("solo max floor", str(int(_as_float(clears.get("solo_max_floor"))))),
        ("group max floor", str(int(_as_float(clears.get("group_max_floor"))))),
    )
    grid = Table.grid(padding=(0, 2))
    for _ in cards:
        grid.add_column(justify="right")
    grid.add_row(*[Text(value, style="bold") for _, value in cards])
    grid.add_row(*[Text(label, style="dim") for label, _ in cards])
    return grid


def _dungeons(snapshot):
    dungeons = [d for d in (snapshot.get("dungeons") or []) if isinstance(d, dict)]
    if not dungeons:
        return Text("no dungeons opened yet", style="dim italic")
    table = _table(
        [
            ("id", {"no_wrap": True}),
            ("party", {"overflow": "ellipsis"}),
            ("max floor", {"justify": "right", "no_wrap": True}),
            ("floors", {"overflow": "fold"}),
        ]
    )
    for dungeon in sorted(dungeons, key=_dungeon_floor, reverse=True):
        party = ", ".join(str(name) for name in (dungeon.get("party") or [])) or "solo"
        table.add_row(
            str(dungeon.get("id") or "?"),
            party,
            str(int(_as_float(dungeon.get("max_floor_reached")))),
            _floor_states(dungeon.get("floors")),
        )
    return table


def _dungeon_floor(dungeon):
    return _as_float(dungeon.get("max_floor_reached"))


def _floor_states(floors):
    parts = []
    for floor in floors or []:
        if not isinstance(floor, dict):
            continue
        state = "cleared" if floor.get("cleared") else "sealed"
        parts.append(
            f"F{floor.get('floor', '?')} "
            f"{floor.get('guards_alive', 0)}/{floor.get('guards_total', 0)} {state}"
        )
    return "; ".join(parts) or "floor 1 not generated yet"


# --- agents --------------------------------------------------------------
def render_agents(snapshot, rows=ACTIVITY_ROWS):
    """Agents tab: the roster plus a merged, newest-first action feed."""
    return Group(
        Text("Agents", style="bold"),
        _agents_table(snapshot),
        Text(""),
        Text("Agent activity", style="bold"),
        _agent_feed(snapshot, rows),
    )


def _agents_table(snapshot):
    players = [p for p in (snapshot.get("players") or []) if isinstance(p, dict)]
    if not players:
        return Text("no agents online", style="dim italic")
    table = _table(
        [
            ("#", {"justify": "right", "no_wrap": True}),
            ("name", {"no_wrap": True}),
            ("lv", {"justify": "right", "no_wrap": True}),
            ("score", {"justify": "right", "no_wrap": True}),
            ("kills", {"justify": "right", "no_wrap": True}),
            ("deaths", {"justify": "right", "no_wrap": True}),
            ("gold", {"justify": "right", "no_wrap": True}),
            ("hp", {"justify": "right", "no_wrap": True}),
            ("room", {"overflow": "ellipsis", "style": "dim"}),
            ("last action", {"overflow": "ellipsis"}),
        ]
    )
    for rank, player in enumerate(
        sorted(players, key=_agent_score, reverse=True), start=1
    ):
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
            str(player.get("last_action") or "-"),
        )
    return table


def _agent_score(player):
    return _as_float(player.get("score"))


def _agent_feed(snapshot, rows=ACTIVITY_ROWS):
    feed = []
    for player in snapshot.get("players") or []:
        if not isinstance(player, dict):
            continue
        for action in player.get("recent_actions") or []:
            if isinstance(action, dict):
                feed.append({**action, "who": player.get("name") or "?"})
    if not feed:
        return Text("no tracked actions yet", style="dim italic")
    table = _table(
        [
            ("time", {"no_wrap": True}),
            ("player", {"no_wrap": True}),
            ("cmd", {"no_wrap": True, "style": "magenta"}),
            ("detail", {"overflow": "ellipsis", "style": "dim"}),
        ]
    )
    ordered = sorted(feed, key=_action_seq, reverse=True)
    for action in ordered[:rows]:
        table.add_row(
            str(action.get("time") or "-"),
            str(action.get("who") or "?"),
            str(action.get("cmd") or "-"),
            str(action.get("detail") or ""),
        )
    return table


def _action_seq(action):
    return _as_float(action.get("seq"))


# --- quests --------------------------------------------------------------
def render_quests(snapshot, rows=ACTIVITY_ROWS):
    """Quests tab: catalog, turn-in feed, commissions, commission history."""
    return Group(
        Text("Quests", style="bold"),
        _quest_summary(snapshot),
        Text(""),
        _quest_catalog(snapshot),
        Text(""),
        Text("Recent turn-ins", style="bold"),
        _quest_feed(snapshot, rows),
        Text(""),
        Text("Commissions", style="bold"),
        _commissions(snapshot),
        Text(""),
        Text("Commission history", style="bold"),
        _commission_history(snapshot, rows),
    )


def _quest_summary(snapshot):
    quests = snapshot.get("quests") or {}
    active = quests.get("active") or {}
    cards = (
        ("active", str(int(sum(_as_float(v) for v in active.values())))),
        ("turn-ins last min", str(int(_as_float(quests.get("turnins_last_min"))))),
    )
    grid = Table.grid(padding=(0, 2))
    for _ in cards:
        grid.add_column(justify="right")
    grid.add_row(*[Text(value, style="bold") for _, value in cards])
    grid.add_row(*[Text(label, style="dim") for label, _ in cards])
    return grid


def _quest_catalog(snapshot):
    quests = snapshot.get("quests") or {}
    catalog = [c for c in (quests.get("catalog") or []) if isinstance(c, dict)]
    active = quests.get("active") or {}
    completions = quests.get("completions") or {}
    if not catalog:
        return Text("no quests in catalog", style="dim italic")
    table = _table(
        [
            ("quest", {"no_wrap": True}),
            ("brief", {"overflow": "ellipsis"}),
            ("giver", {"no_wrap": True}),
            ("room", {"overflow": "ellipsis", "style": "dim"}),
            ("reward", {"no_wrap": True}),
            ("active", {"justify": "right", "no_wrap": True}),
            ("done", {"justify": "right", "no_wrap": True}),
        ]
    )
    for quest in sorted(catalog, key=lambda c: _active_count(c, active), reverse=True):
        qid = str(quest.get("id") or "?")
        table.add_row(
            qid,
            str(quest.get("brief") or ""),
            str(quest.get("giver_name") or "-"),
            str(quest.get("room") or "-"),
            f"{int(_as_float(quest.get('reward_xp')))}xp"
            f" + {int(_as_float(quest.get('reward_gold')))}g",
            str(int(_as_float(active.get(qid)))),
            str(int(_as_float(completions.get(qid)))),
        )
    return table


def _active_count(quest, active):
    return _as_float(active.get(str(quest.get("id"))))


def _quest_feed(snapshot, rows=ACTIVITY_ROWS):
    quests = snapshot.get("quests") or {}
    turnins = [t for t in (quests.get("recent_turnins") or []) if isinstance(t, dict)]
    if not turnins:
        return Text("no turn-ins recorded yet", style="dim italic")
    table = _table(
        [
            ("time", {"no_wrap": True}),
            ("player", {"no_wrap": True}),
            ("quest", {"no_wrap": True, "style": "magenta"}),
        ]
    )
    ordered = sorted(turnins, key=_action_seq, reverse=True)
    for turnin in ordered[:rows]:
        table.add_row(
            str(turnin.get("t") or "-"),
            str(turnin.get("name") or "?"),
            str(turnin.get("qid") or "?"),
        )
    return table


def _commissions(snapshot):
    commissions = [
        c for c in (snapshot.get("commissions") or []) if isinstance(c, dict)
    ]
    if not commissions:
        return Text("no commissions posted", style="dim italic")
    table = _table(
        [
            ("id", {"no_wrap": True}),
            ("target", {"no_wrap": True}),
            ("progress", {"no_wrap": True}),
            ("reward", {"no_wrap": True}),
            ("poster", {"no_wrap": True}),
            ("status", {"no_wrap": True}),
            ("age", {"justify": "right", "no_wrap": True}),
        ]
    )
    for commission in sorted(commissions, key=_commission_id, reverse=True):
        progress = commission.get("progress") or {}
        required = int(
            _as_float(progress.get("required") or commission.get("required_kills"))
        )
        kills = int(_as_float(progress.get("kills")))
        leader = progress.get("leader")
        shown = f"{kills}/{required}" + (f" {leader}" if leader else "")
        table.add_row(
            f"#{commission.get('id', '?')}",
            str(commission.get("target") or "-"),
            shown,
            f"{int(_as_float(commission.get('reward_gold')))}g"
            f" + {int(_as_float(commission.get('reward_xp')))}xp",
            str(commission.get("poster") or "-"),
            str(commission.get("status") or "-"),
            _age(commission.get("created_ts")),
        )
    return table


def _commission_id(commission):
    return _as_float(commission.get("id"))


def _commission_history(snapshot, rows=ACTIVITY_ROWS):
    history = [
        h for h in (snapshot.get("commission_history") or []) if isinstance(h, dict)
    ]
    if not history:
        return Text("no commission history yet", style="dim italic")
    table = _table(
        [
            ("seq", {"justify": "right", "no_wrap": True}),
            ("time", {"no_wrap": True}),
            ("id", {"no_wrap": True}),
            ("filler", {"no_wrap": True}),
            ("payout", {"no_wrap": True}),
            ("status", {"no_wrap": True}),
        ]
    )
    ordered = sorted(history, key=_action_seq, reverse=True)
    for entry in ordered[:rows]:
        table.add_row(
            str(int(_as_float(entry.get("seq")))),
            str(entry.get("t") or "-"),
            f"#{entry.get('id', '?')} {entry.get('target') or ''}".strip(),
            str(entry.get("filler") or "-"),
            _commission_payout(entry),
            str(entry.get("status") or "-"),
        )
    return table


def _commission_payout(entry):
    if entry.get("status") == "cancelled":
        return f"refund {entry.get('refund', 0)}g / forfeit {entry.get('forfeit', 0)}g"
    return f"{entry.get('gold', 0)}g + {entry.get('xp', 0)}xp"


def _age(ts):
    value = _as_float(ts)
    if not value:
        return "-"
    seconds = max(0, int(time.time() - value))
    if seconds < 60:
        return f"{seconds}s"
    if seconds < 3600:
        return f"{seconds // 60}m"
    if seconds < 86400:
        return f"{seconds // 3600}h"
    return f"{seconds // 86400}d"


# --- crafting ------------------------------------------------------------
FLOW_COMMANDS = (
    ("gather", ("gather",)),
    ("craft", ("craft",)),
    ("take", ("take",)),
    ("buy", ("buy", "market_buy")),
    ("sell", ("sell", "market_post")),
)


def render_crafting(snapshot, rows=ACTIVITY_ROWS):
    """Crafting tab: recipes, craft history, supply/demand, command flow."""
    return Group(
        Text("Recipes", style="bold"),
        _recipes(snapshot),
        Text(""),
        Text("Craft history", style="bold"),
        _craft_history(snapshot, rows),
        Text(""),
        Text("Material supply", style="bold"),
        _material_supply(snapshot),
        Text(""),
        Text("Listed supply", style="bold"),
        _listed_supply(snapshot),
        Text(""),
        Text("Command flow", style="bold"),
        _command_flow(snapshot),
    )


def _recipes(snapshot):
    recipes = [r for r in (snapshot.get("recipes") or []) if isinstance(r, dict)]
    if not recipes:
        return Text("no recipes known", style="dim italic")
    table = _table(
        [
            ("result", {"no_wrap": True}),
            ("tier", {"justify": "right", "no_wrap": True}),
            ("category", {"no_wrap": True, "style": "dim"}),
            ("inputs", {"overflow": "fold"}),
        ]
    )
    for recipe in sorted(recipes, key=lambda r: str(r.get("result") or "")):
        result = str(recipe.get("result") or "?")
        qty = int(_as_float(recipe.get("result_qty"), 1))
        if qty > 1:
            result = f"{result} x{qty}"
        table.add_row(
            result,
            str(recipe.get("tier") or "-"),
            str(recipe.get("category") or "-"),
            _recipe_inputs(recipe.get("inputs")),
        )
    return table


def _recipe_inputs(inputs):
    parts = []
    for item in inputs or []:
        if not isinstance(item, dict):
            continue
        parts.append(f"{item.get('qty', 0)}x {item.get('item', '?')}")
    return ", ".join(parts) or "-"


def _craft_history(snapshot, rows=ACTIVITY_ROWS):
    history = [h for h in (snapshot.get("craft_history") or []) if isinstance(h, dict)]
    if not history:
        return Text("no crafts yet", style="dim italic")
    table = _table(
        [
            ("seq", {"justify": "right", "no_wrap": True}),
            ("time", {"no_wrap": True}),
            ("player", {"no_wrap": True}),
            ("recipe", {"no_wrap": True}),
            ("result", {"no_wrap": True}),
        ]
    )
    ordered = sorted(history, key=_action_seq, reverse=True)
    for entry in ordered[:rows]:
        table.add_row(
            str(int(_as_float(entry.get("seq")))),
            str(entry.get("t") or "-"),
            str(entry.get("name") or "?"),
            str(entry.get("recipe") or "?"),
            str(entry.get("result") or "?"),
        )
    return table


def _material_supply(snapshot):
    supply = {}
    rooms_of = {}
    for room in snapshot.get("rooms") or []:
        if not isinstance(room, dict):
            continue
        for item in room.get("items") or []:
            name = str(item)
            supply[name] = supply.get(name, 0) + 1
            rooms_of.setdefault(name, set()).add(str(room.get("name") or "?"))
    if not supply:
        return Text("no loose materials on the ground", style="dim italic")
    table = _table(
        [
            ("item", {"overflow": "ellipsis"}),
            ("count", {"justify": "right", "no_wrap": True}),
            ("rooms", {"overflow": "fold", "style": "dim"}),
        ]
    )
    for item, count in sorted(supply.items(), key=_count_desc)[:15]:
        table.add_row(item, str(count), ", ".join(sorted(rooms_of[item])))
    return table


def _listed_supply(snapshot):
    market = snapshot.get("market") or {}
    orders = [o for o in (market.get("orders") or []) if isinstance(o, dict)]
    if not orders:
        return Text("no listed supply", style="dim italic")
    items = {}
    for order in orders:
        name = str(order.get("item") or "?")
        entry = items.setdefault(name, {"count": 0, "min": None})
        entry["count"] += 1
        price = _as_float(order.get("price"))
        if entry["min"] is None or price < entry["min"]:
            entry["min"] = price
    table = _table(
        [
            ("item", {"overflow": "ellipsis"}),
            ("orders", {"justify": "right", "no_wrap": True}),
            ("low ask", {"justify": "right", "no_wrap": True}),
        ]
    )
    ordered = sorted(items.items(), key=lambda pair: pair[1]["count"], reverse=True)
    for item, entry in ordered[:15]:
        table.add_row(item, str(entry["count"]), _gold(entry["min"]))
    return table


def _count_desc(pair):
    return pair[1]


def _command_flow(snapshot):
    lookup = {}
    for label, names in FLOW_COMMANDS:
        for name in names:
            lookup[name] = label
    counts = {label: 0 for label, _ in FLOW_COMMANDS}
    for entry in snapshot.get("activity") or []:
        if not isinstance(entry, dict):
            continue
        label = lookup.get(str(entry.get("cmd") or "").lower())
        if label:
            counts[label] += 1
    grid = Table.grid(padding=(0, 2))
    for _ in FLOW_COMMANDS:
        grid.add_column(justify="right")
    grid.add_row(
        *[Text(str(counts[label]), style="bold") for label, _ in FLOW_COMMANDS]
    )
    grid.add_row(*[Text(label, style="dim") for label, _ in FLOW_COMMANDS])
    return grid


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
