from __future__ import annotations
from typing import Any, Dict, Tuple

FORBIDDEN = [
    ";", "&&", "||", "|", "`", "$(",
    "../", "~", "sudo", "rm ", "chmod",
    "chown", "ssh", "scp",
    "http://", "https://"
]


def validate_intent_safety(intent: Dict[str, Any]) -> Tuple[bool, str]:
    if not isinstance(intent, dict):
        return False, "Intent must be a JSON object"

    if "intent" not in intent:
        return False, "Missing 'intent' field"

    for key, value in intent.items():
        if isinstance(value, str):
            lowered = value.lower()
            for token in FORBIDDEN:
                if token in lowered:
                    return False, f"Forbidden content detected in field '{key}'"

    return True, "ok"
