from __future__ import annotations

# =============================================================================
# Shared utilities for pipeline controller stages
#
# These functions were previously duplicated across translator.py, planner.py,
# and summarizer.py. Centralizing them eliminates the "kept in sync manually"
# maintenance burden and ensures all stages use identical behaviour.
#
# Contents:
#   _env_int         — read an integer env var with a default
#   _env_float       — read a float env var with a default
#   _get_node_count  — read the active node count from runtime/node_count
#   _strip_code_fences — remove LLM markdown fences from JSON output
#   _repair_json     — best-effort heuristic repair of LLM JSON mistakes
# =============================================================================

import os
import re
from pathlib import Path


def _env_int(name: str, default: int) -> int:
    """Read an integer env var; return default if absent, blank, or non-numeric."""
    v = os.getenv(name)
    if v is None or v.strip() == "":
        return default
    try:
        return int(v)
    except (ValueError, TypeError):
        return default


def _env_float(name: str, default: float) -> float:
    """Read a float env var; return default if absent, blank, or non-numeric."""
    v = os.getenv(name)
    if v is None or v.strip() == "":
        return default
    try:
        return float(v)
    except (ValueError, TypeError):
        return default


def _get_node_count() -> int:
    """
    Read the number of active Lightning Network nodes from runtime/node_count.

    This file is written by the network setup/launcher scripts when nodes start.
    It tells the agent which node numbers are valid targets for tool calls (e.g.
    node=1 through node=N). Reading it on every call means changes to the node
    count (e.g. adding a third node) take effect without restarting the pipeline.

    Path resolution: walks up from ai/controllers/shared.py → ai/ → ln-ai-network/
    → runtime/node_count (the repo root's runtime directory).

    Fallback: if the file is absent (e.g. first run before setup) or unreadable
    (corrupt, wrong format), we fall back to 2 (the standard regtest pair). A
    structured warning is printed so operators know the system is running on a
    default rather than a configured value — without this warning, tool calls to
    a non-existent node (e.g. node=3 when only 2 are running) would fail silently
    at the MCP boundary with an opaque "node not found" error instead of a clear
    "node_count was never set" root cause.
    """
    _DEFAULT = 2
    try:
        p = Path(__file__).resolve().parent.parent.parent / "runtime" / "node_count"
        return int(p.read_text().strip())
    except Exception as _e:
        # Emit a structured JSON warning to stdout (same format as pipeline logs)
        # so it appears in the process supervisor's log alongside other events.
        # We import json/time here (not at module level) to avoid a circular
        # import risk — shared.py is imported very early in the module graph.
        import json as _json
        import time as _time
        print(_json.dumps({
            "ts": int(_time.time()),
            "kind": "node_count_fallback",
            "default": _DEFAULT,
            "reason": str(_e),
            "msg": (
                f"Could not read runtime/node_count ({_e}); "
                f"defaulting to {_DEFAULT} nodes. "
                "Run the network setup script to write this file."
            ),
        }), flush=True)
        return _DEFAULT


def _strip_code_fences(text: str) -> str:
    """
    Remove markdown code fences if the LLM wrapped its JSON output in them.

    LLMs frequently emit:
      ```json
      { ... }
      ```
    even when explicitly instructed not to. This strips the leading ```[json]
    and trailing ``` so the JSON parser sees clean input.
    """
    text = text.strip()
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    return text.strip()


def _repair_json(text: str) -> str:
    """
    Best-effort heuristic repair of common LLM JSON formatting mistakes.

    Transformations applied in order:
      1. Strip // single-line comments (not valid JSON)
      2. Strip /* multi-line */ comments (not valid JSON)
      3. Remove trailing commas before } or ] (Python allows, JSON doesn't)
      4. Replace single-quoted strings with double-quoted (Python style → JSON)
      5. Add missing commas between adjacent string values on separate lines
      6. Add missing commas between adjacent object fields
      7. Truncate any trailing garbage after the final closing brace

    This is intentionally permissive — parsing something slightly malformed is
    better than failing the whole request and burning another LLM call.
    """
    # Strip single-line comments (// ...)
    text = re.sub(r'//[^\n]*', '', text)
    # Strip multi-line comments (/* ... */)
    text = re.sub(r'/\*.*?\*/', '', text, flags=re.DOTALL)
    # Remove trailing commas before } or ]
    text = re.sub(r',\s*([}\]])', r'\1', text)
    # Replace single quotes with double quotes (simple cases only)
    text = re.sub(r"(?<![\"\\])'([^']*)'", r'"\1"', text)
    # Fix missing commas between adjacent string values (array elements or object values).
    # Uses \s+ (not \s*\n) so it catches both newline-separated and same-line cases,
    # e.g. "foo" "bar" or "foo"\n"bar" → "foo",\n"bar"
    text = re.sub(r'"\s+"', '",\n"', text)
    # Fix missing commas between object fields: value whitespace "key": → value,\n"key":
    # The first group covers all value-ending tokens:
    #   [\}\]"\d]  — closing brace/bracket, closing string quote, digit
    #   null|true|false — JSON literal values (their last char isn't in the class above)
    # Uses \s+ so it catches both } "key": (same line) and }\n "key": (newline) cases.
    text = re.sub(r'(null|true|false|[\}\]"\d])\s+(\s*"[^"]+"\s*:)', r'\1,\n\2', text)
    # Balance braces and strip trailing garbage.
    #
    # The LLM sometimes:
    #   (a) omits the outer closing } — the regex }\[^}]*$ approach would
    #       then find the INNER context object's } and truncate there, leaving
    #       the JSON incomplete.
    #   (b) appends explanation text after the JSON — needs to be stripped.
    #
    # Walk the string tracking brace/string depth so we know exactly where
    # the top-level JSON value ends, and handle both cases correctly.
    in_str = False
    escaped = False
    depth = 0
    end_pos = -1
    for i, ch in enumerate(text):
        if escaped:
            escaped = False
            continue
        if ch == "\\" and in_str:
            escaped = True
            continue
        if ch == '"':
            in_str = not in_str
            continue
        if in_str:
            continue
        if ch in ("{", "["):
            depth += 1
        elif ch in ("}", "]"):
            depth -= 1
            if depth == 0:
                end_pos = i
                break

    if end_pos >= 0:
        # Found the balanced end of the JSON — strip anything after it.
        text = text[: end_pos + 1]
    elif depth > 0:
        # The outer object/array was never closed — append missing braces.
        text = text.rstrip() + "\n" + "}" * depth

    return text
