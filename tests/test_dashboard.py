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
assert len(_world["rooms"]) == 43  # 32 map rooms + guild_hall + 6 wilds (#336) + 4 wild-interior chain rooms (#423)
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
assert '"cmd-"' in html and "function cmdClass" in html
assert "a.seq ?? -1" in html and "localeCompare" in html
assert "No scores recorded yet." in html
# Server lane: seq field, fan-out registry, SSE route, start timestamp.
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
assert "<th>progress</th>" in html, "progress column missing"
assert "progFrac" in html and "hpbar" in html, "progress bar wiring missing"
for tok in ('"commissions": _commission_snapshot()', "def _commission_snapshot",
            '"created_ts": c.get("created_ts", 0)',
    '"progress": _commission_progress(c)',
    "def _commission_progress",
):
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

# --- GM-tab treasury live binding (V5 follow-up): the GM readout must
# refresh on snapshot ticks while the GM tab is active, and on GM ack ---
rt = re.search(r"function renderTreasury\(m\) \{(.*?)\n\}\n", html, re.DOTALL)
assert rt, "renderTreasury not found"
rtb = rt.group(1)
assert "m.treasury ?? 0" in rtb, "treasury default missing"
assert '"g-treasury"' in rtb, "g-treasury write missing"
assert "updateGMCosts" in rtb, "cost labels not refreshed"
gmsec = re.search(r"gm: \[(.*?)\n  \],", html, re.DOTALL)
assert gmsec and "renderTreasury" in gmsec.group(1), "gm tab has no treasury section"
assert re.search(r"gmws\.onmessage[\s\S]{0,400}?tick\(\)", html), "GM ack does not pull a fresh tick"
print("TREASURY_LIVE_OK")

# --- bughunt milestone: issue #376 (8 fixes + dungeon_clears) ---
# 1. renderActivity resync must not move the SSE cursor backward.
ra = re.search(r"function renderActivity\(activity\) \{(.*?)\n\}", html, re.DOTALL)
assert ra, "renderActivity not found"
assert "Math.max(actMaxSeq, mx)" in ra.group(1), "SSE cursor can move backward"
print("ACT_CURSOR_OK")

# 2. renderRooms guards partial room rows like renderRoomDrawer does.
rr = re.search(r"function renderRooms\(rooms\) \{(.*?)\n\}", html, re.DOTALL)
assert rr, "renderRooms not found"
rrb = rr.group(1)
assert "(r.players || [])" in rrb, "renderRooms players unguarded"
assert "(r.npcs || [])" in rrb, "renderRooms npcs unguarded"
assert "(r.items || [])" in rrb, "renderRooms items unguarded"
print("ROOMS_GUARD_OK")

# 3. cmdClass sanitizes the server token before class concatenation.
cc = re.search(r"function cmdClass\(cmd\) \{(.*?)\}", html, re.DOTALL)
assert cc, "cmdClass not found"
assert "A-Za-z0-9" in cc.group(1), "cmdClass does not sanitize"
print("CMDCLASS_OK")

# 4. gmOpen tolerates snapshots without a server block.
assert "(lastState.server || {}).gm_port" in gm.group(1), "gmOpen crashes without server"
print("GM_PORT_OK")

# 5. renderRoomDrawer must not stack duplicate roomClose listeners.
rd = re.search(r"function renderRoomDrawer\(byId\) \{(.*?)\n\}\n", html, re.DOTALL)
assert rd, "renderRoomDrawer not found"
assert 'addEventListener("click"' not in rd.group(1), "roomClose listener stacks"
assert ".onclick" in rd.group(1) or "onclick=" in html, "roomClose has no single bind"
print("DRAWER_LISTENER_OK")

# 6. m-priceHist charts an ascending copy; the table stays newest-first.
assert "history.slice().reverse()" in html, "price chart not ascending"
print("PRICE_ASC_OK")

