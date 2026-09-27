"""Cross-platform launcher for the text MMO engine + live dashboard.

Replaces start.bat (Windows-only) with one stdlib-only script that works
on Windows, Linux, and Debian: same banner, same verbose logs, same
default ports, plus two things the .bat could not do -- launch a scripted
bot farm alongside the server (--roles passthrough to ml/ml_botfarm.py)
and fail fast with an actionable message when a port is taken.

Usage:
    python start.py
    python start.py --roles "gather:8,dungeon:4,market:3,commissioner:2"
    python start.py --roles "market:4" --bots 4 --port 8770 --http-port 8771 --gm-port 8772
"""

import argparse
import os
import socket
import subprocess
import sys
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
PYTHON = sys.executable or "python3"

GAME_PORT = 8765
HTTP_PORT = 8766
GM_PORT = 8767

SERVICES = (
    ("game server", "--port"),
    ("dashboard", "--http-port"),
    ("GM stream", "--gm-port"),
)


def port_taken(port):
    """True when nothing can bind 127.0.0.1:port (i.e. it is in use)."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        try:
            s.bind(("127.0.0.1", port))
        except OSError:
            return True
    return False


def check_ports(game, http, gm):
    """Fail fast naming the taken service, port, and override flag."""
    taken = [
        (name, flag, port)
        for (name, flag), port in zip(SERVICES, (game, http, gm))
        if port_taken(port)
    ]
    if not taken:
        return True
    for name, flag, port in taken:
        print(f"error: port {port} ({name}) is already in use.", file=sys.stderr)
    print("Stop the other process, or move this server, e.g.:", file=sys.stderr)
    print(
        "  python start.py --port 8770 --http-port 8771 --gm-port 8772",
        file=sys.stderr,
    )
    return False


def wait_healthy(http_port, proc, timeout=15.0):
    """Wait for /health (or an early server exit) after launch."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if proc.poll() is not None:
            print(
                f"error: server exited early with code {proc.poll()}.", file=sys.stderr
            )
            return False
        try:
            with urllib.request.urlopen(
                f"http://127.0.0.1:{http_port}/health", timeout=2
            ) as r:
                if r.status == 200:
                    return True
        except OSError:
            time.sleep(0.5)
    print("error: server did not answer /health in time.", file=sys.stderr)
    return False


def parse_args(argv=None):
    ap = argparse.ArgumentParser(
        description="Start the text MMO engine + live dashboard (cross-platform start.bat)."
    )
    ap.add_argument("--port", type=int, default=GAME_PORT, help="game WebSocket port")
    ap.add_argument(
        "--http-port", type=int, default=HTTP_PORT, help="dashboard HTTP port"
    )
    ap.add_argument("--gm-port", type=int, default=GM_PORT, help="GM loopback port")
    ap.add_argument(
        "--roles",
        default=None,
        help='also launch bots, e.g. "gather:8,dungeon:4,market:3,commissioner:2"',
    )
    ap.add_argument(
        "--bots",
        type=int,
        default=None,
        help="bot count for --roles (default: the farm's own default)",
    )
    return ap.parse_args(argv)


def banner(args):
    print("=" * 60)
    print(" Text MMO Engine + Live Dashboard")
    print("=" * 60)
    print()
    print(f"  Game server:    ws://localhost:{args.port}")
    print(f"  Live dashboard: http://localhost:{args.http_port}/")
    print(
        f"  GM stream:      ws://127.0.0.1:{args.gm_port} (dashboard GM tab, loopback-only)"
    )
    print()
    print("  Train the ML bot:    ml/ml_client.py         (single agent)")
    print("                       ml/ml_botfarm.py        (N concurrent bots)")
    print(
        "                       torch_agents/torch_bot.py      (PyTorch DQN, single agent)"
    )
    print(
        "                       torch_agents/torch_farm.py     (PyTorch DQN, N agents, shared policy)"
    )
    print(
        "                       torch_agents/torch_batch_loop.py (4 agents, repeat batches)"
    )
    print("  GM console:          the dashboard's GM tab  (local, no auth)")
    print()
    if args.roles:
        print(
            f"  Bot farm:          roles={args.roles} bots={args.bots or 'farm default'}"
        )
        print()
    print("  This window shows live logs (every command, HTTP hits,")
    print("  connections). Press Ctrl+C to stop the whole thing.")
    print("=" * 60)
    print()


def stop(proc, name):
    if proc is not None and proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()


def main(argv=None):
    args = parse_args(argv)
    # Verbose mode, like start.bat: every client command, HTTP request,
    # and connect/disconnect event goes to the log.
    os.environ["TEXTMMO_VERBOSE"] = "1"
    banner(args)
    if not check_ports(args.port, args.http_port, args.gm_port):
        return 2
    server = subprocess.Popen(
        [
            PYTHON,
            os.path.join(HERE, "server.py"),
            "--port",
            str(args.port),
            "--http-port",
            str(args.http_port),
            "--gm-port",
            str(args.gm_port),
        ],
        cwd=HERE,
    )
    farm = None
    try:
        if not wait_healthy(args.http_port, server):
            stop(server, "server")
            return 1
        if args.roles:
            farm_cmd = [
                PYTHON,
                os.path.join(HERE, "ml", "ml_botfarm.py"),
                "--roles",
                args.roles,
                "--url",
                f"ws://127.0.0.1:{args.port}",
                "--name-prefix",
                "StartFarm",
            ]
            if args.bots is not None:
                farm_cmd += ["--bots", str(args.bots)]
            farm = subprocess.Popen(farm_cmd, cwd=HERE)
            print(f"bot farm starting (roles={args.roles})")
        server.wait()
        print()
        print("Server stopped (it may have crashed, or the port is in use).")
        return server.returncode or 0
    except KeyboardInterrupt:
        print()
        print("Stopping...")
        return 0
    finally:
        stop(farm, "farm")
        stop(server, "server")


if __name__ == "__main__":
    sys.exit(main())
