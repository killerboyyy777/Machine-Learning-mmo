// Runtime smoke for the canonical dashboard.html (manual visual-proof
// tool, #209 revamp through the #207 swap which promoted dashboard2.html).
// (Kept filename: history. It tests dashboard.html now.)
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
const html = fs.readFileSync(path.join(ROOT, "dashboard.html"), "utf8");
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
    // Listeners are recorded so a delegated handler can be dispatched: the
    // real DOM does the dispatching, and a stub that dropped them would
    // silently prove nothing about the handler body.
    _handlers: {},
    addEventListener(t, fn) { (this._handlers[t] ||= []).push(fn); },
    fire(t, ev) { for (const fn of this._handlers[t] || []) fn(ev); },
    appendChild() {}, prepend() {},
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
const docHandlers = {};
const sandbox = {
  console, Math, JSON, Object, Array, String, Number, Boolean, Date,
  document: {
    documentElement: { dataset: {} },
    hidden: false,
    getElementById: id => (els[id] ||= (realIds.has(id) ? makeEl(id) : undefined)),
    querySelectorAll: () => [],
    createElement: () => makeEl("dyn"),
    // Recorded like the element stubs: the config editor delegates on
    // document, and a no-op here would prove nothing about those handlers.
    addEventListener(t, fn) { (docHandlers[t] ||= []).push(fn); },
  },
  window: { devicePixelRatio: 1, addEventListener() {} },
  uPlot: class {
    constructor(o, d) { this.opts = o; this.data = d; this.constructor.created++; }
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

// Runs catalog (#65). /api/runs cannot resolve in the vm -- fetch throws by
// design -- so seed the last-good payload the tab renders from. td_loss is
// deliberately better on the LOWER-scoring run: a verdict that ignores the
// server's higher_is_better would then crown the same run twice.
const RUNS_FIXTURE = {
  // root_name, not root: the payload must not carry an absolute path (#65).
  root_name: "runs",
  fields: [{ name: "score", higher_is_better: true },
           { name: "td_loss", higher_is_better: false },
           { name: "fitness", higher_is_better: true },
           { name: "episodes", higher_is_better: true },
           { name: "steps", higher_is_better: true },
           { name: "score_hr", higher_is_better: true },
           { name: "alive", higher_is_better: true }],
  // Per-kind column lists: the runs table must show a botfarm's fitness and a
  // soak's episodes, not just the dqn metrics (#65 send-back).
  kind_fields: {
    dqn: ["steps", "total_steps", "total_reward"],
    ml_botfarm: ["steps", "fitness", "top_score"],
    soak: ["episodes", "mean_reward", "alive"],
  },
  runs: [
    { run_id: "20260101-120000", kind: "dqn", status: "finished", seed: 7,
      config_hash: "abcdef1234567890", git_sha: "1234567890abcdef",
      started: 1700000000, metrics: { score: 10, steps: 500, score_hr: 20, td_loss: 0.9,
                                      total_steps: 500, total_reward: 12 } },
    { run_id: "20260101-130000", kind: "dqn", status: "running", seed: 8,
      config_hash: "abcdef1234567890", git_sha: "1234567890abcdef",
      started: 1700003600, metrics: { score: 4, steps: 120, score_hr: 8, td_loss: 0.5,
                                      total_steps: 120, total_reward: 3 } },
    { run_id: "20260101-140000", kind: "ml_botfarm", status: "finished", seed: 9,
      config_hash: "12345678abcdef90", git_sha: "abcdef1234567890",
      started: 1700007200, metrics: { steps: 900, fitness: 3.5, top_score: 44,
                                      total_reward: null } },
    { run_id: "20260101-150000", kind: "soak", status: "failed", seed: null,
      config_hash: "", git_sha: "",
      started: 1700010800, metrics: { episodes: 120, mean_reward: -0.25, alive: 0,
                                      steps: null, total_steps: null } },
  ],
  series: {
    "20260101-120000": [{ _ts: 1700000000, score: 1, td_loss: 1.4 },
                        { _ts: 1700000100, score: 6, td_loss: 1.1 },
                        { _ts: 1700000200, score: 10, td_loss: 0.9 }],
    "20260101-130000": [{ _ts: 1700003600, score: 2, td_loss: 0.7 },
                        { _ts: 1700003700, score: 4, td_loss: 0.5 }],
  },
};
vm.runInContext("runsData = " + JSON.stringify(RUNS_FIXTURE) + "; runsSel = []", sandbox);

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
// Recipe spec: ingredients column last + result-asc default. Scoped to
// the recipe table: other tables (e.g. craft history) may reuse the
// "result" header word without affecting the recipe spec.
const recipeTable = (html.match(/<table data-sort="recipeRows">[\s\S]*?<\/table>/) || [""])[0];
const thOrder = [...recipeTable.matchAll(/<th>(result|tier|category|ingredients)<\/th>/g)].map(m => m[1]);
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
// Hover hooks: drive the stored setCursor fns with a fake cursor.
// Trend chart: idx 1 shows "value @ time", null restores current.
const ovU = vm.runInContext("charts['ovPlayers']", sandbox);
const hook = ovU.opts.hooks.setCursor[0];
hook({ cursor: { idx: 1 }, data: [[0, 1], [5, 6]], _times: ["a", "b"], _current: 9 });
if (els["ovPlayersVal"]._text !== "6 @ b") { failed++; console.error("FAIL hover show: " + els["ovPlayersVal"]._text); }
hook({ cursor: { idx: null }, data: [[0, 1], [5, 6]], _times: ["a", "b"], _current: 9 });
if (els["ovPlayersVal"]._text !== "9") { failed++; console.error("FAIL hover restore: " + els["ovPlayersVal"]._text); }
// Steam chart: idx 0 shows the bucket label, null restores the caption.
const stU = vm.runInContext("charts['st-chart']", sandbox);
const stHook = stU.opts.hooks.setCursor[0];
stHook({ cursor: { idx: 0 }, _labels: ["8g median · 2 fills"] });
if (els["st-tip"]._text !== "8g median · 2 fills") { failed++; console.error("FAIL steam hover: " + els["st-tip"]._text); }
stHook({ cursor: { idx: null }, _labels: [] });
if (els["st-tip"]._text !== "hover a bucket for its median + fills") {
  failed++; console.error("FAIL steam hover restore: " + els["st-tip"]._text);
}
// Theme invalidation: identical rooms skip repaint; clearing the key
// (what the toggle handler does) repaints exactly once.
const roomsOnce = live.rooms;
vm.runInContext("renderWorldMap", sandbox)(roomsOnce);
const mm0 = PERF.mapRepaints;
vm.runInContext("renderWorldMap", sandbox)(roomsOnce);
if (PERF.mapRepaints !== mm0) { failed++; console.error("FAIL map guard skipped"); }
vm.runInContext('lastMapKey = ""', sandbox);
vm.runInContext("renderWorldMap", sandbox)(roomsOnce);
if (PERF.mapRepaints !== mm0 + 1) { failed++; console.error("FAIL theme invalidate repaint"); }

// ---- Runs tab (#65) --------------------------------------------------
const runRowsHtml = els["runsBody"]._html;
if (runRowsHtml.indexOf("20260101-120000") < runRowsHtml.indexOf("20260101-130000")) {
  failed++; console.error("FAIL runs not newest-first");
}
if (!runRowsHtml.includes('data-run="20260101-130000"')) {
  failed++; console.error("FAIL runs compare checkbox missing");
}
if (!runRowsHtml.includes(">finished<") || !runRowsHtml.includes(">running<")) {
  failed++; console.error("FAIL runs status column");
}
if (els["runsTotal"]._text !== "4" || els["runsRunning"]._text !== "1") {
  failed++; console.error(`FAIL runs counters ${els["runsTotal"]._text}/${els["runsRunning"]._text}`);
}
// setHTML, so innerHTML: the directory name comes from the payload and is
// never an absolute path.
if (!els["runsCount"]._html.includes("in runs")) {
  failed++; console.error("FAIL runs count missing dir name: " + els["runsCount"]._html);
}
// Per-kind columns (#65 send-back): a fixed score/steps/score_hr list left
// every botfarm and soak metric blank. Column set = identity + union of kinds.
const runColsLabels = vm.runInContext("runCols.map(c => c.label)", sandbox);
for (const want of ["steps", "total_steps", "total_reward", "fitness", "top_score",
                    "episodes", "mean_reward", "alive"]) {
  if (!runColsLabels.includes(want)) {
    failed++; console.error("FAIL runs table missing per-kind column " + want);
  }
}
const nAll = els["runsHead"]._html.split("</th>").length;
const oneKind = {dqn: ["steps"]};
vm.runInContext("runsData.kind_fields = ONE_KIND; renderRuns({})", Object.assign(sandbox, { ONE_KIND: oneKind }));
const nOne = els["runsHead"]._html.split("</th>").length;
if (nOne === nAll) {
  failed++; console.error("FAIL runs header ignored a changed kind-field set");
}
// An unchanged kind set must not rebuild the column spec or re-bind the
// sorter: the tab polls every 5s and the handlers stack if they do.
vm.runInContext("runColsRef = runCols", sandbox);
vm.runInContext("runsData.kind_fields = ONE_KIND; renderRuns({})", sandbox);
if (vm.runInContext("runColsRef === runCols", sandbox) !== true) {
  failed++; console.error("FAIL runs column spec rebuilt on every poll");
}
sandbox.RUNS0 = RUNS_FIXTURE;
vm.runInContext("runsData = RUNS0; renderRuns({})", sandbox);
// A metric the server no longer offers snaps back to one it does, instead of
// leaving the verdict reading about a metric no run has.
vm.runInContext('runsMetric = "gone_metric"; renderRuns({})', sandbox);
const snapped = vm.runInContext("runsMetric", sandbox);
if (!["score", "td_loss", "fitness", "episodes", "steps", "score_hr", "alive"].includes(snapped)) {
  failed++; console.error("FAIL metric snap left an unknown metric: " + snapped);
}
// Score ranks the better-scoring run first...
if (!els["runsBest"]._text.startsWith("20260101-120000 wins on score")) {
  failed++; console.error("FAIL best-by-score verdict: " + els["runsBest"]._text);
}
// Runs of a kind that records no "score" at all (botfarm/soak) used to make the
// verdict claim nothing had a score. fitness is the farm's own axis.
vm.runInContext('runsMetric = "fitness"; runsSel = ["20260101-140000"]; renderRuns({})', sandbox);
if (!els["runsBest"]._text.startsWith("20260101-140000 is the only run with a fitness")) {
  failed++; console.error("FAIL per-kind verdict: " + els["runsBest"]._text);
}
// bestRunNote lands in textContent: esc() there printed "&amp;" literally.
vm.runInContext('runsSel = []; runsMetric = "score"; renderRuns({})', sandbox);
const escNote = vm.runInContext("bestRunNote", sandbox)(
  [{ run_id: "a&b", metrics: { score: 1 } }, { run_id: "c", metrics: { score: 2 } }],
  "score", true);
if (escNote.includes("&amp;") || escNote.includes("&lt;")) {
  failed++; console.error("FAIL bestRunNote double-escapes: " + escNote);
}
// ...and switching to a loss metric must flip it, not keep crowning the same
// run, since the direction comes from the server rather than the column name.
vm.runInContext('runsMetric = "td_loss"; renderRuns({})', sandbox);
if (!els["runsBest"]._text.startsWith("20260101-130000 wins on td_loss")) {
  failed++; console.error("FAIL best-by-loss verdict: " + els["runsBest"]._text);
}
if (!els["runsMetric"]._html.includes("td_loss (lower is better)")) {
  failed++; console.error("FAIL metric picker direction hint missing");
}
// Two selected runs: comparison table fills, chart builds one line each,
// padded to a shared x axis so the shorter run ends in a gap not a resample.
vm.runInContext('runsMetric = "score"; runsSel = ["20260101-120000", "20260101-130000"]; renderRuns({})', sandbox);
if (els["runsSelCount"]._text !== "2") { failed++; console.error("FAIL sel count " + els["runsSelCount"]._text); }
const cmpHtml = els["runCmpBody"]._html;
if (!cmpHtml.includes("20260101-120000") || !cmpHtml.includes("20260101-130000")) {
  failed++; console.error("FAIL comparison rows missing");
}
const cmpU = vm.runInContext("charts['runCmp']", sandbox);
if (!cmpU || cmpU.data.length !== 3 || cmpU.data[2][2] !== null) {
  failed++; console.error("FAIL comparison chart series/padding");
}
if ((els["runCmpLegend"]._html.match(/<span>/g) || []).length !== 2) {
  failed++; console.error("FAIL comparison legend");
}
const cmpHook = cmpU.opts.hooks.setCursor[0];
// The two fixture runs were logged an hour apart, so on a time axis their
// samples sit at different instants and one hover names the run that actually
// has a sample there. The old assertion pinned sample index 1 onto both runs,
// which is the false shared timeline #429 is about: it made a faster-cadence
// run look like it had a value where it had none.
const grid = cmpU.data[0];
if (grid.length !== 5 || grid[3] !== 1700003600) {
  failed++; console.error("FAIL comparison x grid is not the union of run timestamps: " + JSON.stringify(grid));
}
cmpHook({ cursor: { idx: 1 }, data: cmpU.data, _current: cmpU._current });
if (!els["runCmpVal"]._text.includes("20260101-120000: 6") ||
    !els["runCmpVal"]._text.includes("@")) {
  failed++; console.error("FAIL comparison hover names the run at that instant: " + els["runCmpVal"]._text);
}
if (els["runCmpVal"]._text.includes("20260101-130000")) {
  failed++; console.error("FAIL comparison hover invented a value for a run with no sample there: " + els["runCmpVal"]._text);
}
cmpHook({ cursor: { idx: 3 }, data: cmpU.data, _current: cmpU._current });
if (!els["runCmpVal"]._text.includes("20260101-130000: 2")) {
  failed++; console.error("FAIL comparison hover misses the other run at its own instant: " + els["runCmpVal"]._text);
}
cmpHook({ cursor: { idx: null }, data: cmpU.data, _current: cmpU._current });
if (els["runCmpVal"]._text !== cmpU._current) {
  failed++; console.error("FAIL comparison hover restore");
}
// Dropping a run changes the series count, which uPlot treats as creation-time
// config: the chart has to rebuild, not setData onto a 2-line config.
const destroyedBefore = sandbox.uPlot.destroyed;
vm.runInContext('runsSel = ["20260101-120000"]; renderRuns({})', sandbox);
const cmpU2 = vm.runInContext("charts['runCmp']", sandbox);
if (sandbox.uPlot.destroyed !== destroyedBefore + 1 || cmpU2.data.length !== 2) {
  failed++; console.error("FAIL comparison chart rebuild on series change");
}
// Ticking past the server's MAX_RUN_IDS: the box un-ticks itself instead of
// sending a query the server silently truncates (checked box, no series).
const cap = vm.runInContext("MAX_RUN_IDS", sandbox);
sandbox.FULL_SEL = Array.from({ length: cap }, (_, i) => "20260101-12000" + i);
vm.runInContext("runsSel = FULL_SEL", sandbox);
const box = { checked: true, dataset: { run: "20260101-999999" } };
els["runsBody"].fire("change", { target: box });
if (box.checked !== false) {
  failed++; console.error("FAIL selection cap: a " + (cap + 1) + "th run stayed ticked");
}
if (vm.runInContext("runsSel.length", sandbox) !== cap) {
  failed++; console.error("FAIL selection cap: runsSel grew past " + cap);
}
// Un-ticking still removes, or a capped selection could never be reduced.
els["runsBody"].fire("change", { target: { checked: false, dataset: { run: sandbox.FULL_SEL[0] } } });
if (vm.runInContext("runsSel.length", sandbox) !== cap - 1) {
  failed++; console.error("FAIL selection cap: un-tick did not remove");
}
// --- Config editor (#63) -------------------------------------------------
// The config tab's editor is async (fetch + save), so this case lives in an
// async function and the verdicts run from its continuation: a promise the
// harness never awaits would prove nothing.
const CFG_FIXTURE = {
  editable: true,
  files: [
    { id: "server", label: "server_config.json", path: "server_config.json",
      restart: "Restart the server to apply.",
      sections: [
        { name: "scoring", fields: [
          { key: "ACTION_WINDOW", type: "int", value: 20, text: "20", default: 20,
            present: true, min: 1, max: 500, step: 1, help: "actions scored per turn" },
          { key: "DEATH_PENALTY", type: "float", value: 5, text: "5.0", default: 5,
            present: true, min: 0, max: 100, step: 0.5, help: "score lost on death" } ] },
        { name: "economy", fields: [
          { key: "TAX_RATE", type: "float", value: 0.1, text: "0.1", default: 0.1,
            present: true, min: 0, max: 1, step: 0.01, help: "market tax fraction" } ] } ] },
    { id: "ml", label: "ml/ml_config.json", path: "ml/ml_config.json",
      restart: "Restart training to apply.",
      sections: [
        { name: "curriculum", fields: [
          { key: "CURRICULUM_THRESHOLDS", type: "list", value: [0, 10, 30, 60],
            text: "[0, 10, 30, 60]", default: [0, 10, 30, 60], present: true, count: 4,
            min: 0, max: 1000, step: 1, help: "score gates per stage" } ] } ] },
  ],
  presets: [
    { name: "Balanced", help: "code defaults", values: { server: {}, ml: {} } },
    { name: "Fast Training", help: "sharper reward, denser curriculum",
      values: { server: { "scoring.DEATH_PENALTY": 2 }, ml: { "curriculum.CURRICULUM_THRESHOLDS": [0, 5, 15, 30] } } },
    { name: "Economy Focus", help: "lower tax", values: { server: { "economy.TAX_RATE": 0.05 }, ml: {} } },
  ],
};
const cfgFetches = [];
let cfgPayload = CFG_FIXTURE;
sandbox.fetch = async (url, opts) => {
  cfgFetches.push({ url, opts });
  if (opts && opts.method === "POST") {
    return { ok: true, status: 200, json: async () => ({ ok: true, changed: ["economy.TAX_RATE"],
      restart: "Restart the server to apply." }) };
  }
  return { ok: true, status: 200, json: async () => cfgPayload };
};

async function configCase() {
  await vm.runInContext("loadConfig()", sandbox);
  if (vm.runInContext("cfgData === null", sandbox)) {
    failed++; console.error("FAIL config editor did not load the payload");
  }
  // Every field carries its bounds, its tooltip, and the server's value text
  // (0.10 on disk has to read 0.1 here, or every load looks dirty).
  const form = els["cfgForm"]._html;
  if (!form.includes('title="actions scored per turn"') || !form.includes('min="1"') ||
      !form.includes('max="500"') || !form.includes('value="0.1"') ||
      !form.includes("data-cfgreset")) {
    failed++; console.error("FAIL config field row missing bounds/tooltip/value: " + form);
  }
  if (!els["cfgPresets"]._html.includes("Fast Training") ||
      (els["cfgPresets"]._html.match(/data-cfgpreset=/g) || []).length !== 3) {
    failed++; console.error("FAIL config presets not rendered");
  }
  if (els["cfgSave"].disabled !== false) {
    failed++; console.error("FAIL save disabled on a loopback editable payload");
  }
  // The other file renders its own shape: a list field, not a scalar.
  vm.runInContext('cfgFile = "ml"; renderConfigEditor()', sandbox);
  if (!els["cfgForm"]._html.includes('value="[0, 10, 30, 60]"')) {
    failed++; console.error("FAIL ml file not rendered: " + els["cfgForm"]._html);
  }
  // A preset stages into the draft and must not fetch: the operator still
  // presses Save, so a preset can never be a silent write.
  vm.runInContext('cfgFile = "server"; cfgDraft = {}; renderConfigEditor()', sandbox);
  const beforePreset = cfgFetches.length;
  for (const fn of docHandlers["click"] || []) {
    fn({ target: { closest: sel => (sel === "[data-cfgpreset]" ? { dataset: { cfgpreset: "2" } } : null) } });
  }
  if (cfgFetches.length !== beforePreset) {
    failed++; console.error("FAIL preset wrote without pressing Save");
  }
  if (vm.runInContext('cfgDraft["server|economy.TAX_RATE"]', sandbox) !== "0.05") {
    failed++; console.error("FAIL preset did not stage its value");
  }
  // A value retyped to what the field already holds is not an edit.
  vm.runInContext('cfgDraft = { "server|economy.TAX_RATE": "0.1" }; renderConfigEditor()', sandbox);
  const beforeNoop = cfgFetches.length;
  await vm.runInContext("saveConfig()", sandbox);
  if (cfgFetches.length !== beforeNoop) {
    failed++; console.error("FAIL save sent an edit that changes nothing");
  }
  if (!els["cfgMsg"]._text.includes("nothing changed")) {
    failed++; console.error("FAIL no-op save verdict: " + els["cfgMsg"]._text);
  }
  // A real edit posts the file id and the dotted keys.
  vm.runInContext('cfgDraft = { "server|economy.TAX_RATE": "0.05" }; renderConfigEditor()', sandbox);
  const beforeEdit = cfgFetches.length;
  await vm.runInContext("saveConfig()", sandbox);
  // A successful save reloads, so the POST is in the middle of the window
  // rather than last; looking only at the tail would miss it.
  const window = cfgFetches.slice(beforeEdit);
  const post = window.find(f => f.opts && f.opts.method === "POST");
  if (!post || post.url !== "/api/config") {
    failed++; console.error("FAIL save did not POST /api/config");
  } else {
    const sent = JSON.parse(post.opts.body);
    if (sent.file !== "server" || sent.edits["economy.TAX_RATE"] !== "0.05") {
      failed++; console.error("FAIL save body wrong: " + post.opts.body);
    }
  }
  // An off-box viewer gets the same fields with writes refused: the dashboard
  // binds 0.0.0.0, so the client has to honour the server's verdict.
  vm.runInContext("cfgData.editable = false; renderConfigEditor()", sandbox);
  if (els["cfgSave"].disabled !== true || !els["cfgPresets"]._html.includes("disabled")) {
    failed++; console.error("FAIL read-only payload still offered a save");
  }
  const beforeReadOnly = cfgFetches.length;
  await vm.runInContext("saveConfig()", sandbox);
  if (cfgFetches.length !== beforeReadOnly) {
    failed++; console.error("FAIL save fired on a non-editable payload");
  }
  // A dropped fetch must not wipe the form the operator is halfway through.
  sandbox.CFG_FIXTURE_2 = CFG_FIXTURE;
  vm.runInContext('cfgFile = "server"; cfgData = CFG_FIXTURE_2', sandbox);
  cfgPayload = null;
  sandbox.fetch = async () => { throw new Error("boom"); };
  await vm.runInContext("loadConfig()", sandbox);
  if (!els["cfgMsg"]._text.includes("could not load config")) {
    failed++; console.error("FAIL load error not surfaced: " + els["cfgMsg"]._text);
  }
  if (vm.runInContext("cfgData === null", sandbox)) {
    failed++; console.error("FAIL a failed load dropped the last good schema");
  }
  sandbox.fetch = async (url, opts) => {
    cfgFetches.push({ url, opts });
    return { ok: true, status: 200, json: async () => CFG_FIXTURE };
  };
}

async function geoCase() {
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

  if (failed) { console.error("PROVE_FAIL (" + failed + ")"); process.exit(1); }
  console.log("PROVE_OK (live+empty+idempotent+seq+sortflip+recipe+chain+readouts+steam+commissions+hover+theme+runs+config)");
}

configCase().then(geoCase).catch(e => {
  console.error("SMOKE_THREW " + ((e && e.stack) || e));
  process.exit(1);
});
