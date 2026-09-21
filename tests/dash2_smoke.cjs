// Runtime smoke for dashboard2.html (manual visual-proof tool, #209).
//
// Executes the REAL inline script against DOM stubs with three snapshots
// (live-shaped, EMPTY {}, live-twice for change-guard idempotence) plus
// sort-flip, seq-sort, and the north-up GEO + tile OVERLAP proofs against
// real world.json exits. Any throw or failed assertion exits nonzero.
//
// Run manually from the repo root (requires node, no dependencies):
//     node tests/dash2_smoke.cjs
// NOT wired into CI (python-only workflows); the python contract suite
// tests/test_dashboard.py covers markup statically.
const fs = require("fs");
const vm = require("vm");
const path = require("path");

const ROOT = path.join(__dirname, "..");
const html = fs.readFileSync(path.join(ROOT, "dashboard2.html"), "utf8");
const realIds = new Set([...html.matchAll(/id="([\w-]+)"/g)].map(m => m[1]));

const PERF = { htmlWrites: 0, mapRepaints: 0, sized: 0 };
function makeEl(id) {
  return {
    _html: "", _text: "",
    set innerHTML(v) { this._html = String(v); PERF.htmlWrites++; },
    get innerHTML() { return this._html; },
    set textContent(v) { this._text = String(v); },
    get textContent() { return this._text; },
    style: {}, dataset: {}, value: "",
    classList: { add() {}, remove() {}, toggle() {} },
    addEventListener() {}, appendChild() {}, prepend() {},
    get children() { return []; },
    getContext() {
      PERF.mapRepaints++;
      return new Proxy({}, { get: (t, p) => (p === "canvas" ? undefined : () => {}),
        set: () => true });
    },
    getBoundingClientRect: () => ({ left: 0, top: 0, width: 1200, height: 480 }),
  };
}

const els = {};
const sandbox = {
  console, Math, JSON, Object, Array, String, Number, Boolean, Date,
  document: {
    documentElement: { dataset: {} },
    hidden: false,
    getElementById: id => (els[id] ||= (realIds.has(id) ? makeEl(id) : undefined)),
    querySelectorAll: () => [],
    createElement: () => makeEl("dyn"),
    addEventListener() {},
  },
  window: { devicePixelRatio: 1, addEventListener() {} },
  uPlot: class {
    constructor(o, d) { this.data = d; this.constructor.created++; }
    setData(d) { this.data = d; this.constructor.updated++; }
    setSize() { PERF.sized++; }
    destroy() { this.constructor.destroyed++; }
  },
  EventSource: undefined,
  fetch: async () => { throw new Error("no network in smoke"); },
  setInterval: () => 0, setTimeout: () => 0, clearTimeout: () => {},
  WebSocket: function () { throw new Error("no sockets in smoke"); },
};
sandbox.globalThis = sandbox;
sandbox.uPlot.created = 0; sandbox.uPlot.updated = 0; sandbox.uPlot.destroyed = 0;
sandbox.uPlot.paths = { bars: () => ({}) };
vm.createContext(sandbox);

const scripts = [...html.matchAll(/<script(?![^>]*src=)[^>]*>([\s\S]*?)<\/script>/g)].map(m => m[1]);
if (!scripts.length) { console.error("NO_INLINE_SCRIPT"); process.exit(1); }
let code = scripts.join("\n")
  .replace(/^\s*tick\(\);\s*$/m, "")
  .replace(/^\s*schedulePoll\(\);\s*$/m, "");
vm.runInContext(code, sandbox, { filename: "dashboard2.js" });

