"""Dashboard contract tests (no server needed).

Run from the repo root:  python tests/test_dashboard.py
Covers #207: the canonical dashboard.html (promoted from dashboard2.html
in the swap) keeps every tab/view pairing, every $("id") referenced in JS
exists in markup, the default landing is Overview, and the snapshot-server
markers the client depends on are present. Old dashboard.html retired with
the swap; its blocks were removed, not ported.
"""
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# --- canonical dashboard contract (#207 swap of the #205 revamp) ---
html = open(os.path.join(ROOT, "dashboard.html"), encoding="utf-8").read()
tabs = re.findall(r'data-tab="([\w-]+)"', html)
views = re.findall(r'id="(view-[\w-]+)"', html)
assert tabs, "no tabs found"
for tab in tabs:
    assert f"view-{tab}" in views, f"tab {tab!r} has no view div"
ids = set(re.findall(r'id="([\w-]+)"', html))
used = set(re.findall(r'\$\("([\w-]+)"\)', html))
assert not (used - ids), f"JS references missing ids: {sorted(used - ids)}"
m = re.search(r'<div class="tab active" data-tab="([\w-]+)"', html)
assert m and m.group(1) == "overview", "default tab is not overview"
# Vendored chart lib referenced (served by the dashboard HTTP handler).
assert '<script src="/uplot.min.js">' in html, "uplot script tag missing"
# Design tokens: spacing/type scales + font + radius on top of legacy vars.
for tok in ("--sp-md", "--fs-md", "--font", "--r-md"):
    assert tok in html, f"token {tok} missing"
# Market/Dungeons/GM (2/4) and Agents/Quests/Crafting (3/4) are
# functional; only Config still points at its follow-up (#63).
assert "revamp 2/4 (#206)" not in html, "market/dungeon/gm still placeholder"
assert "revamp 3/4 (#209)" not in html, "agents/quests/crafting still placeholder"
assert "(#63)" in html, "config editor pointer missing"
for wid in ("m-treasury", "orders", "tradeHistory", "m-priceHist",
             "buffs", "bosses", "dungeons", "gmlog", "gm-send-gold",
             "gm-action", "gm-players", "gm-rooms",
             "agents", "noAgents", "agentActivity",
             "q-active", "q-turnins", "questCatalog",
             "matSupply", "noSupply", "matOrders", "noMatOrders",
             "flow-gather", "flow-craft", "flow-take", "flow-buy", "flow-sell",
             "configRows", "themeToggle"):
    assert f'id="{wid}"' in html, f"missing widget {wid}"
# Players tab split out of World (more tabs, less content each).
assert '<div class="tab" data-tab="players">Players</div>' in html
assert '<div id="view-players" class="tabview">' in html
# North-up map: compass deltas put north exits above their source.
assert "north: [0, -1]" in html and "south: [0, 1]" in html
# No forced horizontal scroll: the 720px canvas floor is gone.
assert "min-width: 720px" not in html
# Tiles hold still: reserved widths + tabular numerals.
assert "tabular-nums" in html and "min-height: 120px" in html
# Round 2 follow-ups: no hardcoded dark surfaces remain (GM black stripe
# was #gmlog's #0b0f13; the map canvas fill is token-read in JS now).
assert "background: #0b0f13" not in html
assert 'background: var(--panel2)' in html
# Tiles can never overlap: the world fits its container width with tiles
# scaled down as cells narrow (round 3), and the layout returns world
# dims the canvas sizes itself from.
assert "worldW, worldH" in html and "tileW, tileH" in html
# Charts: empty states render axes (no values.length early-out), themed
# palette keys at every call site, refit helper + tab-switch hook.
assert "!values.length" not in html and "history.length < 2" not in html
assert "hist.length >= 2" not in html
for key in ('"blue"', '"purple"', '"amber"'):
    assert key in html, f"palette key {key} missing"
assert "requestAnimationFrame(resizeCharts)" in html
# Containment: grid blowout kill, table scroll regions, capped roster,
# block canvas in trends.
for tok in (".grid > * { min-width: 0; }", ".tscroll.cap", ".trend canvas"):
    assert tok in html, f"containment {tok} missing"
# Quest turn-in feed + recipe browser widgets present.
for wid in ("questFeed", "recipeSearch", "recipeRows", "noRecipes"):
    assert f'id="{wid}"' in html, f"missing widget {wid}"
# Round 3: uPlot layers are absolutely positioned (the stacked-layers +
# overflow clip blanked every chart); the world fits its container with
# scaled tiles instead of growing+scrolling.
assert ".trend .u-under" in html and "position: absolute" in html
assert "availW" in html and "tileW, tileH" in html
assert "TILE_W + TILE_GAP" not in html, "map still grows+scrolls"
# Crafting order: flow first, recipe browser last.
assert html.index("Recent flow") < html.index("Recipe browser")
# Indoor/outdoor legend + full shelter coverage in world.json.
assert "solid tile = indoor, dashed = outdoor" in html
# Spawn-border fix: outdoor-home tile carries accent AND dash.
assert "ctx.setLineDash(outdoor ? [5, 4] : []);" in html
import json as _json, os as _os
_world = _json.load(open(_os.path.join(_os.path.dirname(__file__), "..", "world.json")))
assert len(_world["rooms"]) == 32
assert all(isinstance(r.get("shelter"), bool) for r in _world["rooms"].values())
# Settled-price panel renamed to plain words.
assert "Price per completed sale" in html
assert "Settled-price history" not in html
# Split-lane live feel: SSE stream + shared row helper + start_ts ticker.
for tok in ("EventSource", "/api/activity/stream", "activityRow",
            "actMaxSeq", "start_ts", "startTs"):
    assert tok in html, f"split-lane token {tok} missing"
