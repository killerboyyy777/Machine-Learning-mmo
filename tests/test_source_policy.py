"""Source policy (#749): ASCII source and a frozen buy-side call surface.

Run from the repo root:  python tests/test_source_policy.py

Both rules are negative guards over tracked files. The buy-side rule counts
matches instead of pinning line numbers, so an unrelated edit above a call
cannot move the allowlist.
"""

import ast
import io
import os
import re
import subprocess
import sys
import tokenize

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

# Signature comments are attribution, never explanation.
SIGNATURE_RE = re.compile(
    r"^\s*(?:#|//)\s*(?:overlord|killerboy(?:yy777)?|agent[-_ ]?name|claude|codex|opencode)\s*:",
    re.IGNORECASE,
)

# A standalone comment that parses as one of these statements is commented-out
# code. Prose does not parse, and bare expressions are left alone to avoid
# flagging ordinary notes.
CODE_STMTS = (
    ast.Assign,
    ast.AnnAssign,
    ast.AugAssign,
    ast.Import,
    ast.ImportFrom,
    ast.FunctionDef,
    ast.AsyncFunctionDef,
    ast.ClassDef,
    ast.Return,
    ast.For,
    ast.AsyncFor,
    ast.While,
    ast.If,
    ast.With,
    ast.AsyncWith,
    ast.Try,
    ast.Raise,
    ast.Delete,
    ast.Global,
    ast.Nonlocal,
    ast.Assert,
    ast.Pass,
    ast.Break,
    ast.Continue,
)


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


def comment_tokens(path):
    if path.endswith(".py"):
        source = read_text(path)
        for token in tokenize.generate_tokens(io.StringIO(source).readline):
            if token.type == tokenize.COMMENT:
                prefix = source.splitlines()[token.start[0] - 1][: token.start[1]]
                yield token.string, prefix.strip() == ""
    else:
        for lineno, line in enumerate(read_text(path).splitlines(), 1):
            match = re.search(r"#|//|<!--|/\*", line)
            if match:
                yield line[match.start() :].split("-->", 1)[0].split("*/", 1)[0], line[
                    : match.start()
                ].strip() == ""


def check_comments(failures):
    for path in code_files():
        for text, standalone in comment_tokens(path):
            if SIGNATURE_RE.match(text):
                failures.append(f"signature comment: {path}: {text.strip()}")
            if standalone and path.endswith(".py"):
                body = text.lstrip("#").strip()
                try:
                    tree = ast.parse(body)
                except SyntaxError:
                    continue
                if any(isinstance(node, CODE_STMTS) for node in tree.body):
                    failures.append(f"commented-out code: {path}: {body}")


def main():
    failures = []
    check_ascii(failures)
    check_buy_side(failures)
    check_comments(failures)
    if failures:
        for failure in failures:
            print("FAIL " + failure)
        sys.exit(1)
    print("SOURCE_POLICY_OK")


if __name__ == "__main__":
    main()
