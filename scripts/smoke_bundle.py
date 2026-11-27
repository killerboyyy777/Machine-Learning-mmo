"""Smoke test for a standalone server bundle (#841).

Extracts the zip, starts the executable on spare ports, checks the game
port and dashboard, then stops the process. Run from the repo root:

    python scripts/smoke_bundle.py --bundle-dir dist/bundle
"""

import argparse
import base64
import hashlib
import json
import os
import socket
import subprocess
import tempfile
import time
import urllib.request
import zipfile
from pathlib import Path

GAME_PORT = 18765
HTTP_PORT = 18766
GM_PORT = 18767
START_TIMEOUT_SECONDS = 60
EXIT_TIMEOUT_SECONDS = 15
POLL_SECONDS = 0.25
WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


def find_exe(root):
    matches = [
        path
        for path in root.rglob("*")
        if path.is_file()
        and path.name.startswith("small-text-mmo")
        and (path.suffix == ".exe" or path.suffix == "")
    ]
    if len(matches) != 1:
        raise RuntimeError(f"expected one bundled executable, got {matches}")
    exe = matches[0]
    if os.name != "nt":
        exe.chmod(exe.stat().st_mode | 0o111)
    return exe


def check_ports_free():
    for port in (GAME_PORT, HTTP_PORT, GM_PORT):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            try:
                sock.bind(("127.0.0.1", port))
            except OSError as exc:
                raise RuntimeError(f"port {port} is already in use") from exc


def http_get(port, path):
    url = f"http://127.0.0.1:{port}{path}"
    with urllib.request.urlopen(url, timeout=2) as response:
        return response.status, response.read()


def game_handshake(port):
    key = base64.b64encode(os.urandom(16)).decode("ascii")
    request = (
        f"GET / HTTP/1.1\r\n"
        f"Host: 127.0.0.1:{port}\r\n"
        "Upgrade: websocket\r\n"
        "Connection: Upgrade\r\n"
        f"Sec-WebSocket-Key: {key}\r\n"
        "Sec-WebSocket-Version: 13\r\n"
        "\r\n"
    )
    with socket.create_connection(("127.0.0.1", port), timeout=2) as sock:
        sock.settimeout(2)
        sock.sendall(request.encode("ascii"))
        response = b""
        while b"\r\n\r\n" not in response and len(response) < 4096:
            chunk = sock.recv(4096)
            if not chunk:
                break
            response += chunk
    head = response.split(b"\r\n\r\n", 1)[0].decode("latin-1")
    if not head.startswith("HTTP/1.1 101"):
        raise RuntimeError(f"game port did not upgrade: {head!r}")
    expected = base64.b64encode(
        hashlib.sha1((key + WS_GUID).encode("ascii")).digest()
    ).decode("ascii")
    headers = {}
    for line in head.split("\r\n")[1:]:
        name, sep, value = line.partition(":")
        if sep:
            headers[name.strip().lower()] = value.strip()
    if headers.get("sec-websocket-accept") != expected:
        raise RuntimeError(f"bad WebSocket accept header: {headers}")


def check_dashboard():
    status, body = http_get(HTTP_PORT, "/health")
    if status != 200 or json.loads(body).get("status") != "ok":
        raise RuntimeError("dashboard health route did not report ok")
    status, body = http_get(HTTP_PORT, "/")
    if status != 200 or b"<!DOCTYPE html>" not in body:
        raise RuntimeError("dashboard HTML is missing")
    if b"/uplot.min.js" not in body:
        raise RuntimeError("dashboard HTML does not reference uplot")
    status, _ = http_get(HTTP_PORT, "/uplot.min.js")
    if status != 200:
        raise RuntimeError("bundled uplot asset did not load")


def wait_until_ready(proc):
    deadline = time.monotonic() + START_TIMEOUT_SECONDS
    last = "no probe ran"
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            raise RuntimeError(f"bundle exited early with {proc.returncode}")
        try:
            game_handshake(GAME_PORT)
            check_dashboard()
            return
        except (OSError, ValueError, RuntimeError) as exc:
            last = str(exc)
        time.sleep(POLL_SECONDS)
    raise RuntimeError(f"bundle did not become ready: {last}")


def stop(proc):
    if proc.poll() is not None:
        return proc.returncode
    if os.name == "nt":
        # A onefile bundle runs as a parent plus a child, and terminate()
        # signals only the parent. Kill both so the temp extraction is removable.
        subprocess.run(
            ["taskkill", "/T", "/F", "/PID", str(proc.pid)], capture_output=True
        )
    else:
        proc.terminate()
    try:
        return proc.wait(timeout=EXIT_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired:
        proc.kill()
        return proc.wait()


def run(bundle):
    check_ports_free()
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        work = root / "work"
        work.mkdir()
        with zipfile.ZipFile(bundle) as archive:
            archive.extractall(root)
        exe = find_exe(root)
        log = root / "server.log"
        env = dict(os.environ)
        env.pop("TEXTMMO_DATA_DIR", None)
        env["TEXTMMO_GM_SEED"] = "700"
        with open(log, "wb") as stream:
            proc = subprocess.Popen(
                [
                    str(exe),
                    "--host",
                    "127.0.0.1",
                    "--port",
                    str(GAME_PORT),
                    "--http-port",
                    str(HTTP_PORT),
                    "--gm-port",
                    str(GM_PORT),
                ],
                cwd=str(work),
                env=env,
                stdout=stream,
                stderr=subprocess.STDOUT,
            )
            try:
                wait_until_ready(proc)
                data_dir = root / "data"
                if not data_dir.is_dir():
                    raise RuntimeError(f"no data dir beside executable: {data_dir}")
                if (work / "scores.json").exists():
                    raise RuntimeError("state leaked into the launch directory")
            except BaseException:
                print(log.read_text(encoding="utf-8", errors="replace"))
                raise
            finally:
                code = stop(proc)
        if os.name != "nt" and code != 0:
            raise RuntimeError(f"bundle did not stop cleanly: exit {code}")
        if os.name != "nt" and not (root / "data" / "scores.json").is_file():
            raise RuntimeError("graceful shutdown did not save scores.json")
    print("BUNDLE_SMOKE_OK")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--bundle-dir", required=True, type=Path)
    args = parser.parse_args()
    if not args.bundle_dir.is_dir():
        raise SystemExit(f"bundle directory not found: {args.bundle_dir}")
    bundles = sorted(args.bundle_dir.glob("small-text-mmo-*.zip"))
    if len(bundles) != 1:
        raise SystemExit(f"expected one bundle, got {bundles}")
    try:
        run(bundles[0])
    except Exception as exc:
        raise SystemExit(f"BUNDLE_SMOKE_FAIL {exc}") from exc


if __name__ == "__main__":
    main()
