"""Dashboard markup/JS consistency tests (no server needed).

Run from the repo root:  python tests/test_dashboard.py
Covers #127: every tab has a view, every $("id") referenced in JS exists
in markup, the default landing is Overview, and placeholder panels point
at their follow-up issues.
"""
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
html = open(os.path.join(ROOT, "dashboard.html"), encoding="utf-8").read()

# --- every data-tab has a matching view div ---
tabs = re.findall(r'data-tab="([\w-]+)"', html)
views = re.findall(r'id="(view-[\w-]+)"', html)
assert tabs, "no tabs found"
for tab in tabs:
    assert f"view-{tab}" in views, f"tab {tab!r} has no view div"
print(f"TABS_OK ({len(tabs)} tabs, {len(views)} views)")

# --- every $("literal") referenced in JS exists in markup ---
ids = set(re.findall(r'id="([\w-]+)"', html))
used = set(re.findall(r'\$\("([\w-]+)"\)', html))
missing = used - ids
assert not missing, f"JS references missing ids: {sorted(missing)}"
print(f"IDS_OK ({len(used)} referenced ids all present)")

# --- default landing is Overview ---
m = re.search(r'<div class="tab active" data-tab="([\w-]+)"', html)
assert m and m.group(1) == "overview", "default tab is not overview"
m = re.search(r'<div id="(view-[\w-]+)" class="tabview active"', html)
assert m and m.group(1) == "view-overview", "default view is not overview"
print("DEFAULT_TAB_OK")

# --- Phase 2 widgets exist: health pill, volume chart, canvas map, drawer ---
for wid in ("ovHealth", "ovVolume", "worldMapCanvas", "roomDrawer"):
    assert f'id="{wid}"' in html, f"missing widget {wid}"
print("PHASE2_WIDGETS_OK")

# --- placeholder panels point at follow-up work (no bare TODOs) ---
for view in ("agents", "quests", "crafting", "config"):
    block = re.search(rf'<div id="view-{view}" class="tabview">(.*?)</div>\s*</div>',
                      html, re.DOTALL)
    assert block, f"view-{view} block not found"
    assert re.search(r"#\d+", block.group(1)), f"view-{view} has no issue ref"
stray = [line for line in html.splitlines()
          if re.search(r"TODO|FIXME|XXX", line) and "coming in" not in line.lower()
          and "lands here" not in line.lower()]
assert not stray, f"stray markers: {stray[:3]}"
print("PLACEHOLDERS_OK")

# --- #76: metric/config labels carry hover tooltips (range + impact) ---
titled = re.findall(r'title="([^"]*)"', html)
assert len(titled) >= 40, f"too few tooltips: {len(titled)}"
assert not re.search(r'title=""', html), "empty title attribute"
assert all(len(t) >= 20 for t in titled), "tooltip too short to explain range/impact"
for probe in ('<div class="l" title="GM treasury',
               '<label for="gm-action" title="',
               "CARD_TIPS"):
    assert probe in html, f"tooltip probe missing: {probe[:40]}"
print(f"TOOLTIPS_OK ({len(titled)} titles, all non-empty)")

# --- dashboard2.html revamp contract (#205): same invariants, v2 specifics ---
html2 = open(os.path.join(ROOT, "dashboard2.html"), encoding="utf-8").read()
tabs2 = re.findall(r'data-tab="([\w-]+)"', html2)
views2 = re.findall(r'id="(view-[\w-]+)"', html2)
assert tabs2, "v2: no tabs found"
for tab in tabs2:
    assert f"view-{tab}" in views2, f"v2: tab {tab!r} has no view div"
ids2 = set(re.findall(r'id="([\w-]+)"', html2))
used2 = set(re.findall(r'\$\("([\w-]+)"\)', html2))
assert not (used2 - ids2), f"v2: JS references missing ids: {sorted(used2 - ids2)}"
m = re.search(r'<div class="tab active" data-tab="([\w-]+)"', html2)
assert m and m.group(1) == "overview", "v2: default tab is not overview"
# Vendored chart lib referenced (served by the dashboard HTTP handler).
assert '<script src="/uplot.min.js">' in html2, "v2: uplot script tag missing"
# Design tokens: spacing/type scales + font + radius on top of legacy vars.
for tok in ("--sp-md", "--fs-md", "--font", "--r-md"):
    assert tok in html2, f"v2: token {tok} missing"