const live = {
  server: { players_online: 2, connections: 3, uptime: 99, gm_port: 8767, ws_port: 8765,
    start_ts: 1700000000 },
  market: { treasury: 100.5, collected_lifetime: 7, trade_count: 1, tax_rate: 0.1, tax_min: 1,
    orders: [{ id: 1, item: "Healing Herb", price: 9, seller: "A", ts: 1 },
      { id: 2, item: "Healing Herb", price: 12, seller: "B", ts: 2 }],
    history: [{ time: "t", buyer: "B", seller: "A", item: "Healing Herb", price: 9, tax: 1, payout: 8 }] },
  rooms: [{ id: "town_square", name: "Town Square", shelter: false,
    exits: [{ dir: "north", to: "x", to_name: "X" }],
    players: [{ name: "A", level: 2 }], npcs: [{ name: "N", alive: true, hp: 3, max_hp: 5 }],
    items: ["Healing Herb", "Iron Ore"], gold: 0 }],
  players: [
    { name: "A", level: 2, score: 10.5, kills: 1, deaths: 0, gold: 5, hp: 18, max_hp: 20,
      room: "town_square", last_action: "gather",
      recent_actions: [{ seq: 5, time: "t", cmd: "gather" }, { seq: 3, time: "t", cmd: "craft" }] },
    { name: "B", level: 5, score: 99.9, kills: 9, deaths: 1, gold: 50, hp: 20, max_hp: 20,
      room: "market", last_action: "buy", recent_actions: [] }],
  scores: [{ name: "A", level: 2, score: 10.5 }],
  history: [{ players_online: 1, top_scores: [{ score: 5 }], market_orders: 0 },
    { players_online: 2, top_scores: [{ score: 10.5 }], market_orders: 1 }],
  activity: [{ seq: 9, time: "t", name: "A", level: 2, cmd: "gather" },
    { seq: 7, time: "t", name: "A", level: 2, cmd: "buy" }],
  buffs: { xp: 0, gold: 0 },
  bosses: [{ name: "E", room: "r", hp: 10, max_hp: 10, attack: 2 }],
  dungeons: [{ id: 1, party: ["A"], max_floor_reached: 1,
    floors: [{ floor: 1, guards_alive: 0, guards_total: 1, cleared: true }] }],
  catalog: { players: ["A"], items: ["Healing Herb"], rooms: ["town_square"] },
  quests: {
    catalog: [{ id: "guard_charm", giver_name: "Town Guard", room: "town_square",
      brief: "craft a charm", reward_xp: 50, reward_gold: 25 }],
    active: { guard_charm: 1 }, completions: { guard_charm: 3 }, turnins_last_min: 1,
    recent_turnins: [{ seq: 4, t: "12:00:01", name: "A", qid: "guard_charm" }],
  },
  recipes: [
    { id: "iron_plate", result: "Iron Plate", result_qty: 1,
      inputs: [{ item: "Iron Ore", qty: 2 }], tier: 2, category: "equipment" },
    { id: "herb_tea", result: "Herb Tea", result_qty: 1,
      inputs: [{ item: "Healing Herb", qty: 1 }], tier: 1, category: "consumable" },
  ],
  commissions: [
    { id: 2, poster: "B", target: "wolf", required_kills: 3, reward_gold: 10,
      reward_xp: 5, status: "completed", filled_by: "A", created_ts: 1700000060 },
    { id: 1, poster: "A", target: "rat", required_kills: 2, reward_gold: 5,
      reward_xp: 3, status: "open", filled_by: null, created_ts: 1700000000 },
  ],
};

const sections = vm.runInContext("TAB_SECTIONS", sandbox);
let failed = 0;
function runAll(snap, label) {
  for (const [tab, entries] of Object.entries(sections)) {
    for (const [name, render] of entries) {
      try { render(snap); }
      catch (e) { failed++; console.error(`FAIL ${label} ${tab}/${name}: ${e.message}`); }
    }
  }
}
runAll(live, "live");
runAll({}, "empty");
runAll(live, "live2");
PERF.htmlWrites = 0; sandbox.uPlot.updated = 0; PERF.sized = 0; PERF.mapRepaints = 0;
runAll(live, "live3-idempotent");
console.log(`idempotent: html=${PERF.htmlWrites} setData=${sandbox.uPlot.updated} setSize=${PERF.sized} map=${PERF.mapRepaints}`);
if (PERF.htmlWrites || sandbox.uPlot.updated || PERF.sized || PERF.mapRepaints) {
  failed++; console.error("FAIL perf regression: idle tick churns");
}