# 7. resizeCharts reuses per-chart creation height (st-chart is 110).
assert "u._h" in html, "per-chart height missing"
rs = re.search(r"function resizeCharts\(\) \{(.*?)\n\}", html, re.DOTALL)
assert rs, "resizeCharts not found"
assert "height: 80" not in rs.group(1), "resizeCharts still forces 80"
assert "u._h || 80" in rs.group(1), "resizeCharts ignores creation height"
assert "u._h = H" in html, "st-chart height not stored"
print("CHART_HEIGHT_OK")

# 8. tick() generations stop overlapping fetches resolving out of order.
tk = re.search(r"async function tick\(\) \{(.*?)\n\}\n", html, re.DOTALL)
assert tk, "tick not found"
tkb = tk.group(1)
assert "tickGen" in tkb, "tick generation counter missing"
assert "gen !== tickGen" in tkb, "stale tick not dropped"
print("TICK_GEN_OK")

# Dungeon clears addendum: the four snapshot numbers are surfaced text-only.
for wid in ("dc-solo", "dc-group", "dc-solo-max", "dc-group-max"):
    assert f'id="{wid}"' in html, f"missing clears widget {wid}"
assert "function renderDungeonClears" in html, "renderDungeonClears missing"
assert "dungeon_clears" in html, "dungeon_clears not consumed"
dc = re.search(r"function renderDungeonClears\(c\) \{(.*?)\n\}", html, re.DOTALL)
assert dc, "renderDungeonClears not found"
assert ".textContent" in dc.group(1), "clears not text-only"
assert "innerHTML" not in dc.group(1), "clears risk layout shift"
print("CLEARS_OK")

# --- History rings (#333): craft + commission activity tables ---
for wid in ("craftHistory", "noCraftHistory", "craftHistoryCount",
            "commHistory", "noCommHistory", "commHistoryCount"):
    assert f'id="{wid}"' in html, f"missing history widget {wid}"
assert 'data-sort="craftHistory"' in html
assert 'data-sort="commHistory"' in html
for fn in ("renderCraftHistory", "renderCommHistory"):
    assert f"function {fn}" in html, f"{fn} missing"
    body = re.search(rf"function {fn}\(.*?\) \{{(.*?)\n\}}", html, re.DOTALL)
    assert body, f"{fn} not found"
    assert ".textContent" in body.group(1), f"{fn} shows no numbers"
ch = re.search(r"craft_history", html)
assert ch, "craft_history not consumed"
assert "commission_history" in html, "commission_history not consumed"
assert "refund" in html and "forfeit" in html, "cancel economics not rendered"
for tok in ('"craft_history": list(craft_feed)', '"commission_history": list(comm_feed)',
            "craft_feed_seq", "comm_feed_seq"):
    assert tok in _srv, f"server: history token {tok} missing"
print("HISTORY_TABLES_OK")

# --- Runs tab (#65): compare a finished run against the one still going ---
assert '<div class="tab" data-tab="runs">Runs</div>' in html
assert '<div id="view-runs" class="tabview">' in html
for tid in ("runsTable", "runCmpTable"):
    assert f'data-sort="{tid}"' in html, f"table {tid} not sortable"
for wid in ("runsCount", "runsMetric", "runsBest", "runsTotal", "runsSelCount",
            "runsRunning", "runsHead", "runsBody", "noRuns", "runsCmpNote",
            "runsCmpMetric", "runCmpVal", "runCmp", "runCmpLegend",
            "runCmpHead", "runCmpBody", "noRunCmp"):
    assert f'id="{wid}"' in html, f"missing runs widget {wid}"
assert 'runs: [["runs", s => renderRuns(s)]]' in html, "runs tab has no section"
for fn in ("renderRuns", "loadRuns", "scheduleRunsPoll", "compareChart",
           "markSorted", "specHead", "specRow", "bestRunNote"):
    assert f"function {fn}" in html, f"{fn} missing"
assert 'fetch("/api/runs"' in html, "runs tab does not read /api/runs"
for tok in ("def _runs_payload", '"/api/runs"', "runlog.runs_payload"):
    assert tok in _srv, f"server: runs token {tok} missing"