# Market/Dungeons/GM (2/4) and Agents/Quests/Crafting (3/4) are
# functional; only Config still points at its follow-up (#63).
assert "revamp 2/4 (#206)" not in html2, "v2: market/dungeon/gm still placeholder"
assert "revamp 3/4 (#209)" not in html2, "v2: agents/quests/crafting still placeholder"
assert "(#63)" in html2, "v2: config editor pointer missing"
for wid in ("m-treasury", "orders", "tradeHistory", "m-priceHist",
             "buffs", "bosses", "dungeons", "gmlog", "gm-send-gold",
             "gm-action", "gm-players", "gm-rooms",
             "agents", "noAgents", "agentActivity",
             "q-active", "q-turnins", "questCatalog",
             "matSupply", "noSupply", "matOrders", "noMatOrders",
             "flow-gather", "flow-craft", "flow-take", "flow-buy", "flow-sell",
             "configRows", "themeToggle"):
    assert f'id="{wid}"' in html2, f"v2: missing widget {wid}"
# Players tab split out of World (more tabs, less content each).
assert '<div class="tab" data-tab="players">Players</div>' in html2
assert '<div id="view-players" class="tabview">' in html2
# North-up map: compass deltas put north exits above their source.
assert "north: [0, -1]" in html2 and "south: [0, 1]" in html2
# No forced horizontal scroll: the 720px canvas floor is gone.
assert "min-width: 720px" not in html2
# Tiles hold still: reserved widths + tabular numerals.
assert "tabular-nums" in html2 and "min-height: 120px" in html2
# Round 2 follow-ups: no hardcoded dark surfaces remain (GM black stripe
# was #gmlog's #0b0f13; the map canvas fill is token-read in JS now).
assert "background: #0b0f13" not in html2
assert 'background: var(--panel2)' in html2
# Tiles can never overlap: the world fits its container width with tiles
# scaled down as cells narrow (round 3), and the layout returns world
# dims the canvas sizes itself from.
assert "worldW, worldH" in html2 and "tileW, tileH" in html2
# Charts: empty states render axes (no values.length early-out), themed
# palette keys at every call site, refit helper + tab-switch hook.
assert "!values.length" not in html2 and "history.length < 2" not in html2
assert "hist.length >= 2" not in html2
for key in ('"blue"', '"purple"', '"amber"'):
    assert key in html2, f"v2: palette key {key} missing"
assert "requestAnimationFrame(resizeCharts)" in html2
# Containment: grid blowout kill, table scroll regions, capped roster,
# block canvas in trends.
for tok in (".grid > * { min-width: 0; }", ".tscroll.cap", ".trend canvas"):
    assert tok in html2, f"v2: containment {tok} missing"
# Quest turn-in feed + recipe browser widgets present.
for wid in ("questFeed", "recipeSearch", "recipeRows", "noRecipes"):
    assert f'id="{wid}"' in html2, f"v2: missing widget {wid}"
# Round 3: uPlot layers are absolutely positioned (the stacked-layers +
# overflow clip blanked every chart); the world fits its container with
# scaled tiles instead of growing+scrolling.
assert ".trend .u-under" in html2 and "position: absolute" in html2
assert "availW" in html2 and "tileW, tileH" in html2
assert "TILE_W + TILE_GAP" not in html2, "v2: map still grows+scrolls"
# Crafting order: flow first, recipe browser last.
assert html2.index("Recent flow") < html2.index("Recipe browser")
# Indoor/outdoor legend + full shelter coverage in world.json.
assert "solid tile = indoor, dashed = outdoor" in html2
import json as _json, os as _os
_world = _json.load(open(_os.path.join(_os.path.dirname(__file__), "..", "world.json")))
assert len(_world["rooms"]) == 32
assert all(isinstance(r.get("shelter"), bool) for r in _world["rooms"].values())
# Settled-price panel renamed to plain words.
assert "Price per completed sale" in html2
assert "Settled-price history" not in html2
# Split-lane live feel: SSE stream + shared row helper + start_ts ticker.
for tok in ("EventSource", "/api/activity/stream", "activityRow",
            "actMaxSeq", "start_ts", "startTs"):
    assert tok in html2, f"v2: split-lane token {tok} missing"
