"""Stat-based checkpoint change detection for the conductor (#75).

Stdlib only. os.stat metadata (mtime_ns, size) decides whether a file moved;
hashlib.sha256 is computed only for those files, in bounded chunks. No plugin
or torch import happens at module load.
"""

import hashlib
import os
import time

_CHUNK = 1 << 20
_SUFFIXES = (".pt", ".json")


def _hash_file(path):
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(_CHUNK), b""):
            digest.update(chunk)
    return digest.hexdigest()


class CheckpointWatcher:
    """Report checkpoint files added or changed since the last scan.

    The first poll records a baseline and reports nothing. Later polls are
    gated by ``interval`` seconds measured with time.monotonic().
    """

    def __init__(self, directory, interval=5.0):
        self.directory = str(directory)
        self.interval = float(interval)
        self._baseline = None
        self._last_scan = None

    def _snapshot(self):
        found = {}
        for root, dirs, names in os.walk(self.directory):
            dirs[:] = [d for d in dirs if not d.startswith(".")]
            for name in names:
                if name.startswith(".") or name.endswith(".tmp"):
                    continue
                if not name.endswith(_SUFFIXES):
                    continue
                path = os.path.join(root, name)
                try:
                    st = os.stat(path)
                    if not os.path.isfile(path):
                        continue
                except OSError:
                    continue
                found[path] = (st.st_mtime_ns, st.st_size)
        return found

    def poll(self):
        now = time.monotonic()
        if self._baseline is not None and now - self._last_scan < self.interval:
            return []
        current = self._snapshot()
        previous = self._baseline
        self._baseline = current
        self._last_scan = now
        if previous is None:
            return []
        return [
            (path, _hash_file(path))
            for path, meta in current.items()
            if previous.get(path) != meta
        ]
