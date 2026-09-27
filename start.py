"""Cross-platform launcher for the text MMO engine + bots (stdlib only).

Replaces all 7 Windows-only .bat launchers with subcommands that work on
Windows, Linux, and Debian. `serve` keeps start.bat parity (banner,
verbose logs, default ports) and adds what the .bat could not do: an
optional bot farm alongside the server and fail-fast port-taken errors.

Usage:
    python start.py                                   # serve, defaults
    python start.py serve --roles "gather:8,dungeon:4,market:3,commissioner:2"
    python start.py botfarm -- --bots 8 --steps 10000  # extra args pass through
    python start.py client -- --name MLAgent --steps 5000
    python start.py torch-bot [-- --demo]
    python start.py torch-farm [-- --agents 4 --steps 1000000]
    python start.py torch-batch-loop [steps] [rounds]
"""

import argparse
import os
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
PYTHON = sys.executable or "python3"

GAME_PORT = 8765
HTTP_PORT = 8766
GM_PORT = 8767
DEFAULT_URL = "ws://localhost:8765"

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
                f"error: server exited early with code {proc.poll()}.",
                file=sys.stderr,
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


def stop(proc):
    if proc is not None and proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()


def run_child(script, args, env_extra=None, banner_lines=()):
    """Run a repo script once, echoing its exit code (bat parity)."""
    for line in banner_lines:
        print(line)
    env = dict(os.environ)
    env["PYTHONUNBUFFERED"] = "1"
    env.update(env_extra or {})
    proc = subprocess.Popen([PYTHON, script] + args, cwd=HERE, env=env)
    try:
        code = proc.wait()
    except KeyboardInterrupt:
        print()
        stop(proc)
        return 0
    print()
    print(f"exited with code {code}.")
    return code or 0


def cmd_serve(args):
    os.environ["TEXTMMO_VERBOSE"] = "1"
    print("=" * 60)
    print(" Text MMO Engine + Live Dashboard")
    print("=" * 60)
    print()
    print(f"  Game server:    ws://localhost:{args.port}")
    print(f"  Live dashboard: http://localhost:{args.http_port}/")
    print(
        f"  GM stream:      ws://127.0.0.1:{args.gm_port}"
        " (dashboard GM tab, loopback-only)"
    )
    print()
    print("  Train the ML bot:    python start.py client    (single agent)")
    print("                       python start.py botfarm   (N concurrent bots)")
    print("                       python start.py torch-bot      (PyTorch DQN, single)")
    print(
        "                       python start.py torch-farm     (PyTorch DQN, N agents)"
    )
    print("                       python start.py torch-batch-loop (4 agents, batches)")
    print("  GM console:          the dashboard's GM tab  (local, no auth)")
    print()
    if args.roles:
        print(
            f"  Bot farm:          roles={args.roles}"
            f" bots={args.bots or 'farm default'}"
        )
        print()
    print("  This window shows live logs (every command, HTTP hits,")
    print("  connections). Press Ctrl+C to stop the whole thing.")
    print("=" * 60)
    print()
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
            stop(server)
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
            env = dict(os.environ)
            env["PYTHONUNBUFFERED"] = "1"
            farm = subprocess.Popen(farm_cmd, cwd=HERE, env=env)
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
        stop(farm)
        stop(server)


def cmd_client(args):
    return run_child(
        os.path.join(HERE, "ml", "ml_client.py"),
        ["--url", args.url] + args.extra,
        banner_lines=(
            "=" * 60,
            " ML Client - online Q-learning agent (trains a character)",
            "=" * 60,
            "",
            " Make sure the engine is running (python start.py).",
            "",
        ),
    )


def cmd_botfarm(args):
    print("=" * 60)
    print(" ML Bot Farm - train bots, restart only on normal exit")
    print("=" * 60)
    print()
    print(" Make sure the engine is running (python start.py).")
    print(" Default: 4 bots until stopped.")
    print()
    base = ["--url", args.url] + args.extra
    while True:
        code = run_child(os.path.join(HERE, "ml", "ml_botfarm.py"), base)
        if code != 0:
            print("Bot farm stopped.")
            return code
        print()
        print("Restarting bot farm in 5 seconds... (Ctrl+C stops it)")
        try:
            time.sleep(5)
        except KeyboardInterrupt:
            print()
            print("Bot farm stopped.")
            return 0


def cmd_torch_bot(args):
    return run_child(
        os.path.join(HERE, "torch_agents", "dqn_agent.py"),
        ["--url", args.url] + args.extra,
        banner_lines=(
            "=" * 60,
            " PyTorch DQN Agent for Text MMO",
            "=" * 60,
            "",
            " Requires: server running (python start.py) and PyTorch installed.",
            "",
        ),
    )


def cmd_torch_farm(args):
    return run_child(
        os.path.join(HERE, "torch_agents", "torch_farm.py"),
        ["--url", args.url] + args.extra,
        banner_lines=(
            "=" * 60,
            " Shared Torch DQN Farm",
            "=" * 60,
            " Four agents share one policy and one checkpoint writer.",
            " Server: ws://localhost:8765 (must be running first)",
            "",
        ),
    )


