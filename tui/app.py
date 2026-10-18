"""rich application shell for the terminal UI (#319).

rich 15 dropped its widget/`App` classes, so the shell is a plain loop
over `rich.live.Live` with a small blocking key reader: Live owns the
repaint and the alt screen, the poller thread owns HTTP, and the main
loop only drains keystrokes and repaints when the snapshot version
changed. That keeps a keystroke never waiting on the dashboard.

Frame composition itself lives in `tui.panels` as a pure function, and
the console/key source are injectable, so the whole shell is testable
without a terminal.

Launch with `python -m tui`; `--once` renders a single frame and exits
(the headless/ssh-proof path).
"""

import argparse
import contextlib
import os
import queue
import sys
import threading
import time

from rich.console import Console
from rich.live import Live

from .panels import frame
from .snapshot import (
    DEFAULT_HOST,
    DEFAULT_INTERVAL,
    DEFAULT_PORT,
    DEFAULT_TIMEOUT,
    SnapshotClient,
    SnapshotPoller,
)
from .tabs import DEFAULT_TAB, TAB_IDS, next_tab, prev_tab

REFRESH_FPS = 20.0
ESC = "\x1b"
# Escape sequences the terminal sends for the keys we bind. Anything else
# (mouse reports, unhandled arrows) is ignored rather than guessed at.
CSI_KEYS = {"[C": "right", "[D": "left", "[Z": "shift+tab"}
SEQ_TIMEOUT = 0.05  # seconds to wait for the rest of an escape sequence

try:
    import select
    import termios
    import tty
except ImportError:  # pragma: no cover - non-POSIX
    HAS_TERMIOS = False
else:
    HAS_TERMIOS = True


class KeyReader:
    """Blocking single-char stdin reader handing key names to a queue.

    Reads the fd unbuffered: a TextIOWrapper can swallow the rest of an
    escape sequence into its own buffer, which leaves `select()` claiming
    the terminal has nothing more to give and the arrow key lost.

    Puts cbreak mode on for the duration so arrows arrive as escape
    sequences instead of waiting on Enter, and reports EOF as "quit" so a
    redirected stdin exits instead of spinning.
    """

    def __init__(self, stream=None):
        self.stream = stream if stream is not None else sys.stdin
        self.fd = self._fileno(self.stream)
        self.queue = queue.Queue()
        self._stop = False
        self._thread = None

    @staticmethod
    def _fileno(stream):
        try:
            return stream.fileno()
        except (AttributeError, OSError, ValueError):
            return None

    def start(self):
        if self._thread is not None:
            return
        self._thread = threading.Thread(target=self._run, name="tui-keys", daemon=True)
        self._thread.start()

    def stop(self):
        self._stop = True

    def poll(self):
        """Next key name, or None when nothing is pending."""
        try:
            return self.queue.get_nowait()
        except queue.Empty:
            return None

    def _run(self):
        with _cbreak(self.stream):
            while not self._stop:
                char = self._read(1)
                if not char:
                    self.queue.put("quit")
                    return
                self.queue.put(self._key_name(char))

    def _key_name(self, char):
        if char != ESC:
            return char
        return CSI_KEYS.get(self._read_sequence(), "esc")

    def _read(self, size):
        if self.fd is None:
            return self.stream.read(size)
        try:
            return os.read(self.fd, size).decode("utf-8", "replace")
        except OSError:
            return ""

    def _read_sequence(self):
        if self.fd is None or not HAS_TERMIOS:
            return self._read(2)
        try:
            ready, _, _ = select.select([self.fd], [], [], SEQ_TIMEOUT)
        except (OSError, ValueError):
            return ""
        return self._read(2) if ready else ""


@contextlib.contextmanager
def _cbreak(stream):
    """No echo, no line buffering, signals still live (so ^C still quits)."""
    usable = HAS_TERMIOS and hasattr(stream, "isatty") and stream.isatty()
    if not usable:
        yield
        return
    fd = stream.fileno()
    saved = tty.setcbreak(fd)
    try:
        yield
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, saved)


