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
# Only Overview + World are functional in 1/4; the rest point at follow-ups.
assert "revamp 2/4 (#206)" in html2 and "revamp 3/4 (#209)" in html2
# Trends moved to uPlot: no canvas line-chart helper may remain.
assert "drawLineChart" not in html2, "v2: legacy canvas charts still present"
# #211 regression pin: sections must receive the snapshot (render(s)).
assert re.search(r"try \{\s*render\(s\);", html2), "v2: renderAll drops s"
print("DASHBOARD2_OK")

print("ALL_DASHBOARD_OK")
