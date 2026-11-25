"""Explicit UTF-8 on JSON reads (#757): offline source inspection.

Run from the repo root:  python tests/test_json_encoding.py

Uses only the standard library. AST inspection avoids importing server.py,
which loads world data at import time and binds a game port.
"""

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
TARGETS = ("server.py", "ml/conductor/registry.py")
WORKFLOW = ROOT / ".github" / "workflows" / "tests.yml"


def _json_reads(rel):
    """(line, source text, encoding) for each open() that feeds json.load."""
    text = (ROOT / rel).read_text(encoding="utf-8")
    tree = ast.parse(text)
    lines = text.splitlines()
    opens, aliases, jsons = [], [], []
    for node in ast.walk(tree):
        if isinstance(node, ast.With):
            for item in node.items:
                call = item.context_expr
                if not (
                    isinstance(call, ast.Call) and ast.unparse(call.func) == "open"
                ):
                    continue
                if not isinstance(item.optional_vars, ast.Name):
                    continue
                mode = "r"
                if len(call.args) >= 2 and isinstance(call.args[1], ast.Constant):
                    mode = call.args[1].value
                for kw in call.keywords:
                    if kw.arg == "mode" and isinstance(kw.value, ast.Constant):
                        mode = kw.value.value
                encoding = next(
                    (
                        kw.value.value
                        for kw in call.keywords
                        if kw.arg == "encoding" and isinstance(kw.value, ast.Constant)
                    ),
                    None,
                )
                opens.append((node.lineno, item.optional_vars.id, mode, encoding))
        if isinstance(node, ast.Assign) and isinstance(node.value, ast.Call):
            value = ast.unparse(node.value)
            if value.endswith(".read()"):
                for target in node.targets:
                    if isinstance(target, ast.Name):
                        aliases.append((node.lineno, value[:-7], target.id))
        if isinstance(node, ast.Call) and ast.unparse(node.func) in (
            "json.load",
            "json.loads",
        ):
            jsons.append((node.lineno, {ast.unparse(a) for a in node.args}))
    opens.sort(key=lambda record: record[0])
    records = []
    for idx, (line, handle, mode, encoding) in enumerate(opens):
        if "r" not in mode or "b" in mode:
            continue
        next_open = min([o[0] for o in opens[idx + 1 :]] or [10**9])
        names = {handle} | {
            name
            for aline, ahandle, name in aliases
            if ahandle == handle and line <= aline < next_open
        }
        if any(line <= jline < next_open and args & names for jline, args in jsons):
            records.append((line, lines[line - 1].strip(), encoding))
    return records


def test_json_reads_are_utf8():
    found = 0
    for rel in TARGETS:
        records = _json_reads(rel)
        found += len(records)
        for lineno, line, encoding in records:
            assert encoding == "utf-8", f"{rel}:{lineno}: {line}"
    assert found >= 12, found
    print("JSON_READS_UTF8_OK")


def test_read_text_is_utf8():
    for rel in TARGETS:
        tree = ast.parse((ROOT / rel).read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if not (
                isinstance(node, ast.Call) and ast.unparse(node.func) == ("read_text")
            ):
                continue
            enc = [kw.value.value for kw in node.keywords if kw.arg == "encoding"]
            assert enc == ["utf-8"], f"{rel}:{node.lineno}"
    print("READ_TEXT_UTF8_OK")


def test_registered_after_source_policy():
    text = WORKFLOW.read_text(encoding="utf-8")
    assert "python tests/test_json_encoding.py" in text
    assert text.index("test_source_policy.py") < text.index("test_json_encoding.py")
    print("REGISTRATION_OK")


if __name__ == "__main__":
    test_json_reads_are_utf8()
    test_read_text_is_utf8()
    test_registered_after_source_policy()
    print("JSON_ENCODING_OK")
