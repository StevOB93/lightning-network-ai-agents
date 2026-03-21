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
    """Read an integer env var; return default if absent or blank."""
    v = os.getenv(name)
    return default if v is None or v.strip() == "" else int(v)


def _env_float(name: str, default: float) -> float:
    """Read a float env var; return default if absent or blank."""
    v = os.getenv(name)
    return default if v is None or v.strip() == "" else float(v)


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
    # Strip any text after the final closing brace (e.g. trailing explanation)
    m = re.search(r'\}[^}]*$', text)
    if m:
        text = text[:m.end() - len(m.group()) + 1]
    return text
