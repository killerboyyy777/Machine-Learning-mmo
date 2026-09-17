"""Eval-stats + protocol-version unit tests (no live server needed).

Run from the repo root:  python tests/test_eval_stats.py
Covers #69 (exact paired t-test, Cohen's d, CI, guards, JSON output)
and #72 (version constants match, handshake status helper).
"""

import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "ml"))
sys.path.insert(0, os.path.join(ROOT, "torch_agents"))
sys.path.insert(0, ROOT)

from dqn_agent import TorchDQNAgent  # noqa: F401  (import shape check only)
from eval import _beta_reg, _t_crit, compare, report
from ml_env import PROTOCOL_VERSION as ENV_PROTO
from ml_env import TextMMOEnv

import server as srv

# --- #72: client and server speak the same version ---
assert ENV_PROTO == srv.PROTOCOL_VERSION == 1
print("PROTO_CONSTANTS_OK")

env = TextMMOEnv("VerTest")
vi = env._version_info()
assert vi == {"protocol_version": 1, "server_version": None, "version_match": True}, vi
env._state["server_version"] = 1
assert env._version_info()["version_match"] is True
env._state["server_version"] = 2
assert env._version_info()["version_match"] is False
print("VERSION_INFO_OK")

# --- #69: math pinned to textbook/reference values ---
assert abs(_beta_reg(2, 3, 0.5) - 0.6875) < 1e-9
assert abs(_t_crit(0.05, 9) - 2.262) < 1e-3
assert abs(_t_crit(0.05, 29) - 2.045) < 1e-3
assert abs(_t_crit(0.01, 9) - 3.250) < 1e-3
print("T_TABLE_OK")

# --- #69: clear gap is significant with a sane CI ---
c = compare(
    [27, 25, 29, 26, 28, 30, 24, 27, 26, 29], [22, 21, 23, 20, 22, 24, 19, 21, 20, 23]
)
assert c["verdict"] == "SIGNIFICANT" and c["p_value"] < 1e-6, c
assert abs(c["mean_diff"] - 5.6) < 1e-9
lo, hi = c["ci95"]
assert lo < 5.6 < hi and lo > 0, c["ci95"]
assert c["cohen_d"] > 2.0
json.dumps(c)  # serializable for --out run records
print("GAP_SIGNIFICANT_OK")

# --- #69: identical scores are inconclusive, not significant ---
c = compare([3, 1, 4, 1, 5, 9], [3, 1, 4, 1, 5, 9])
assert c["verdict"] == "INCONCLUSIVE" and c["p_value"] == 1.0, c
print("IDENTICAL_OK")

# --- #69: guards (small-n, length mismatch) ---
c = compare([1, 2, 3], [1, 1, 1])
assert c["verdict"] == "INCONCLUSIVE" and "small" in c["reason"], c
c = compare([1, 2, 3, 4, 5, 6], [1, 2, 3])
assert c["verdict"] == "INCONCLUSIVE" and "mismatch" in c["reason"], c
print("GUARDS_OK")

# --- #247: report uses sample std, matching compare() ---
import statistics as _statistics
_mean, _std = report("probe", [3, 1, 4, 1, 5, 9, 2, 6, 5, 3])
assert _std == _statistics.stdev([3, 1, 4, 1, 5, 9, 2, 6, 5, 3]), _std
assert _std > _statistics.pstdev([3, 1, 4, 1, 5, 9, 2, 6, 5, 3])
print("REPORT_STDEV_OK")

print("ALL_EVAL_STATS_OK")
