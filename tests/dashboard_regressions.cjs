// Deterministic regressions for the GM socket and visibility handler in
// dashboard.html (#1020, #1022). Runs the real inline blocks against stubs:
// no browser, no network, no timers. Each case also runs against a reverted
// copy of the block so a vacuous harness fails the build.
//
// Run from the repo root: node tests/dashboard_regressions.cjs
const fs = require("fs");
const vm = require("vm");
const path = require("path");

const ROOT = path.join(__dirname, "..");
const html = fs.readFileSync(path.join(ROOT, "dashboard.html"), "utf8");

function extract(startMarker, endMarker) {
  const start = html.indexOf(startMarker);
  if (start < 0) throw new Error("missing block start: " + startMarker);
  const end = html.indexOf(endMarker, start);
  if (end < 0) throw new Error("missing block end: " + endMarker);
  return html.slice(start, end + endMarker.length);
}

const declStart = html.indexOf("let gmws = null;");
const declEnd = html.indexOf("function gmlog", declStart);
if (declStart < 0 || declEnd < 0) throw new Error("GM declarations not found");
const GM_SOURCE =
  html.slice(declStart, declEnd) +
  extract("function gmOpen() {", "\n// ---- GM action panes");
const VIS_SOURCE = extract(
  'document.addEventListener("visibilitychange", () => {',
  "\n  });"
);
if (!GM_SOURCE.includes("function gmSend(payload)")) {
  throw new Error("gmSend was not extracted");
}

// The reverted GM block restores the four failure modes from #1020: no
// handshake guard, send via the global socket, unconditional onclose clear,
// and onerror closing whatever the global points at.
const BUGGY_GM = GM_SOURCE.replace(
  "  if (gmws && gmws.readyState !== WebSocket.CLOSED &&\n" +
    "      gmws.readyState !== WebSocket.CLOSING) return;\n",
  ""
)
  .replace(
    '    const ws = new WebSocket(`ws://${host}:${port}`);\n    gmws = ws;',
    '    gmws = new WebSocket(`ws://${host}:${port}`);\n    const ws = gmws;'
  )
  .replace(
    "      const sock = (ev && ev.target) || ws;\n" +
      "      if (sock !== gmws || sock.readyState !== WebSocket.OPEN) return;",
    "      const sock = gmws;"
  )
  .replace(
    "for (const m of gmBuf.splice(0)) sock.send(JSON.stringify(m));",
    "for (const m of gmBuf.splice(0)) gmws.send(JSON.stringify(m));"
  )
  .replace("      if (!ev || ev.target === gmws) gmws = null;", "      gmws = null;")
  .replace(
    "      const sock = (ev && ev.target) || gmws;\n" +
      "      if (sock && sock === gmws) sock.close();",
    "      if (gmws) gmws.close();"
  );
if (
  BUGGY_GM === GM_SOURCE ||
  BUGGY_GM.includes("sock !== gmws") ||
  BUGGY_GM.includes("ev.target === gmws")
) {
  throw new Error("GM negative control was not reverted");
}

function makeGMContext(source) {
  const sockets = [];
  const sent = [];
  const thrown = [];
  const els = {};

  function FakeSocket(url) {
    this.url = url;
    this.readyState = FakeSocket.CONNECTING;
    sockets.push(this);
  }
  FakeSocket.CONNECTING = 0;
  FakeSocket.OPEN = 1;
  FakeSocket.CLOSING = 2;
  FakeSocket.CLOSED = 3;
  FakeSocket.prototype.send = function (payload) {
    if (this.readyState !== FakeSocket.OPEN) {
      const err = new Error("InvalidStateError: CONNECTING");
      thrown.push(err.message);
      throw err;
    }
    sent.push({ socket: this, payload: JSON.parse(payload) });
  };
  FakeSocket.prototype.open = function () {
    this.readyState = FakeSocket.OPEN;
    if (this.onopen) this.onopen({ target: this });
  };
  FakeSocket.prototype.closeEvent = function () {
    this.readyState = FakeSocket.CLOSED;
    if (this.onclose) this.onclose({ target: this });
  };
  FakeSocket.prototype.close = function () {
    if (this.readyState !== FakeSocket.CLOSED) {
      this.readyState = FakeSocket.CLOSING;
    }
  };

  const sandbox = {
    WebSocket: FakeSocket,
    JSON,
    Math,
    Date,
    Object,
    Array,
    String,
    console,
    location: { hostname: "127.0.0.1" },
    lastState: { server: { gm_port: 8767 } },
    $: id => (els[id] ||= { textContent: "", style: {} }),
    gmlog() {},
    tick() {},
  };
  vm.createContext(sandbox);
  vm.runInContext(source, sandbox, { filename: "gm-block.js" });
  return { sandbox, sockets, sent, thrown, FakeSocket };
}

