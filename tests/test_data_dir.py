"""Mutable state directory tests (#840).

Run from the repo root:  python tests/test_data_dir.py

Each probe imports server in a fresh interpreter so import-time path
resolution is exercised with the real environment, not a reloaded module.
"""

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

ROOT = Path(__file__).resolve().parent.parent

MUTABLE = (
    "SCORES_FILE",
    "COMPANIONS_FILE",
    "GUILDS_FILE",
    "INTEL_FILE",
    "FREIGHT_FILE",
    "ARENA_FILE",
    "MARKET_FILE",
    "COMMISSIONS_FILE",
)
BUNDLED = ("WORLD_FILE", "CONFIG_FILE", "_ML_CONFIG_FILE")

PROBE = r"""
import json
import os
import server as srv

mutable = json.loads(os.environ["PROBE_MUTABLE"])
bundled = json.loads(os.environ["PROBE_BUNDLED"])
os.makedirs(srv.DATA_DIR, exist_ok=True)
for save in (srv.save_scores, srv.save_companions, srv.save_guilds,
             srv.save_intel, srv.save_freight, srv.save_arena,
             srv.save_market, srv.save_commissions):
    assert save(), save.__name__
print("PROBE " + json.dumps({
    "data_dir": srv.DATA_DIR,
    "package_dir": os.path.dirname(os.path.abspath(srv.__file__)),
    "mutable": {name: getattr(srv, name) for name in mutable},
    "bundled": {name: getattr(srv, name) for name in bundled},
    "backups": [srv.SCORES_FILE + ".1", srv.SCORES_FILE + ".2",
                srv.GUILDS_FILE + ".1", srv.INTEL_FILE + ".1",
                srv.FREIGHT_FILE + ".1", srv.ARENA_FILE + ".1",
                srv.MARKET_FILE + ".1", srv.COMMISSIONS_FILE + ".1"],
}))
"""


def _probe(cwd, data_dir=None):
    env = dict(os.environ)
    env["PYTHONPATH"] = str(ROOT) + os.pathsep + env.get("PYTHONPATH", "")
    env["PROBE_MUTABLE"] = json.dumps(MUTABLE)
    env["PROBE_BUNDLED"] = json.dumps(BUNDLED)
    if data_dir is None:
        env.pop("TEXTMMO_DATA_DIR", None)
    else:
        env["TEXTMMO_DATA_DIR"] = str(data_dir)
    proc = subprocess.run(
        [sys.executable, "-c", PROBE],
        cwd=str(cwd),
        env=env,
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    lines = [line for line in proc.stdout.splitlines() if line.startswith("PROBE ")]
    assert len(lines) == 1, proc.stdout
    return json.loads(lines[0][len("PROBE ") :])


def _assert_layout(info, data_dir):
    assert info["data_dir"] == os.path.abspath(data_dir)
    for name, path in info["mutable"].items():
        assert os.path.dirname(path) == info["data_dir"], (name, path)
        assert os.path.isfile(path), (name, path)
        assert path != os.path.join(info["package_dir"], os.path.basename(path)), name
    for path in info["backups"]:
        assert os.path.dirname(path) == info["data_dir"], path
        assert path != os.path.join(info["package_dir"], os.path.basename(path)), path
    for name, path in info["bundled"].items():
        assert Path(path).is_relative_to(info["package_dir"]), (name, path)
        assert os.path.isfile(path), (name, path)
        assert not Path(path).is_relative_to(info["data_dir"]), name


def test_default_dir_is_cwd():
    with tempfile.TemporaryDirectory() as tmp:
        info = _probe(tmp)
        _assert_layout(info, tmp)
        assert info["package_dir"] != info["data_dir"]
    print("DEFAULT_DIR_OK")


def test_override_moves_state_not_inputs():
    with tempfile.TemporaryDirectory() as cwd, tempfile.TemporaryDirectory() as data:
        info = _probe(cwd, data_dir=data)
        _assert_layout(info, data)
        assert info["data_dir"] != info["package_dir"]
        for name, path in info["mutable"].items():
            assert not os.path.exists(os.path.join(cwd, os.path.basename(path))), name
    print("OVERRIDE_DIR_OK")


def test_console_launcher_uses_caller_cwd():
    sys.path.insert(0, str(ROOT))
    import start

    recorded = {}

    class FakeProc:
        returncode = 0

        def poll(self):
            return 0

        def wait(self, timeout=None):
            return 0

        def terminate(self):
            pass

        def kill(self):
            pass

    def fake_popen(argv, **kwargs):
        recorded["cwd"] = kwargs.get("cwd")
        return FakeProc()

    old_popen = start.subprocess.Popen
    old_check = start.check_ports
    old_wait = start.wait_healthy
    start.subprocess.Popen = fake_popen
    start.check_ports = lambda *args: True
    start.wait_healthy = lambda *args: True
    try:
        with tempfile.TemporaryDirectory() as tmp:
            old_cwd = os.getcwd()
            os.chdir(tmp)
            try:
                args = SimpleNamespace(
                    port=18765, http_port=18766, gm_port=18767, roles=None, bots=None
                )
                assert start.cmd_serve(args) == 0
                assert recorded["cwd"] == os.path.abspath(tmp)
            finally:
                os.chdir(old_cwd)
    finally:
        start.subprocess.Popen = old_popen
        start.check_ports = old_check
        start.wait_healthy = old_wait
    print("LAUNCHER_CWD_OK")


if __name__ == "__main__":
    test_default_dir_is_cwd()
    test_override_moves_state_not_inputs()
    test_console_launcher_uses_caller_cwd()
    print("DATA_DIR_OK")