# /OC robustness: guarded scores/servers/cmdClass, seq sort, empty states.
assert ".score.toFixed" not in html2, "v2: unguarded toFixed survives"
assert "s.server.players_online" not in html2, "v2: unguarded s.server survives"
assert "cmd-\" + (cmd" in html2 or 'cmd-" + (cmd' in html2
assert "a.seq ?? -1" in html2 and "localeCompare" in html2
assert "No scores recorded yet." in html2
# Server lane: seq field, fan-out registry, SSE route, start timestamp.
import re as _re
_srv = open(_os.path.join(_os.path.dirname(__file__), "..", "server.py")).read()
for tok in ('"seq": activity_seq', "activity_subscribers",
            "text/event-stream", '"start_ts": START_TIME'):
    assert tok in _srv, f"server: split-lane token {tok} missing"
# Shelter veto: market is outdoor.
assert _world["rooms"]["market"]["shelter"] is False
# Quest panel states the full turn-in chain, not just the brief.
assert "turn-in:" in html2 and "hand it over" in html2
# Chart readouts: always-visible current value per graph.
for wid in ("ovPlayersVal", "ovScoreVal", "ovVolumeVal",
            "histPlayersVal", "histScoreVal", "m-priceHistVal"):
    assert f'id="{wid}"' in html2, f"v2: missing readout {wid}"
assert ".trendval" in html2 and "setTrendVal" in html2
# Sort rule: every data table sortable + div-list controls present.
for tid in ("perf", "agents", "orders", "tradeHistory", "bosses",
            "matSupply", "matOrders", "questCatalog", "recipeRows",
            "steamOrders"):
    assert f'data-sort="{tid}"' in html2, f"v2: table {tid} not sortable"
for wid in ("questFeedSort", "agentActivitySort", "activitySort",
            "dungeonSort"):
    assert f'id="{wid}"' in html2, f"v2: missing sort control {wid}"
assert "function sortRows" in html2 and "bindSortTables()" in html2
# Recipe spec: ingredients column last, sortable result/tier/category.
ri = html2.index('data-sort="recipeRows"')
assert ri < html2.index("<th>tier</th>") < html2.index("<th>category</th>")
assert html2.index("<th>category</th>") < html2.index("<th>ingredients</th>")
# Steam-market panel: picker, range, stats, ladder, orders, median/volume.
for wid in ("steamItem", "steamRange", "st-lowask", "st-med", "st-vol",
            "st-chart", "st-ladder", "st-ladderHead", "steamOrders",
            "noSteamOrders"):
    assert f'id="{wid}"' in html2, f"v2: missing steam widget {wid}"
assert "uPlot.paths.bars" in html2 and "Item market" in html2
# Commissions board: Quests-adjacent sortable panel + snapshot key.
for wid in ("commissions", "noCommissions"):
    assert f'id="{wid}"' in html2, f"v2: missing commissions widget {wid}"
assert 'data-sort="commissions"' in html2
assert "renderCommissions" in html2
for tok in ('"commissions": _commission_snapshot()', "def _commission_snapshot"):
    assert tok in _srv, f"server: commissions token {tok} missing"
# PERF pass: guarded DOM writes, chart/map repaint skips, activity cap,
# throttled poll loop with hidden-tab slowdown.
assert "setInterval(tick" not in html2, "v2: 1s setInterval loop still present"
for tok in ("function setHTML", "lastMapKey", "_dataKey", "slice(-80)",
            "visibilitychange", "schedulePoll", "10000 : 2000"):
    assert tok in html2, f"v2: perf token {tok} missing"
# Trends moved to uPlot: no canvas line-chart helper may remain.
assert "drawLineChart" not in html2, "v2: legacy canvas charts still present"
# #211 regression pin: sections must receive the snapshot (render(s)).
assert re.search(r"try \{\s*render\(s\);", html2), "v2: renderAll drops s"
print("DASHBOARD2_OK")

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
