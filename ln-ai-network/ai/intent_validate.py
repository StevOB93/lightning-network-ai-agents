from __future__ import annotations
from typing import Any, Dict, Tuple

FORBIDDEN = [";", "&&", "||", "|", "`", "$(", "../", "~", "sudo", "rm ", "chmod", "chown", "ssh", "scp", "http://", "https://"]

def validate_intent_safety(intent: Dict[str, Any]) -> Tuple[bool, str]:
    if not isinstance(intent, dict):
        return False, "Intent must be a JSON object"

    for k, v in intent.items():
        if isinstance(v, str):
            low = v.lower()
            if any(tok in low for tok in FORBIDDEN):
                return False, f"Forbidden content detected in field '{k}'"

    if "intent" not in intent:
        return False, "Missing 'intent' field"

    return True, "ok"
