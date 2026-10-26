"""Run the MCP harness over stdio for one game character."""

import argparse
import asyncio
import ipaddress
import os
import sys
from urllib.parse import urlparse

from harness.bridge import BridgeError, GameBridge
from harness.mcp_stdio import McpServer


def _is_loopback(host):
    host = (host or "").lower()
    if host == "localhost" or host.endswith(".localhost"):
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _parser():
    parser = argparse.ArgumentParser(
        prog="python -m harness",
        description="MCP stdio bridge for one Text MMO character.",
    )
    parser.add_argument("--url", default="ws://127.0.0.1:8765")
    parser.add_argument("--name", required=True, help="Character name to log in as.")
    parser.add_argument(
        "--token-env",
        help="Environment variable holding the login token. Preferred over argv.",
    )
    parser.add_argument(
        "--allow-remote",
        action="store_true",
        help="Permit a non-loopback ws:// host (plaintext transport).",
    )
    parser.add_argument("--connect-timeout", type=float, default=5.0)
    return parser


def _validate_url(parser, url, allow_remote):
    parsed = urlparse(url)
    if parsed.scheme not in ("ws", "wss") or not parsed.hostname:
        parser.error("--url must be ws:// or wss:// with a host")
    if not allow_remote and not _is_loopback(parsed.hostname):
        parser.error(
            f"{parsed.hostname} is not loopback; the game speaks plaintext ws://. "
            "Pass --allow-remote to accept the risk."
        )


async def _run(args, token):
    bridge = GameBridge(
        args.url,
        args.name,
        token,
        connect_timeout=args.connect_timeout,
    )
    try:
        await bridge.connect()
    except BridgeError as exc:
        print(f"harness: login failed: {exc}", file=sys.stderr)
        await bridge.close()
        return 2
    try:
        await McpServer(bridge).serve()
    finally:
        await bridge.close()
    return 0


def main(argv=None):
    """Parse CLI arguments and run the stdio MCP bridge over the game character."""
    parser = _parser()
    args = parser.parse_args(argv)
    if not args.name.strip():
        parser.error("--name must not be empty")
    if args.name.strip().lower() == "adventurers guild":
        parser.error("that name is reserved by the server")
    args.name = args.name.strip()
    _validate_url(parser, args.url, args.allow_remote)

    token = None
    if args.token_env:
        token = os.environ.get(args.token_env)
        if not token:
            parser.error(f"environment variable {args.token_env} is not set")
    try:
        return asyncio.run(_run(args, token))
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
