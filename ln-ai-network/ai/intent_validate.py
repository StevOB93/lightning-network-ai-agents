from __future__ import annotations

# =============================================================================
# intent_validate — safety gate for parsed intent objects
#
# Called by the Translator immediately after parsing an IntentBlock to scan
# the intent's field values for dangerous content before they flow downstream
# into the Planner, Executor, or any tool call.
#
# The validator receives a flattened dict of the form:
#   {"intent_type": intent_type_string, **intent.context}
#
# For example:
#   {"intent_type": "open_channel", "from_node": 1, "to_node": 2, "amount_sat": 500000}
#
# It only checks string values — integer/float context values like node numbers
# and amounts cannot contain injection payloads.
#
# What it guards against:
#   Shell metacharacters  (; && || | ` $()   — command injection
#   Path traversal        (../              — filesystem escape
#   Home directory        (~               — home-dir expansion in shells
#   Privilege escalation  (sudo, chmod,    — shell privilege ops
#                          chown, rm
#   Remote access         (ssh, scp        — exfiltration via remote execution
#   HTTP URLs             (http:// https:// — prompt-injection via external fetch
#
# This is a defense-in-depth layer, not the only line of defense. The LLM's
# system prompt also prohibits these patterns, and MCP tool arg validation
# provides a final layer before any real network operation occurs.
# =============================================================================

import re
from typing import Any, Dict, Tuple


# Tokens that, if found in any string field of the intent, indicate a likely
# injection attempt. All checks are case-insensitive (values are lowercased).
FORBIDDEN = [
    # Shell metacharacters and command chaining
    ";", "&&", "||", "|", "`", "$(",
    # Filesystem traversal
    "../",
    # Dangerous shell expansions / privilege ops
    "~/", "sudo", "rm ", "chmod", "chown",
    # Remote execution / data exfiltration
    "ssh", "scp",
    # HTTP(S) URLs — could be used to leak data via prompt injection
    "http://", "https://",
]


def validate_intent_safety(intent: Dict[str, Any]) -> Tuple[bool, str]:
    """
    Check an intent dict for forbidden content in any string field value.

    Parameters:
      intent — a dict with at minimum an "intent_type" key (the intent type string)
               plus any context fields extracted from the user prompt.
               This is NOT the raw IntentBlock — it's the flattened dict
               created by the Translator as:
                 {"intent_type": intent.intent_type, **intent.context}

    Returns:
      (True, "ok")                           — passed
      (False, "<reason>")                    — failed; reason describes the field

    Raises nothing — all errors are surfaced through the return value.
    """
    if not isinstance(intent, dict):
        return False, "Intent must be a JSON object"

    # The "intent_type" key is always required — it's the intent type string and
    # its presence confirms the dict was constructed correctly.
    if "intent_type" not in intent:
        return False, "Missing 'intent_type' field"

    # Scan all string values for forbidden tokens.
    # Normalization step: collapse all whitespace (tabs, newlines, carriage returns,
    # multiple spaces) to a single space before checking. Without this, an attacker
    # could bypass a check like "rm " by embedding "rm\t" or "rm\n" — the raw token
    # wouldn't match the space-suffixed forbidden string. Lowercasing handles case
    # variants ("HTTP://" → "http://"). The two transforms together cover the most
    # common bypass techniques without needing regex for each individual token.
    for key, value in intent.items():
        if isinstance(value, str):
            normalized = re.sub(r"\s+", " ", value.lower())
            for token in FORBIDDEN:
                if token in normalized:
                    return False, (
                        f"Forbidden content in field '{key}': matched '{token}'"
                    )

    return True, "ok"
