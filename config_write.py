"""Surgical config-file writer for the dashboard editor (#63).

json.dump round-trips are unusable here. `ml/versioning.py` hashes the raw
bytes of server_config.json and ml/ml_config.json into every checkpoint's
config_hash, and `version_notes` reports "config differs" when it moves. A
rewrite that reorders keys, drops the blank lines between sections, or spells
0.10 as 0.1 would flip every checkpoint in the repo to "config differs" the
first time an operator saved a config from the browser.

So this rewrites only the lines it must. Each scalar sits alone on its own
line in both files, which makes the edit a match over that one line: the
file's indentation, key order, spacing, trailing commas and `_comment` all
survive untouched, and a save that changes nothing writes no bytes at all
(so the hash does not move). Adding a key inserts a line in its section;
removing one drops the line and fixes up the neighbour's comma.

Validated by json.loads before and after: a config file that does not parse is
never written, and a parse failure after the rewrite leaves the file on disk
untouched (the write is a single os.replace of a temp file).
"""

import json
import os
import re
import tempfile

from config_schema import field_spec, format_value

# One scalar per line: "KEY": value, with the value running to the line end.
# Anchored on the full line so a key whose name is a prefix of another
# (XP_BASE vs XP_BASE2) cannot match across keys.
_ENTRY_RE = re.compile(r'^(\s*)"([A-Za-z0-9_]+)"(\s*:\s*)(.+?)(,?)\s*$')
_SECTION_RE = re.compile(r'^\s*"([A-Za-z0-9_]+)"(\s*:\s*\{)\s*$')


class _Absent:
    """Sentinel: the key is not in the file at all, so it is at its default."""

    def __repr__(self):
        return "<absent>"


_ABSENT = _Absent()


def _json_equal(a, b):
    """Compare as the loader would see them, so 2 and 2.0 are one value."""
    if isinstance(a, bool) != isinstance(b, bool):
        return False
    if isinstance(a, (int, float)) and isinstance(b, (int, float)):
        return float(a) == float(b)
    return a == b


def _read(path):
    try:
        # newline="" keeps the file's own line endings: without it, Windows
        # text mode rewrites every LF to CRLF on write and the whole file's
        # bytes change, which is the exact provenance break this module exists
        # to avoid.
        with open(path, encoding="utf-8", newline="") as handle:
            return handle.read()
    except OSError:
        return ""


def _indent_of(line):
    return line[: len(line) - len(line.lstrip())]


def _section_bounds(lines, section):
    """(header_index, first_body, closing_brace) line indices for a section."""
    for index, line in enumerate(lines):
        match = _SECTION_RE.match(line)
        if not match or match.group(1) != section:
            continue
        for close in range(index + 1, len(lines)):
            if lines[close].strip().startswith("}"):
                return index, index + 1, close
        return None
    return None


def _rendered(key, spec, value, comma):
    return f'"{key}": {format_value(spec, value)}{"," if comma else ""}'


