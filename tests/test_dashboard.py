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