# One column spec drives the header AND the sort keys: a header/key list that
# drifted apart would sort by the wrong field with no error. The runs table
# sorts by runCols (identity + per-kind metrics), not the identity-only base.
assert "runCols.map(c => c.key)" in html and "RUN_CMP_COLS.map(c => c.key)" in html
assert 'sortRows("runsTable"' in html and 'sortRows("runCmpTable"' in html, \
    "runs tables sort on a key bindSortTables never writes"
# The static pre-render must be the first bindSortTables() call, or the initial
# header walk sees an empty row and the table is never sortable.
assert re.search(r'specHead\(RUN_COLS\)\);\s*\nsetHTML\("runCmpHead".*?\nbindSortTables\(\);',
                 html, re.DOTALL), "runs headers built after the sorter binds"
# The client cap is a duplicate of runlog.MAX_RUN_IDS on purpose (the page
# cannot import it), so it is asserted against the server value instead of a
# second hardcoded literal.
with open(os.path.join(ROOT, "ml", "runlog.py"), encoding="utf-8") as _rl_fh:
    RL_MAX_RUN_IDS = int(
        re.search(r"^MAX_RUN_IDS = (\d+)", _rl_fh.read(), re.MULTILINE).group(1)
    )


def _runs_section(h, marker):
    """Body of the runlog/dashboard function whose name starts with marker."""
    m = re.search(r"function " + marker + r"\w*\([^)]*\) \{(.*?)\n\}", h, re.DOTALL)
    assert m, f"{marker} not found"
    return m.group(1)


# Per-trainer columns: a fixed score/steps/score_hr list read "-" for every
# botfarm and soak run, and the header could not be rebuilt per kind.
assert "runTableCols" in html and "kind_fields" in html
assert "runsData.root_name" in html and "runsData.root " not in html, \
    "runs payload path leak"
# A second poll must not attach a second click handler to a rebuilt <th>.
assert "dataset.sortBound" in html
# The server caps a request at MAX_RUN_IDS; a client that allowed more would
# show a checked box the payload has no series for.
assert "MAX_RUN_IDS" in html and f"MAX_RUN_IDS = {RL_MAX_RUN_IDS}" in html
# bestRunNote lands in textContent, so esc() would print its entities.
assert not re.search(r"esc\(", _runs_section(html, "bestRunNote")), \
    "bestRunNote escapes into textContent"
# The verdict ranks on a metric the owner picks, with direction from the
# server -- never a hardcoded assumption that score is what matters.
assert 'runsMetric = "score"' in html and "higher_is_better" in html
assert "dirFor(runsMetric) !== false" in html
# Selection is delegated: renderRuns rewrites the tbody on every poll, so a
# per-row listener would be re-attached (and stack) each time.
rr2 = re.search(r"function renderRuns\(s\) \{(.*?)\n\}\n", html, re.DOTALL)
assert rr2, "renderRuns not found"
assert "addEventListener" not in rr2.group(1), "renderRuns rebinds listeners"
assert '$("runsBody").addEventListener("change"' in html
# The catalog outlives the snapshot, so it polls only while its tab is up.
sp = re.search(r"function scheduleRunsPoll\(\) \{(.*?)\n\}\n", html, re.DOTALL)
assert sp and 'if (activeTab !== "runs") return;' in sp.group(1), "runs polls off-tab"
assert re.search(r'activeTab === "runs"\) \{\s*(?://[^\n]*\n\s*)*loadRuns\(\);', html), \
    "switching to Runs does not load"
# One dropped poll keeps the last good payload instead of blanking the table.
assert "runsError = String(e.message || e)" in html and "let runsData = null" in html
# Multi-series chart reuses the themed palette instead of hardcoding colors.
assert "seriesColor(i)" in html and "series:" in html
print("RUNS_TAB_OK")

print("ALL_DASHBOARD_OK")
