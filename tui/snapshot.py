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
import queue
import threading
import time
from urllib.error import HTTPError, URLError
from urllib.request import urlopen

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8766
STATE_PATH = "/api/state"
# Config, runs and plugins are not in /api/state: the dashboard fetches them
# on tab entry and re-polls runs while that tab is active. The poller runs
# those requests off the UI thread instead of polling every render tick.
CONFIG_PATH = "/api/config"
RUNS_PATH = "/api/runs"
PLUGINS_PATH = "/api/plugins"
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
        return self._get_json(STATE_PATH)

    def fetch_config(self):
        """Editor schema + current values, the same payload the Config tab uses."""
        return self._get_json(CONFIG_PATH)

    def fetch_runs(self):
        """Run index + metric fields, the same payload the Runs tab uses."""
        return self._get_json(RUNS_PATH)

    def fetch_plugins(self):
        """Admission verdicts, the same payload the Plugins tab uses."""
        return self._get_json(PLUGINS_PATH)

    def _get_json(self, path):
        url = f"http://{self.host}:{self.port}{path}"
        try:
            with urlopen(url, timeout=self.timeout) as resp:
                body = resp.read()
        except HTTPError as exc:
            raise SnapshotError(f"{url} returned HTTP {exc.code}") from exc
        except (URLError, OSError, ValueError) as exc:
            raise SnapshotError(f"cannot reach {url}: {exc}") from exc
        try:
            data = json.loads(body.decode("utf-8", "replace"))
        except ValueError as exc:
            raise SnapshotError(f"{url} did not return JSON: {exc}") from exc
        if not isinstance(data, dict):
            raise SnapshotError(f"{url} returned {type(data).__name__}, not an object")
        return data


class SnapshotPoller:
    """Polls a client on an interval and keeps the last good snapshot.

    The fetch runs on a daemon thread so a hung or slow dashboard never
    stalls the UI's keystroke handling, and the last good snapshot
    outlives an outage: `snapshot()` keeps serving stale data and
    `status()` explains why it is stale. Tab-extra and manual-refresh
    requests are queued onto the same worker and published through
    `extra_state()` for the UI to pick up on a later tick.
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
        self._refresh_requested = False
        self._jobs = queue.Queue()
        self._extra_pending = set()
        self._extras = {}
        self._extra_errors = {}
        self._extra_version = 0

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

    def request_refresh(self):
        """Ask the worker for a snapshot fetch without blocking the caller."""
        with self._lock:
            self._refresh_requested = True

    def request_extra(self, tab, loader):
        """Queue one tab-endpoint fetch; repeat calls collapse while pending.

        Collapsing keeps a held key or a slow endpoint from stacking up HTTP
        requests behind the one the UI has already asked for.
        """
        with self._lock:
            if tab in self._extra_pending:
                return False
            self._extra_pending.add(tab)
        self._jobs.put((tab, loader))
        return True

    def _drain_extras(self):
        while True:
            try:
                tab, loader = self._jobs.get_nowait()
            except queue.Empty:
                return
            payload = None
            error = ""
            try:
                payload = getattr(self.client, loader)()
            except SnapshotError as exc:
                error = str(exc)
            except Exception as exc:
                # A bad loader must not kill the worker that owns every fetch.
                error = f"{type(exc).__name__}: {exc}"
            with self._lock:
                self._extra_pending.discard(tab)
                if payload is None:
                    self._extra_errors[tab] = error
                else:
                    self._extras[tab] = payload
                    self._extra_errors[tab] = ""
                self._extra_version += 1

    def extra_state(self):
        """Return (version, payloads, errors) for completed tab fetches.

        A tab with a failed first fetch gets an error payload so a panel can
        show it; a tab that already had a good payload keeps that payload and
        reports the error separately instead of blanking the table.
        """
        with self._lock:
            payloads = dict(self._extras)
            for tab, error in self._extra_errors.items():
                if error and tab not in payloads:
                    payloads[tab] = {"error": error}
            return self._extra_version, payloads, dict(self._extra_errors)

    def _run(self):
        while not self._stop.is_set():
            with self._lock:
                forced = self._refresh_requested
                self._refresh_requested = False
            if forced or not self._paused:
                self.refresh()
            self._drain_extras()
            if self._stop.wait(self.interval):
                break

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
