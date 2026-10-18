"""Snapshot fetching for the terminal UI (#319).

The TUI is read-only ops tooling, so it consumes the exact endpoint the
browser dashboard polls: `GET /api/state` on the dashboard HTTP port
(8766 by default). That keeps the TUI off the WebSocket game protocol
entirely -- stdlib `urllib` only, no client library, no auth surface --
and means every field it shows is already contract-tested by
`tests/test_snapshot_contract.py`.

Rich-free on purpose, so the fetch path is testable without rich.
"""

import json
import threading
import time
from urllib.error import HTTPError, URLError
from urllib.request import urlopen

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8766
STATE_PATH = "/api/state"
DEFAULT_TIMEOUT = 5.0
DEFAULT_INTERVAL = 2.0  # dashboard polls /api/state on the same cadence


class SnapshotError(Exception):
    """A snapshot could not be fetched or was not a JSON object."""


class SnapshotClient:
    """Fetches the dashboard snapshot over plain HTTP."""

    def __init__(self, host=DEFAULT_HOST, port=DEFAULT_PORT, timeout=DEFAULT_TIMEOUT):
        self.host = host
        self.port = port
        self.timeout = timeout

    @property
    def url(self):
        return f"http://{self.host}:{self.port}{STATE_PATH}"

    def fetch(self):
        """Return the snapshot dict, or raise SnapshotError.

        A stale-but-last-good snapshot is the caller's business, not the
        client's: this either produces a fresh dict or raises.
        """
        try:
            with urlopen(self.url, timeout=self.timeout) as resp:
                body = resp.read()
        except HTTPError as exc:
            raise SnapshotError(f"{self.url} returned HTTP {exc.code}") from exc
        except (URLError, OSError, ValueError) as exc:
            raise SnapshotError(f"cannot reach {self.url}: {exc}") from exc
        try:
            snapshot = json.loads(body.decode("utf-8", "replace"))
        except ValueError as exc:
            raise SnapshotError(f"{self.url} did not return JSON: {exc}") from exc
        if not isinstance(snapshot, dict):
            raise SnapshotError(
                f"{self.url} returned {type(snapshot).__name__}, not an object"
            )
        return snapshot


class SnapshotPoller:
    """Polls a client on an interval and keeps the last good snapshot.

    The fetch runs on a daemon thread so a hung or slow dashboard never
    stalls the UI's keystroke handling, and the last good snapshot
    outlives an outage: `snapshot()` keeps serving stale data and
    `status()` explains why it is stale.
    """

    def __init__(self, client=None, interval=DEFAULT_INTERVAL):
        self.client = client if client is not None else SnapshotClient()
        self.interval = interval
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread = None
        self._snapshot = None
        self._error = ""
        self._updated_at = 0.0
        self._paused = False
        self._version = 0

    # --- lifecycle -----------------------------------------------------
    def start(self):
        """Begin polling. Safe to call twice; the second call is a no-op."""
        if self._thread is not None:
            return
        self._thread = threading.Thread(
            target=self._run, name="tui-snapshot", daemon=True
        )
        self._thread.start()

    def stop(self):
        """Stop polling and wait briefly for the thread to finish."""
        self._stop.set()
        thread, self._thread = self._thread, None
        if thread is not None:
            thread.join(timeout=self.interval + 1.0)

    # --- polling -------------------------------------------------------
    def refresh(self):
        """Fetch once. Returns True on success and records any error."""
        try:
            snapshot = self.client.fetch()
        except SnapshotError as exc:
            with self._lock:
                self._error = str(exc)
                self._version += 1
            return False
        with self._lock:
            self._snapshot = snapshot
            self._error = ""
            self._updated_at = time.time()
            self._version += 1
        return True

    def _run(self):
        while not self._stop.is_set():
            if not self._paused:
                self.refresh()
            if self._stop.wait(self.interval):
                break

    # --- state ---------------------------------------------------------
    def snapshot(self):
        """Latest good snapshot, or None if nothing has ever arrived."""
        with self._lock:
            return self._snapshot

    def version(self):
        """Counter bumped on every refresh attempt (including failures)."""
        with self._lock:
            return self._version

    def age(self):
        """Seconds since the last successful fetch (0.0 before the first)."""
        with self._lock:
            return 0.0 if not self._updated_at else time.time() - self._updated_at

    def status(self):
        """Short human string for the TUI status line."""
        with self._lock:
            if self._paused:
                return "paused"
            if self._error:
                if not self._updated_at:
                    return f"no snapshot: {self._error}"
                return f"stale ({int(time.time() - self._updated_at)}s): {self._error}"
            if not self._updated_at:
                return "connecting"
        return "live"

    def set_paused(self, paused):
        """Pause or resume polling; the last snapshot stays on screen."""
        with self._lock:
            self._paused = bool(paused)
            self._version += 1

    def paused(self):
        with self._lock:
            return self._paused