def _apply_section(lines, file_id, section, edits):
    """Apply {key: value_or_None} inside one section. Returns (ok, note).

    Resets (None) are applied first: removing a line shifts every index after
    it, and doing the removals in one pass keeps the remaining lookups valid.
    """
    bounds = _section_bounds(lines, section)
    if bounds is None:
        return False, f"section {section} not found in file"
    header, body_start, close = bounds
    indent = _indent_of(lines[header]) + "  "

    pending = dict(edits)
    for key, value in list(pending.items()):
        if value is not None:
            continue
        for index in range(body_start, close):
            match = _ENTRY_RE.match(lines[index])
            if not match or match.group(2) != key:
                continue
            del lines[index]
            # Deleting a middle or first entry leaves the surrounding commas
            # correct. Deleting the LAST entry does not: the entry that now
            # ends the section still carries the comma that separated it from
            # the removed one. After the delete that entry sits at close - 2
            # (the closing brace has shifted into close - 1).
            if index == close - 1 and close - 2 >= body_start:
                lines[close - 2] = _strip_comma(lines[close - 2])
            close -= 1
            break
        del pending[key]

    if pending:
        updates = {}
        for index in range(body_start, close):
            match = _ENTRY_RE.match(lines[index])
            if match and match.group(2) in pending:
                updates[match.group(2)] = index
        for key, index in updates.items():
            # A submitted value that already parses to what the line says must
            # not be respelled. 0.10 and 0.1 are the same value to the loader
            # but different bytes, and a save that only retypes a value the
            # operator did not change would move the config hash and make every
            # existing checkpoint report "config differs".
            try:
                have = json.loads(_ENTRY_RE.match(lines[index]).group(4))
            except (ValueError, AttributeError):
                have = _ABSENT
            if _json_equal(have, pending[key]):
                continue
            spec = field_spec(file_id, section, key)
            comma = lines[index].rstrip().endswith(",")
            lines[index] = _indent_of(lines[index]) + _rendered(
                key, spec, pending[key], comma
            )
        added = [k for k in pending if k not in updates]
        if added:
            # New entries go last in the section, each comma-terminated except
            # the final one so the closing brace keeps a clean neighbour.
            if close - 1 > body_start:
                lines[close - 1] = _strip_comma(lines[close - 1])
            for offset, key in enumerate(added):
                last = offset == len(added) - 1
                lines.insert(
                    close + offset,
                    indent
                    + _rendered(
                        key, field_spec(file_id, section, key), pending[key], not last
                    ),
                )
    return True, ""


def _strip_comma(line):
    return line.rstrip().rstrip(",")


def write_edits(path, file_id, edits):
    """Write {section: {key: value_or_None}} into the config file at path.

    None means "delete the override" (reset to the code default). Returns
    (ok, message, changed_keys). Writes no bytes when every submitted value
    already matches what the file says.
    """
    wanted = {}
    for section, keys in edits.items():
        for key, value in keys.items():
            if field_spec(file_id, section, key) is None:
                return False, f"unknown key {section}.{key}", []
            wanted[(section, key)] = value

    original = _read(path)
    try:
        before = json.loads(original) if original.strip() else {}
    except ValueError as exc:
        return False, f"{os.path.basename(path)} is not valid JSON ({exc})", []

    lines = original.splitlines()
    changed = []
    for section, keys in edits.items():
        current = before.get(section) if isinstance(before.get(section), dict) else {}
        for key, value in keys.items():
            have = current.get(key, _ABSENT)
            if _json_equal(have, value) or (value is None and have is _ABSENT):
                continue
            changed.append(f"{section}.{key}")
        ok, note = _apply_section(lines, file_id, section, keys)
        if not ok:
            return False, note, []

    updated = "\n".join(lines)
    if original.endswith("\n"):
        updated += "\n"
    if updated == original:
        return True, "no changes", []

    try:
        parsed = json.loads(updated)
    except ValueError as exc:
        return False, f"refusing to write invalid JSON ({exc})", []
    # The rewritten text must mean what was asked for, not merely parse.
    for (section, key), value in wanted.items():
        got = (
            parsed.get(section, {}).get(key)
            if isinstance(parsed.get(section), dict)
            else None
        )
        if value is None:
            if got is not None:
                return False, f"{section}.{key} survived removal", []
        elif not _json_equal(got, value):
            return False, f"{section}.{key} did not round-trip", []

    directory = os.path.dirname(os.path.abspath(path)) or "."
    handle, tmp = tempfile.mkstemp(dir=directory, prefix=".config-", suffix=".tmp")
    try:
        with os.fdopen(handle, "w", encoding="utf-8", newline="") as out:
            out.write(updated)
        os.replace(tmp, path)
    except OSError as exc:
        try:
            os.remove(tmp)
        except OSError:
            pass
        return False, f"could not write {os.path.basename(path)} ({exc})", []
    return True, f"{len(changed)} value(s) written", changed