def cmd_torch_batch_worker(args):
    """One batch worker: run a named agent, always write the done marker."""
    code = run_child(
        os.path.join(HERE, "torch_agents", "dqn_agent.py"),
        ["--name", args.name, "--steps", str(args.steps), "--url", args.url],
    )
    try:
        with open(args.done_file, "w") as f:
            f.write(str(code))
    except OSError as e:
        print(f"warning: could not write done marker: {e}", file=sys.stderr)
    return code


def cmd_torch_batch_loop(args):
    steps = args.steps if args.steps >= 1 else 2000
    rounds = max(args.rounds, 0)
    run_dir = tempfile.mkdtemp(prefix="textmmo_torch_batch_")
    print("=" * 60)
    print(" Torch Agent Batch Loop")
    print("=" * 60)
    print()
    print(" Server:       ws://localhost:8765 (must be running first)")
    print(" Batch size:   4 concurrent agents")
    print(f" Steps/agent:  {steps}")
    print(f" Rounds:       {rounds} (0 = forever)")
    print(f" Logs/markers: {run_dir}")
    print()
    print(" Each completed batch is replaced by four fresh characters.")
    print(" Press Ctrl+C to stop the loop and its current agents.")
    print("=" * 60)
    print()
    procs = []
    try:
        rnd = 0
        while rounds <= 0 or rnd < rounds:
            names = [f"TorchBatch{rnd}_{i}" for i in range(4)]
            print(f"Starting batch {rnd} ...")
            procs = []
            for name in names:
                log = os.path.join(run_dir, name + ".log")
                done = os.path.join(run_dir, name + ".done")
                for stale in (done,):
                    try:
                        os.remove(stale)
                    except OSError:
                        pass
                print(f"  Launching {name} > {log}")
                with open(log, "w") as lf:
                    procs.append(
                        subprocess.Popen(
                            [
                                PYTHON,
                                os.path.join(HERE, "start.py"),
                                "torch-batch-worker",
                                name,
                                str(steps),
                                done,
                                "--url",
                                args.url,
                            ],
                            cwd=HERE,
                            stdout=lf,
                            stderr=subprocess.STDOUT,
                        )
                    )
            while True:
                try:
                    time.sleep(5)
                except KeyboardInterrupt:
                    print()
                    print("Stopping loop and current agents...")
                    return 0
                finished = sum(
                    1
                    for n in names
                    if os.path.exists(os.path.join(run_dir, n + ".done"))
                )
                if finished >= 4:
                    break
            print(f"Batch {rnd} finished. Logs are in {run_dir}.")
            rnd += 1
        print(f"Requested batch count reached. Logs are in {run_dir}.")
        return 0
    finally:
        for p in procs:
            stop(p)


def parse_args(argv=None):
    ap = argparse.ArgumentParser(
        description="Cross-platform launcher for the text MMO (replaces all .bat files)."
    )
    sub = ap.add_subparsers(dest="cmd")

    p_serve = sub.add_parser(
        "serve", help="engine + dashboard (default, start.bat parity)"
    )
    p_serve.add_argument("--port", type=int, default=GAME_PORT)
    p_serve.add_argument("--http-port", type=int, default=HTTP_PORT)
    p_serve.add_argument("--gm-port", type=int, default=GM_PORT)
    p_serve.add_argument("--roles", default=None)
    p_serve.add_argument("--bots", type=int, default=None)
    p_serve.set_defaults(func=cmd_serve)

    def add_runner(name, help_text, func, url=True):
        p = sub.add_parser(name, help=help_text)
        if url:
            p.add_argument("--url", default=DEFAULT_URL)
        p.add_argument("extra", nargs=argparse.REMAINDER)
        p.set_defaults(func=func)
        return p

    add_runner("client", "single Q-learning agent (ml_client.bat parity)", cmd_client)
    add_runner("botfarm", "N concurrent bots, restart on clean exit", cmd_botfarm)
    add_runner("torch-bot", "PyTorch DQN single agent", cmd_torch_bot)
    add_runner("torch-farm", "PyTorch DQN shared farm", cmd_torch_farm)

    p_loop = sub.add_parser("torch-batch-loop", help="batches of 4 torch agents")
    p_loop.add_argument("--url", default=DEFAULT_URL)
    p_loop.add_argument("steps", type=int, nargs="?", default=2000)
    p_loop.add_argument("rounds", type=int, nargs="?", default=0)
    p_loop.set_defaults(func=cmd_torch_batch_loop)

    p_worker = sub.add_parser(
        "torch-batch-worker", help="one batch worker (spawned by the loop)"
    )
    p_worker.add_argument("--url", default=DEFAULT_URL)
    p_worker.add_argument("name")
    p_worker.add_argument("steps", type=int)
    p_worker.add_argument("done_file")
    p_worker.set_defaults(func=cmd_torch_batch_worker)

    # No subcommand: serve with top-level serve flags (backward compatible).
    ap.add_argument("--port", type=int, default=GAME_PORT)
    ap.add_argument("--http-port", type=int, default=HTTP_PORT)
    ap.add_argument("--gm-port", type=int, default=GM_PORT)
    ap.add_argument("--roles", default=None)
    ap.add_argument("--bots", type=int, default=None)
    args = ap.parse_args(argv)
    if args.cmd is None:
        args.func = cmd_serve
    extra = getattr(args, "extra", None)
    if extra and extra[0] == "--":
        args.extra = extra[1:]  # REMAINDER keeps the separator; children don't need it
    return args


def main(argv=None):
    args = parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