class TuiApp:
    """Tabbed live view of the dashboard snapshot."""

    def __init__(self, poller, tab=DEFAULT_TAB, console=None, keys=None):
        self.poller = poller
        self.tab = tab
        self.console = console if console is not None else Console()
        self.keys = keys if keys is not None else KeyReader()
        self._state = None

    # --- lifecycle -----------------------------------------------------
    def start(self):
        self.keys.start()
        self.poller.start()

    def stop(self):
        self.keys.stop()
        self.poller.stop()

    def run(self):
        """Own the terminal until the user quits."""
        self.start()
        try:
            with Live(
                self.frame(),
                console=self.console,
                screen=True,
                auto_refresh=False,
                refresh_per_second=REFRESH_FPS,
            ) as live:
                self._tick(live)
                while True:
                    key = self.keys.poll()
                    if key is not None and not self.handle(key):
                        break
                    self._tick(live)
                    time.sleep(1.0 / REFRESH_FPS)
        finally:
            self.stop()

    # --- state ---------------------------------------------------------
    def frame(self):
        snapshot = self.poller.snapshot()
        age = None if snapshot is None else self.poller.age()
        return frame(self.tab, snapshot, self.poller.status(), age, self.console.height)

    def handle(self, key):
        """Apply a key. Returns False when the app should quit."""
        if key in ("q", "quit", "ctrl+c"):
            return False
        if key in ("right", "tab"):
            self.tab = next_tab(self.tab)
        elif key in ("left", "shift+tab"):
            self.tab = prev_tab(self.tab)
        elif key == "r":
            self.poller.refresh()
        elif key == "p":
            self.poller.set_paused(not self.poller.paused())
        return True

    def _tick(self, live):
        """Repaint only when something on screen actually changed."""
        state = (self.poller.version(), self.tab)
        if state != self._state:
            self._state = state
            live.update(self.frame(), refresh=True)


def build_parser():
    parser = argparse.ArgumentParser(
        prog="python -m tui",
        description="Terminal UI for server and GM ops (reads /api/state).",
    )
    parser.add_argument("--host", default=DEFAULT_HOST, help="dashboard HTTP host")
    parser.add_argument(
        "--port", type=int, default=DEFAULT_PORT, help="dashboard HTTP port"
    )
    parser.add_argument(
        "--timeout", type=float, default=DEFAULT_TIMEOUT, help="fetch timeout (s)"
    )
    parser.add_argument(
        "--interval",
        type=float,
        default=DEFAULT_INTERVAL,
        help="seconds between /api/state polls",
    )
    parser.add_argument(
        "--tab", default=DEFAULT_TAB, choices=TAB_IDS, help="tab to open on"
    )
    parser.add_argument(
        "--once", action="store_true", help="render one frame to stdout and exit"
    )
    parser.add_argument(
        "--width", type=int, help="force output width (default: terminal)"
    )
    return parser


def render_once(poller, tab=DEFAULT_TAB, width=None, console=None):
    """Print a single frame without taking over the terminal.

    Returns the process exit code: 0 when a snapshot arrived, 1 when the
    dashboard could not be reached.
    """
    out = console if console is not None else Console(width=width, no_color=True)
    ok = poller.refresh()
    out.print(frame(tab, poller.snapshot(), poller.status(), poller.age()))
    if not ok:
        print(f"tui: {poller.status()}", file=sys.stderr)
    return 0 if ok else 1


def main(argv=None):
    args = build_parser().parse_args(argv)
    poller = SnapshotPoller(
        SnapshotClient(args.host, args.port, args.timeout), interval=args.interval
    )
    if args.once:
        return render_once(poller, args.tab, args.width)
    if not sys.stdin.isatty():
        # Live paints nothing when stdout is not a terminal, so a piped run
        # would sit there blank; say what to do instead.
        print(
            "tui: the interactive view needs a terminal; "
            "use --once for a single frame on stdout",
            file=sys.stderr,
        )
        return 2
    try:
        TuiApp(poller, args.tab, Console(width=args.width)).run()
    except KeyboardInterrupt:
        return 130
    return 0
