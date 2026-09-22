"""Headless-Firefox smoke for the canonical dashboard (CI browser proof).

Uses Playwright with its BUNDLED Firefox (downloaded into the tool cache
by `python -m playwright install firefox`) - never a system browser, and
headless-only always (browser rule, owner 09-22). Loads / (and the /v2
alias) with one live bot moving: fails on console errors or uncaught page
errors, requires the status pill to read exactly "live" (no render-issue
banner), requires trend canvases present, and always writes screenshots +
DOM + console log under dashshots/ for the CI artifact upload.

Run: python tests/dash_browser_smoke.py [--base URL] [--ws URL]
  (CI starts the server first, same as the live job.)
Needs: playwright + firefox engine (`python -m playwright install
  firefox`), websockets (pip).
"""

import argparse
import asyncio
import json
import os
import urllib.request

import websockets
import websockets.exceptions
from playwright.sync_api import TimeoutError as PlaywrightTimeout
from playwright.sync_api import sync_playwright


async def bot(ws_url):
    # Drain/read timeouts and dropped sockets end the bot quietly; anything
    # else fails loudly (CI must not swallow real breakage).
    quiet = (
        asyncio.TimeoutError,
        ConnectionResetError,
        OSError,
        websockets.exceptions.ConnectionClosed,
    )
    async with websockets.connect(ws_url) as ws:
        await ws.send(json.dumps({"cmd": "login", "name": "SmokeBot"}))
        for _ in range(5):
            try:
                await asyncio.wait_for(ws.recv(), timeout=2)
            except quiet:
                break
        for direction in ("north", "south", "east"):
            await ws.send(json.dumps({"cmd": "move", "dir": direction}))
            try:
                await asyncio.wait_for(ws.recv(), timeout=2)
            except quiet:
                pass
            await asyncio.sleep(1)
    print("SMOKE_BOT_OK", flush=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:8766")
    ap.add_argument("--ws", default="ws://127.0.0.1:8765")
    ap.add_argument("--shots", default="dashshots")
    args = ap.parse_args()

    os.makedirs(args.shots, exist_ok=True)

    # Both routes serve the canonical dashboard, byte-identical.
    root = urllib.request.urlopen(args.base + "/", timeout=10).read()
    alias = urllib.request.urlopen(args.base + "/v2", timeout=10).read()
    assert root == alias and len(root) > 10000, "route mismatch"
    print(f"ROUTES_OK ({len(root)} bytes)", flush=True)

    asyncio.run(bot(args.ws))

    console_errors = []
    page_errors = []
    dom = ""
    shot = os.path.join(args.shots, "overview.png")
    with sync_playwright() as pw:
        browser = pw.firefox.launch(headless=True)
        page = browser.new_page(viewport={"width": 1280, "height": 2200})
        page.on(
            "console",
            lambda msg: console_errors.append(msg) if msg.type == "error" else None,
        )
        page.on("pageerror", lambda err: page_errors.append(str(err)))
        # The dump can win the race with the page's first fetch: reload
        # until the status pill reads live (or attempts run out).
        for attempt in range(6):
            page.goto(args.base + "/", wait_until="load", timeout=30000)
            try:
                page.wait_for_function(
                    "document.getElementById('status').textContent === 'live'",
                    timeout=15000,
                )
                break
            except PlaywrightTimeout:
                print(f"RETRY {attempt}: status not live yet", flush=True)
        dom = page.content()
        page.screenshot(path=os.path.abspath(shot), full_page=True)
        browser.close()
    with open(os.path.join(args.shots, "dom.html"), "w", encoding="utf-8") as f:
        f.write(dom)
    with open(os.path.join(args.shots, "console.log"), "w", encoding="utf-8") as f:
        f.write("\n".join([m.text for m in console_errors] + page_errors))

    print(
        f"CONSOLE_ERRORS: {len(console_errors)} PAGE_ERRORS: {len(page_errors)}",
        flush=True,
    )
    for m in console_errors[:5]:
        print("  CONSOLE> " + m.text[-200:], flush=True)
    for e in page_errors[:5]:
        print("  PAGEERROR> " + e[-200:], flush=True)
    assert (
        not console_errors
    ), f"browser console errors: {[m.text for m in console_errors][:3]}"
    assert not page_errors, f"uncaught page errors: {page_errors[:3]}"
    print("CONSOLE_CLEAN_OK", flush=True)

    import re as _re

    m = _re.search(r'<span id="status"[^>]*>(.*?)</span>', dom)
    assert m and m.group(1) == "live", f"status pill: {m.group(1) if m else 'missing'}"
    print("STATUS_LIVE_OK", flush=True)

    n_canvas = dom.count("<canvas")
    assert n_canvas >= 4, f"only {n_canvas} canvases rendered"
    print(f"CANVAS_OK ({n_canvas})", flush=True)

    assert os.path.exists(shot) and os.path.getsize(shot) > 10000, "screenshot missing"
    print("SHOT_OK", flush=True)
    print("BROWSER_SMOKE_OK", flush=True)


main()