# /OC robustness: guarded scores/servers/cmdClass, seq sort, empty states.
assert ".score.toFixed" not in html, "unguarded toFixed survives"
assert "s.server.players_online" not in html, "unguarded s.server survives"
assert "cmd-\" + (cmd" in html or 'cmd-" + (cmd' in html
assert "a.seq ?? -1" in html and "localeCompare" in html
assert "No scores recorded yet." in html
# Server lane: seq field, fan-out registry, SSE route, start timestamp.
import re as _re
_srv = open(_os.path.join(_os.path.dirname(__file__), "..", "server.py")).read()
for tok in ('"seq": activity_seq', "activity_subscribers",
            "text/event-stream", '"start_ts": START_TIME'):
    assert tok in _srv, f"server: split-lane token {tok} missing"
# Shelter veto: market is outdoor.
assert _world["rooms"]["market"]["shelter"] is False
# Quest panel states the full turn-in chain, not just the brief.
assert "turn-in:" in html and "hand it over" in html
# Chart readouts: always-visible current value per graph.
for wid in ("ovPlayersVal", "ovScoreVal", "ovVolumeVal",
            "histPlayersVal", "histScoreVal", "m-priceHistVal"):
    assert f'id="{wid}"' in html, f"missing readout {wid}"
assert ".trendval" in html and "setTrendVal" in html
# Sort rule: every data table sortable + div-list controls present.
for tid in ("perf", "agents", "orders", "tradeHistory", "bosses",
            "matSupply", "matOrders", "questCatalog", "recipeRows",
            "steamOrders"):
    assert f'data-sort="{tid}"' in html, f"table {tid} not sortable"
for wid in ("questFeedSort", "agentActivitySort", "activitySort",
            "dungeonSort"):
    assert f'id="{wid}"' in html, f"missing sort control {wid}"
assert "function sortRows" in html and "bindSortTables()" in html
# Recipe spec: ingredients column last, sortable result/tier/category.
ri = html.index('data-sort="recipeRows"')
assert ri < html.index("<th>tier</th>") < html.index("<th>category</th>")
assert html.index("<th>category</th>") < html.index("<th>ingredients</th>")
# Steam-market panel: picker, range, stats, ladder, orders, median/volume.
for wid in ("steamItem", "steamRange", "st-lowask", "st-med", "st-vol",
            "st-chart", "st-ladder", "st-ladderHead", "steamOrders",
            "noSteamOrders"):
    assert f'id="{wid}"' in html, f"missing steam widget {wid}"
assert "uPlot.paths.bars" in html and "Item market" in html
# Hover readouts: cursor + setCursor hook + timestamps on every chart.
assert html.count("setCursor") >= 2, "hover hooks missing"
assert "fmtTs" in html and ".u-cursor-x" in html
# Theme micro-fix: toggle invalidates the map key AND repaints at once.
assert 'lastMapKey = "";' in html
i = html.index('$("themeToggle").addEventListener')
assert "renderAll(lastState)" in html[i:i + 1200], "toggle does not repaint at once"
assert "drag: {x: false, y: false}" in html
assert 'id="st-tip"' in html
# Commissions board: Quests-adjacent sortable panel + snapshot key.
for wid in ("commissions", "noCommissions"):
    assert f'id="{wid}"' in html, f"missing commissions widget {wid}"
assert 'data-sort="commissions"' in html
assert "renderCommissions" in html
assert "<th>posted</th>" in html and "fmtAge" in html
for tok in ('"commissions": _commission_snapshot()', "def _commission_snapshot",
            '"created_ts": c.get("created_ts", 0)'):
    assert tok in _srv, f"server: commissions token {tok} missing"
# PERF pass: guarded DOM writes, chart/map repaint skips, activity cap,
# throttled poll loop with hidden-tab slowdown.
assert "setInterval(tick" not in html, "1s setInterval loop still present"
for tok in ("function setHTML", "lastMapKey", "_dataKey", "slice(-80)",
            "visibilitychange", "schedulePoll", "10000 : 2000"):
    assert tok in html, f"perf token {tok} missing"
# Trends moved to uPlot: no canvas line-chart helper may remain.
assert "drawLineChart" not in html, "legacy canvas charts still present"
# #211 regression pin: sections must receive the snapshot (render(s)).
assert re.search(r"try \{\s*render\(s\);", html), "renderAll drops s"
print("DASHBOARD_OK")

# --- renderMarket tolerates partial snapshots (#246) ---
rm = re.search(r"function renderMarket\(m\) \{(.*?)\n\}\n", html, re.DOTALL)
assert rm, "renderMarket not found"
body = rm.group(1)
assert "m.treasury ?? 0" in body or "treasury ?? 0" in body, "treasury default missing"
assert "m.collected_lifetime ?? 0" in body or "collected_lifetime ?? 0" in body
assert "m.trade_count ?? 0" in body or "trade_count ?? 0" in body
assert "m.orders || []" in body or "orders || []" in body
print("MARKET_DEFAULTS_OK")

# --- GM console follows the page origin, not hardcoded loopback (#236) ---
gm = re.search(r"function gmOpen\(\) \{(.*?)\n\}\n", html, re.DOTALL)
assert gm, "gmOpen not found"
assert "location.hostname" in gm.group(1), "gmOpen ignores page origin"
assert "ws://127.0.0.1" not in gm.group(1), "gmOpen still hardcodes loopback"
print("GM_ORIGIN_OK")

print("ALL_DASHBOARD_OK")
