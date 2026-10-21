"""Game bridge for the MCP harness: one character over the WebSocket protocol."""

import asyncio
import json
import time

import websockets

PROTOCOL_VERSION = 2
QUIET_SECONDS = 0.12
RESULT_EVENT_LIMIT = 50
RESULT_EVENT_CHARS = 16000
TEXT_LIMIT = 2000


class BridgeError(Exception):
    """Base class for bridge failures."""


class LoginError(BridgeError):
    """The login handshake did not complete."""


class GameBridge:
    """A single logged-in character with serialized commands and a cache.

    The game protocol has no request ids, so commands must not overlap: every
    call takes the lock, drains stale broadcasts, sends one command, and
    settles on the last matching frame after a quiet window.
    """

    def __init__(
        self,
        url,
        name,
        token=None,
        *,
        connect_timeout=5.0,
        command_timeout=8.0,
        move_timeout=15.0,
    ):
        self.url = url
        self.name = name
        self._token = token
        self.connect_timeout = connect_timeout
        self.command_timeout = command_timeout
        self.move_timeout = move_timeout
        self._ws = None
        self._lock = asyncio.Lock()
        self._seq = 0
        self.cache = {"room": None, "stats": None}
        self._cache_at = {}

    async def connect(self):
        """Open the socket and complete login (welcome + room + stats)."""
        if self._ws is not None:
            return
        try:
            self._ws = await websockets.connect(
                self.url,
                open_timeout=self.connect_timeout,
                ping_interval=20,
                ping_timeout=20,
                close_timeout=2,
                max_size=2**20,
            )
        except (OSError, websockets.WebSocketException) as exc:
            raise LoginError(f"connect failed: {exc}") from exc

        login = {
            "cmd": "login",
            "name": self.name,
            "protocol_version": PROTOCOL_VERSION,
        }
        if self._token:
            login["token"] = self._token
        try:
            await self._ws.send(json.dumps(login))
        except websockets.WebSocketException as exc:
            raise LoginError(f"login send failed: {exc}") from exc

        deadline = time.monotonic() + self.connect_timeout
        while time.monotonic() < deadline:
            frame = await self._recv_frame(max(0.01, deadline - time.monotonic()))
            if frame is None:
                continue
            self._observe(frame)
            ftype = frame.get("type")
            if ftype == "error":
                raise LoginError(str(frame.get("text", "login rejected")))
            if ftype == "welcome":
                version = frame.get("protocol_version")
                if version != PROTOCOL_VERSION:
                    raise LoginError(
                        f"server speaks protocol {version}, harness needs {PROTOCOL_VERSION}"
                    )
                if frame.get("version_mismatch"):
                    raise LoginError(str(frame["version_mismatch"]))
            if self.cache["room"] is not None and self.cache["stats"] is not None:
                return
        raise LoginError("login did not return room and stats before the timeout")

    async def close(self):
        ws, self._ws = self._ws, None
        if ws is not None:
            await ws.close()

    async def command(self, cmd, params=None, *, expect, timeout=None):
        """Run one command and settle on the last expected frame."""
        if self._ws is None:
            raise BridgeError("not connected")
        if timeout is None:
            timeout = self.move_timeout if cmd == "move" else self.command_timeout

        async with self._lock:
            await self._drain()
            payload = {"cmd": cmd}
            payload.update(params or {})
            await self._ws.send(json.dumps(payload))

            deadline = time.monotonic() + timeout
            quiet_until = None
            terminal = None
            seen = []
            while time.monotonic() < deadline:
                wait = min(0.5, max(0.01, deadline - time.monotonic()))
                if quiet_until is not None:
                    wait = min(wait, max(0.01, quiet_until - time.monotonic()))
                frame = await self._recv_frame(wait)
                if frame is not None:
                    entry = self._observe(frame)
                    seen.append(entry)
                    ftype = frame.get("type")
                    if ftype == "error":
                        terminal = frame
                        break
                    if ftype in expect:
                        terminal = frame
                        quiet_until = time.monotonic() + QUIET_SECONDS
                if quiet_until is not None and time.monotonic() >= quiet_until:
                    break

            if terminal is None:
                return self._result(
                    cmd, seen, f"timed out after {timeout:g}s waiting for {expect}"
                )
            if terminal.get("type") == "error":
                return self._result(
                    cmd, seen, str(terminal.get("text", "command failed"))
                )
            return self._result(cmd, seen)

    def summary(self):
        stats = self.cache["stats"] or {}
        room = self.cache["room"] or {}
        return {
            "name": self.name,
            "room": room.get("id"),
            "room_name": room.get("name"),
            "hp": stats.get("hp"),
            "max_hp": stats.get("max_hp"),
            "gold": stats.get("gold"),
            "level": stats.get("level"),
            "pack": stats.get("pack"),
            "pack_max": stats.get("pack_max"),
            "party_size": stats.get("party_size"),
        }

    def resource_room(self):
        room = self.cache["room"]
        at = self._cache_at.get("room")
        return {
            "available": room is not None,
            "age_ms": int((time.monotonic() - at) * 1000) if at else None,
            "room": room,
        }

    def _result(self, cmd, seen, error=None):
        events, truncated = _clip_events(seen)
        return {
            "ok": error is None,
            "command": cmd,
            "events": events,
            "state": self.summary(),
            "error": error,
            "truncated": truncated,
        }

    async def _drain(self):
        for _ in range(100):
            frame = await self._recv_frame(0.005)
            if frame is None:
                return
            self._observe(frame)

    async def _recv_frame(self, timeout):
        try:
            raw = await asyncio.wait_for(self._ws.recv(), timeout)
        except TimeoutError:
            return None
        except websockets.ConnectionClosed as exc:
            self._ws = None
            raise BridgeError(f"connection closed: {exc}") from exc
        try:
            frame = json.loads(raw)
        except (TypeError, ValueError):
            return None
        return frame if isinstance(frame, dict) else None

    def _observe(self, frame):
        self._seq += 1
        entry = {"seq": self._seq, "type": frame.get("type")}
        for key, value in frame.items():
            if key == "type":
                continue
            entry[key] = _clip_text(value)

        ftype = frame.get("type")
        if ftype in ("room", "stats"):
            self.cache[ftype] = frame
            self._cache_at[ftype] = time.monotonic()
        return entry


def _clip_text(value):
    if isinstance(value, str) and len(value) > TEXT_LIMIT:
        return value[:TEXT_LIMIT] + "..."
    return value


def _clip_events(events):
    """Keep the newest events under the result budget."""
    kept = events[-RESULT_EVENT_LIMIT:]
    truncated = len(events) > RESULT_EVENT_LIMIT
    while kept:
        size = len(json.dumps(kept, ensure_ascii=True))
        if size <= RESULT_EVENT_CHARS:
            break
        kept = kept[1:]
        truncated = True
    return kept, truncated
