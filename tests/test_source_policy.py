"""Source policy (#749): ASCII source and a frozen buy-side call surface.

Run from the repo root:  python tests/test_source_policy.py

Both rules are negative guards over tracked files. The buy-side rule counts
matches instead of pinning line numbers, so an unrelated edit above a call
cannot move the allowlist.
"""

import os
import re
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

CODE_EXTENSIONS = {"py", "js", "cjs", "html", "css", "yml", "yaml", "sh", "toml"}

# These files pre-date the rule and are documented exceptions.
ASCII_ALLOWLIST = {
    ".github/labeler.yml",
    "dashboard.html",
    "server.py",
    "tests/dash2_smoke.cjs",
}

BUY_TOKENS = (
    "cmd_market_buy_order",
    "cmd_market_buy_modify",
    "cmd_market_buy",
    "market_buy_order",
    "market_buy_modify",
    "market_buy",
)
# Longest first so the alternation prefers the full symbol over a prefix.
BUY_RE = re.compile(
    r"\b(?:" + "|".join(sorted(BUY_TOKENS, key=len, reverse=True)) + r")\b"
)

# The freeze is on agent code; the server and its tests own the real commands.
AGENT_CODE_PREFIXES = ("ml/", "torch_agents/")
BUY_ALLOWLIST = {
    "ml/ml_env.py": 16,
    "ml/plugins/scripted.py": 3,
    "torch_agents/dqn_agent.py": 1,
    "torch_agents/torch_farm.py": 1,
}

VENDORED_SUFFIX = "uplot.min.js"


def tracked_files():
    raw = subprocess.check_output(["git", "ls-files", "-z"], cwd=ROOT)
    return [p.decode("utf-8").replace("\\", "/") for p in raw.split(b"\0") if p]


def code_files():
    for path in tracked_files():
        if path.endswith(VENDORED_SUFFIX):
            continue
        if os.path.splitext(path)[1].lstrip(".") in CODE_EXTENSIONS:
            yield path


def read_text(path):
    with open(os.path.join(ROOT, path), encoding="utf-8") as handle:
        return handle.read()


def check_ascii(failures):
    for path in code_files():
        if path in ASCII_ALLOWLIST:
            continue
        if any(ord(c) > 127 for c in read_text(path)):
            failures.append("non-ASCII source: " + path)


def check_buy_side(failures):
    for path in code_files():
        if not path.startswith(AGENT_CODE_PREFIXES):
            continue
        count = len(BUY_RE.findall(read_text(path)))
        allowed = BUY_ALLOWLIST.get(path, 0)
        if count > allowed:
            failures.append(f"buy-side tokens {count} > {allowed} in {path}")


def main():
    failures = []
    check_ascii(failures)
    check_buy_side(failures)
    if failures:
        for failure in failures:
            print("FAIL " + failure)
        sys.exit(1)
    print("SOURCE_POLICY_OK")


if __name__ == "__main__":
    main()
