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
    Read the active node count from runtime/node_count.

    Written by the launcher when nodes start. Falls back to 2 (standard regtest
    pair) if the file is absent or unreadable. Read on every call so changes to
    the node count take effect without restarting the pipeline.

    Path resolution: walks up from this file (ai/controllers/shared.py) to the
    repo root (ln-ai-network/) and then into runtime/node_count.
    """
    try:
        p = Path(__file__).resolve().parent.parent.parent / "runtime" / "node_count"
        return int(p.read_text().strip())
    except (FileNotFoundError, ValueError):
        return 2


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
    # Fix missing commas between array elements: "foo"\n"bar" → "foo",\n"bar"
    text = re.sub(r'"\s*\n\s*"', '",\n"', text)
    # Fix missing commas between object fields: value\n  "key": → value,\n  "key":
    text = re.sub(r'([\}\]"\d])\s*\n(\s*"[^"]+"\s*:)', r'\1,\n\2', text)
    # Strip any text after the final closing brace (e.g. trailing explanation)
    m = re.search(r'\}[^}]*$', text)
    if m:
        text = text[:m.end() - len(m.group()) + 1]
    return text