// seq sort: gather(seq5) before craft(seq3) despite equal times.
const feedHtml = els["agentActivity"]._html;
if (feedHtml.indexOf("gather") > feedHtml.indexOf("craft")) {
  failed++; console.error("FAIL seq-sort not newest-first");
}
// Sort flip: orders default price-asc (9 before 12); flip col 3 desc.
const oAsc = els["orders"]._html;
if (oAsc.indexOf(">9<") > oAsc.indexOf(">12<")) { failed++; console.error("FAIL orders default not price-asc"); }
vm.runInContext('sortState["orders"] = {col: 3, dir: -1}', sandbox);
vm.runInContext("renderMarket", sandbox)(live.market);
const oDesc = els["orders"]._html;
if (oDesc.indexOf(">9<") < oDesc.indexOf(">12<")) { failed++; console.error("FAIL orders flip not price-desc"); }
// Recipe spec: ingredients column last + result-asc default.
const thOrder = [...html.matchAll(/<th>(result|tier|category|ingredients)<\/th>/g)].map(m => m[1]);
if (thOrder.join(",") !== "result,tier,category,ingredients") {
  failed++; console.error("FAIL recipe column order: " + thOrder.join(","));
}
const rHtml = els["recipeRows"]._html;
if (rHtml.indexOf("Herb Tea") > rHtml.indexOf("Iron Plate")) {
  failed++; console.error("FAIL recipe default not result-asc");
}
// Quest chain text present.
if (!els["questCatalog"]._html.includes("turn-in:")) { failed++; console.error("FAIL quest chain missing"); }
// Readout values set from live data.
for (const [id, want] of [["ovPlayersVal", "2"], ["ovScoreVal", "10.5"], ["m-priceHistVal", "9"]]) {
  if (els[id]._text !== want) { failed++; console.error(`FAIL readout ${id}=${els[id]._text} want ${want}`); }
}
// Steam panel renders ladder + stats from live data.
if (!els["st-ladderHead"]._html.includes("for sale starting at")) {
  failed++; console.error("FAIL steam ladder head");
}
if (els["st-lowask"]._text !== "9g") { failed++; console.error("FAIL steam lowask=" + els["st-lowask"]._text); }
// Commissions board: newest-first default (#2 before #1), open tagged,
// posted-age rendered from created_ts.
const cHtml = els["commissions"]._html;
if (cHtml.indexOf("#2") > cHtml.indexOf("#1")) { failed++; console.error("FAIL commissions not newest-first"); }
if (!cHtml.includes(">open<") || !cHtml.includes("wolf")) { failed++; console.error("FAIL commissions content"); }
if (!cHtml.includes(" ago</td>")) { failed++; console.error("FAIL commissions posted-age missing"); }
if (failed) { console.error(`PROVE_FAIL (${failed})`); process.exit(1); }
console.log("PROVE_OK (live+empty+idempotent+seq+sortflip+recipe+chain+readouts+steam+commissions)");

// North-up GEO + tile OVERLAP on the town subgraph (real world.json exits).
const geoRooms = [
  { id: "town_square", exits: { north: "forest_edge", east: "old_shop", south: "graveyard", west: "market" } },
  { id: "market", exits: { east: "town_square", west: "artisan_row" } },
  { id: "graveyard", exits: { north: "town_square", up: "mountain_pass", enter: "dungeon_entrance", down: "crypt_hall" } },
  { id: "forest_edge", exits: { south: "town_square", north: "deep_forest", east: "healing_spring" } },
  { id: "old_shop", exits: { west: "town_square", east: "harbor_lane" } },
  { id: "artisan_row", exits: { east: "market" } },
  { id: "deep_forest", exits: { south: "forest_edge" } },
  { id: "healing_spring", exits: { west: "forest_edge" } },
  { id: "harbor_lane", exits: { west: "old_shop" } },
  { id: "mountain_pass", exits: { down: "graveyard" } },
  { id: "crypt_hall", exits: { up: "graveyard" } },
];
const layout = vm.runInContext("mapLayout", sandbox)(geoRooms);
let geoFail = 0;
for (const r of geoRooms) {
  for (const [dir, target] of Object.entries(r.exits)) {
    if (!layout.positions[target]) continue;
    const a = layout.positions[r.id], b = layout.positions[target];
    const d = dir.toLowerCase();
    let ok = true;
    if (d === "north") ok = b.y < a.y;
    else if (d === "south") ok = b.y > a.y;
    else if (d === "east") ok = b.x > a.x;
    else if (d === "west") ok = b.x < a.x;
    if (!ok) { geoFail++; console.error(`GEO_FAIL ${dir} ${r.id} -> ${target}`); }
  }
}
const again = vm.runInContext("mapLayout", sandbox)(geoRooms);
if (JSON.stringify(layout.positions) !== JSON.stringify(again.positions)) {
  geoFail++; console.error("GEO_FAIL nondeterministic");
}
const TW = layout.tileW, TH = layout.tileH;
const ids = Object.keys(layout.positions);
for (let i = 0; i < ids.length; i++) {
  for (let j = i + 1; j < ids.length; j++) {
    const a = layout.positions[ids[i]], b = layout.positions[ids[j]];
    if (Math.abs(a.x - b.x) < TW && Math.abs(a.y - b.y) < TH) {
      geoFail++; console.error(`OVERLAP ${ids[i]} vs ${ids[j]}`);
    }
  }
}
if (geoFail) { console.error(`GEO_FAIL (${geoFail})`); process.exit(1); }
console.log(`GEO_OK (north-up, deterministic, no overlap at tile ${TW}x${TH})`);
