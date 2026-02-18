import os
import json
import requests
from typing import Any, Dict

class LLMClient:
    def __init__(self):
        # Set these as environment variables (safe, no hardcoding)
        self.base_url = os.environ.get("LLM_BASE_URL", "").rstrip("/")
        self.api_key = os.environ.get("LLM_API_KEY", "")
        self.model = os.environ.get("LLM_MODEL", "gpt-4o-mini")  # any model name supported by your endpoint

        if not self.base_url:
            raise RuntimeError("LLM_BASE_URL is not set. Example: http://localhost:1234/v1 or https://.../v1")

    def propose_intent(self, prompt: str) -> Dict[str, Any]:
        url = f"{self.base_url}/chat/completions"
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"

        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": "You output JSON only."},
                {"role": "user", "content": prompt}
            ],
            "temperature": 0,
        }

        resp = requests.post(url, headers=headers, json=payload, timeout=30)
        resp.raise_for_status()
        data = resp.json()

        text = data["choices"][0]["message"]["content"].strip()

        # Strict: must parse as JSON object
        try:
            obj = json.loads(text)
            if not isinstance(obj, dict):
                return {"intent": "noop", "reason": "LLM returned non-object JSON"}
            return obj
        except Exception:
            return {"intent": "noop", "reason": "LLM did not return valid JSON"}