function gmScenario(source) {
  const ctx = makeGMContext(source);
  const { sandbox, sockets, sent, thrown, FakeSocket } = ctx;
  const fail = [];
  const attempt = fn => {
    try {
      fn();
      return null;
    } catch (e) {
      return e.message;
    }
  };
  const active = () => vm.runInContext("gmws", sandbox);
  const buffer = () => vm.runInContext("gmBuf.length", sandbox);
  const send = text => attempt(() => vm.runInContext(text, sandbox));

  // Two clicks before the handshake completes: one socket, both queued.
  send('gmSend({cmd:"grant",amount:1}); gmSend({cmd:"grant",amount:2})');
  if (sockets.length !== 1) fail.push("sockets=" + sockets.length + " want 1");
  if (buffer() !== 2) fail.push("buffer=" + buffer() + " want 2");
  if (sent.length !== 0) fail.push("sent before open");
  const openErr = attempt(() => sockets[0].open());
  if (openErr) fail.push("onopen threw " + openErr);
  if (sent.length !== 2) fail.push("delivered=" + sent.length + " want 2");
  if (sent.some(entry => entry.socket !== sockets[0])) {
    fail.push("queued payload sent through the wrong socket");
  }
  const amounts = sent.map(entry => entry.payload.amount).join(",");
  if (amounts !== "1,2") fail.push("amounts=" + amounts + " want 1,2");
  if (buffer() !== 0) fail.push("buffer not drained on open");

  // An open socket sends directly without opening another one.
  send('gmSend({cmd:"heal",amount:3})');
  if (sockets.length !== 1) fail.push("open send created a second socket");
  if (sent.length !== 3 || sent[2].payload.cmd !== "heal") {
    fail.push("open send not delivered");
  }
  if (active() !== sockets[0]) fail.push("active socket drifted");

  // A real close clears the reference; the next action reconnects once.
  sockets[0].closeEvent();
  if (active() !== null) fail.push("close did not clear the active socket");
  send('gmSend({cmd:"teleport",amount:4})');
  if (sockets.length !== 2) fail.push("reconnect opened " + sockets.length + " sockets");
  const second = sockets[1];

  // A late close/error from the old socket must not touch the live one.
  attempt(() => sockets[0].onclose({ target: sockets[0] }));
  if (active() !== second) fail.push("stale close cleared the active socket");
  attempt(() => sockets[0].onerror({ target: sockets[0] }));
  if (second.readyState !== FakeSocket.CONNECTING) {
    fail.push("stale error closed the active socket");
  }
  attempt(() => second.open());
  if (sent.length !== 4 || sent[3].payload.amount !== 4) {
    fail.push("payload queued during reconnect not delivered once");
  }

  // A CLOSING socket is replaced; its late close must not clear the new ref.
  second.readyState = FakeSocket.CLOSING;
  send('gmSend({cmd:"slay",amount:5})');
  if (sockets.length !== 3) fail.push("CLOSING socket was not replaced once");
  const third = sockets[2];
  attempt(() => second.onclose({ target: second }));
  if (active() !== third) fail.push("late close cleared the replacement socket");
  const replacementErr = attempt(() => third.open());
  if (replacementErr) fail.push("replacement onopen threw " + replacementErr);
  if (sent.length !== 5 || sent[4].payload.amount !== 5) {
    fail.push("replacement did not deliver its queued payload");
  }
  if (thrown.length) fail.push("fake socket saw " + thrown.length + " invalid sends");
  return fail;
}

const BUGGY_VIS = VIS_SOURCE.replace(
  "    if (!document.hidden) tick();",
  "    if (!document.hidden && lastState) renderAll(lastState);"
);
if (BUGGY_VIS === VIS_SOURCE) {
  throw new Error("visibility negative control was not reverted");
}

function visibilityScenario(source, hidden) {
  const handlers = {};
  const calls = { tick: 0, render: 0, poll: 0 };
  const sandbox = {
    document: {
      hidden,
      addEventListener(name, fn) {
        (handlers[name] ||= []).push(fn);
      },
    },
    lastState: { server: {} },
    renderAll() {
      calls.render += 1;
    },
    tick() {
      calls.tick += 1;
    },
    schedulePoll() {
      calls.poll += 1;
    },
    console,
  };
  vm.createContext(sandbox);
  vm.runInContext(source, sandbox, { filename: "visibility-block.js" });
  const fail = [];
  if (!handlers.visibilitychange || handlers.visibilitychange.length !== 1) {
    fail.push("visibilitychange handler not bound");
    return fail;
  }
  handlers.visibilitychange[0]();
  const wantTick = hidden ? 0 : 1;
  if (calls.tick !== wantTick) {
    fail.push("tick calls=" + calls.tick + " want " + wantTick);
  }
  if (calls.poll !== 1) fail.push("schedulePoll calls=" + calls.poll + " want 1");
  return fail;
}

function report(label, failures) {
  if (failures.length) {
    for (const failure of failures) console.error("FAIL " + label + ": " + failure);
    process.exitCode = 1;
    return false;
  }
  console.log(label + "_OK");
  return true;
}

report("GM_CONNECT_QUEUE", gmScenario(GM_SOURCE));
const gmBug = gmScenario(BUGGY_GM);
if (gmBug.length) {
  console.log("GM_NEGATIVE_CONTROL_OK (" + gmBug.length + " failures detected)");
} else {
  console.error("FAIL GM negative control did not detect the old behavior");
  process.exitCode = 1;
}
report("VISIBILITY_REFRESH", visibilityScenario(VIS_SOURCE, false));
report("VISIBILITY_HIDDEN_NO_FETCH", visibilityScenario(VIS_SOURCE, true));
const visBug = visibilityScenario(BUGGY_VIS, false);
if (visBug.length) {
  console.log("VISIBILITY_NEGATIVE_CONTROL_OK (" + visBug.length + " failures detected)");
} else {
  console.error("FAIL visibility negative control did not detect the old behavior");
  process.exitCode = 1;
}
if (!process.exitCode) console.log("DASHBOARD_REGRESSIONS_OK");
