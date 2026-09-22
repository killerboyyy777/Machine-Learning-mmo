"""Headless-Chrome smoke for the canonical dashboard (CI browser proof).

Loads / (and the /v2 alias) in headless Chrome with one live bot moving:
fails on ANY browser console message, requires the status pill to read
exactly "live" (no render-issue banner), requires trend canvases present,
and always writes screenshots + DOM + console log under dashshots/ for
the CI artifact upload.

Run: python tests/dash_browser_smoke.py [--base URL] [--ws URL]
  (CI starts the server first, same as the live job.)
Needs: headless Chrome/Chromium on PATH, websockets (pip).
"""
import argparse
import asyncio
import json
import os
import re
import shutil
import subprocess
import sys
import urllib.request

import websockets

CHROME_CANDIDATES = ("google-chrome", "chromium", "chromium-browser",
                     r"C:\Program Files\Google\Chrome\Application\chrome.exe")


def find_chrome(explicit=None):
    if explicit:
        return explicit
    for name in CHROME_CANDIDATES:
        found = shutil.which(name)
        if found:
            return found
        if os.path.exists(name):
            return name
    raise SystemExit("no headless Chrome found "
                     f"(tried {', '.join(CHROME_CANDIDATES)})")


async def bot(ws_url):
    async with websockets.connect(ws_url) as ws:
        await ws.send(json.dumps({"cmd": "login", "name": "SmokeBot"}))
        for _ in range(5):
            try:
                await asyncio.wait_for(ws.recv(), timeout=2)
            except Exception:
                break
        for direction in ("north", "south", "east"):
            await ws.send(json.dumps({"cmd": "move", "dir": direction}))
            try:
                await asyncio.wait_for(ws.recv(), timeout=2)
            except Exception:
                pass
            await asyncio.sleep(1)
    print("SMOKE_BOT_OK")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:8766")
    ap.add_argument("--ws", default="ws://127.0.0.1:8765")
    ap.add_argument("--shots", default="dashshots")
    ap.add_argument("--chrome", default=None)
    args = ap.parse_args()

    os.makedirs(args.shots, exist_ok=True)

    # Both routes serve the canonical dashboard, byte-identical.
    root = urllib.request.urlopen(args.base + "/", timeout=10).read()
    alias = urllib.request.urlopen(args.base + "/v2", timeout=10).read()
    assert root == alias and len(root) > 10000, "route mismatch"
    print(f"ROUTES_OK ({len(root)} bytes)")

    asyncio.run(bot(args.ws))

    chrome = find_chrome(args.chrome)
    shot = os.path.join(args.shots, "overview.png")
    p = subprocess.run(
        [chrome, "--headless=new", "--disable-gpu", "--no-sandbox",
         "--window-size=1280,2200", "--timeout=45000",
         "--enable-logging=stderr", "--v=0",
         f"--screenshot={os.path.abspath(shot)}", "--dump-dom",
         args.base + "/"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=180)
    err = p.stderr.decode("utf-8", errors="replace")
    dom = p.stdout.decode("utf-8", errors="replace")
    with open(os.path.join(args.shots, "dom.html"), "w", encoding="utf-8") as f:
        f.write(dom)
    with open(os.path.join(args.shots, "console.log"), "w", encoding="utf-8") as f:
        f.write(err)

    console_lines = [l for l in err.splitlines() if "CONSOLE" in l]
    assert not console_lines, f"browser console not clean: {console_lines[:5]}"
    print("CONSOLE_CLEAN_OK")

    m = re.search(r'<span id="status"[^>]*>(.*?)</span>', dom)
    assert m and m.group(1) == "live", f"status pill: {m.group(1) if m else 'missing'}"
    print("STATUS_LIVE_OK")

    n_canvas = dom.count("<canvas")
    assert n_canvas >= 4, f"only {n_canvas} canvases rendered"
    print(f"CANVAS_OK ({n_canvas})")

    assert os.path.exists(shot) and os.path.getsize(shot) > 10000, "screenshot missing"
    print("SHOT_OK")
    print("BROWSER_SMOKE_OK")


main()
