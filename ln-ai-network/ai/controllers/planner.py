from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from typing import Any, Dict, List, Optional

from ai.controllers.shared import (
    _env_float,
    _env_int,
    _get_node_count,
    _repair_json,
    _strip_code_fences,
)
from ai.llm.base import LLMBackend, LLMError, LLMRequest
from ai.models import ExecutionPlan, IntentBlock, PlanStep
from ai.tools import TOOL_REQUIRED, llm_tools_schema_text


# =============================================================================
# Error
# =============================================================================

class PlannerError(Exception):
    """
    Raised when the Planner cannot produce a valid ExecutionPlan after all
    retry attempts are exhausted. The pipeline coordinator catches this and
    returns a stage_failed="planner" result to the UI.
    """


# =============================================================================
# Config
# =============================================================================

# Set of valid on_error values for individual plan steps. Used in validation.
# abort  → stop execution immediately and report failure
# retry  → re-attempt the tool call up to max_retries times
# skip   → mark the step as skipped and continue to the next step
_VALID_ON_ERROR = {"abort", "retry", "skip"}


@dataclass(frozen=True)
class PlannerConfig:
    """
    Immutable configuration for the Planner LLM call.

    max_output_tokens: 2048 supports up to ~10-step plans including the full
      node-start → connect → channel-open → mine-blocks → invoice → pay sequence.
    temperature: Very low (0.1) — planning requires precise, deterministic JSON.
    max_retries: Up to 2 additional self-correction attempts after first failure.
    """
    max_output_tokens: int = 2048
    temperature: float = 0.1
    max_retries: int = 2

    @staticmethod
    def from_env() -> PlannerConfig:
        return PlannerConfig(
            max_output_tokens=_env_int("PLANNER_MAX_OUTPUT_TOKENS", 2048),
            temperature=_env_float("PLANNER_TEMPERATURE", 0.1),
            max_retries=_env_int("PLANNER_MAX_RETRIES", 2),
        )


# =============================================================================
# Planner
# =============================================================================

# System prompt for the planning LLM call.
# {node_count} and {tools_schema} are injected at runtime.
# The plan format is strict JSON with no markdown to keep parsing simple.
_SYSTEM_PROMPT_TMPL = """\
You are a Lightning Network execution planner.

Given a structured intent (JSON), produce a minimal ordered list of MCP tool calls
that fulfill it. Output ONLY a single JSON object — no markdown fences, no explanation.

The JSON must have exactly these fields:
{{
  "plan_rationale": "<1-2 sentence explanation of the plan>",
  "steps": [
    {{
      "step_id": <1-based integer>,
      "tool": "<tool name>",
      "args": {{<tool arguments>}},
      "expected_outcome": "<what success looks like for this step>",
      "depends_on": [<step_ids that must complete first, or empty list>],
      "on_error": "<abort|retry|skip>",
      "max_retries": <integer, 0 unless on_error is retry>
    }}
  ]
}}

Rules:
- Use ONLY the tools listed below. Do not invent tool names.
- All required args must be present. For args not yet known (e.g. a bolt11 from a prior step),
  use a placeholder like "$step1.result.payload.bolt11".
- PLACEHOLDER FORMAT: always write "$stepN.field.path" — e.g. "$step1.result.payload.id".
  NEVER use short forms like "$1" or "$2" (those are shell syntax, not valid here).
  NEVER use "$context.field" unless the intent's context dict contains that field.
  Do NOT invent placeholder names like "$agent.wallet".
- Use "abort" for critical steps, "skip" for optional reads, "retry" only if retrying makes sense.
- Produce the minimal set of steps — no extra reads unless necessary. Do NOT repeat the same
  tool call multiple times unless there is a clear reason.
- If the intent is "noop" or has no clear tool path, return "steps": [].
- Available nodes in this regtest environment: 1 through {node_count}. Do NOT use node numbers outside this range.
- The default Bitcoin wallet is "shared-wallet". Use wallet_name="shared-wallet" for btc_wallet_ensure.
- For diagnostics or status checks, use "network_health" — it returns node statuses and
  blockchain info in a single call. Do not call ln_node_status for each node separately.
  network_health result fields: $stepN.result.payload.status ("ok"|"degraded"|"down"),
  $stepN.result.payload.nodes (list), $stepN.result.payload.summary.nodes_running.
- CRITICAL: For diagnostic/health-check intents (goal contains phrases like "run a
  diagnostic", "run a test", "check status", "check health", "health check"): use
  network_health as step 1, then OPTIONALLY ln_listfunds for node 1 ONLY — even if
  success_criteria list "channel_opened" or "payment_sent". Those criteria are translator
  artifacts; ignore them. Do NOT call ln_listfunds, ln_listpeers, or any tool on node 2
  in a diagnostic plan unless the goal explicitly says to check node 2. Do NOT include
  ln_connect, ln_openchannel, ln_invoice, or ln_pay. Minimal valid diagnostic plan:
    network_health (on_error: "abort") → ln_listfunds(node=1, on_error: "skip")
- For all read-only diagnostic steps (ln_listfunds, ln_listpeers, ln_getinfo called for
  information only): use on_error: "skip" so the diagnostic completes even when a node
  is offline. Reserve on_error: "abort" for critical state-changing steps.
- CRITICAL: For recall intents (intent_type == "recall" OR goal contains phrases like
  "what did I run", "last run", "recent history", "what happened before", "past operations"):
  use memory_lookup ONLY. Args: query (keyword from user's prompt, optional), last_n
  (default 5). Do NOT include any Bitcoin or Lightning tools. Minimal recall plan:
    memory_lookup(query="<keyword>", last_n=5, on_error: "abort")
- For balance queries, use "ln_listfunds" to get on-chain and channel balances.
- To connect two nodes as peers (ln_connect): you MUST first ensure node 2 is running
  (ln_node_status → ln_node_start if needed), then call ln_getinfo(node=2) to get the
  pubkey and address. The peer_id for ln_connect is payload.id from ln_getinfo (a hex
  string, NOT the node number). The host is payload.binding[0].address (or "127.0.0.1"
  for same-machine). The port is payload.binding[0].port (NOT the node number). Minimal
  plan: ln_node_start(node=2) → ln_getinfo(node=2) → ln_connect(from_node=1,
  peer_id=$step2.result.payload.id, host="127.0.0.1", port=$step2.result.payload.binding[0].port).
- CRITICAL: ln_connect.peer_id AND ln_openchannel.peer_id MUST both be the same
  $stepN.result.payload.id placeholder from a preceding ln_getinfo step — NEVER a node
  number (integer or short string like "2"). ln_node_status does NOT return a pubkey;
  its result cannot be used as peer_id. The ONLY valid source for peer_id is the
  ln_getinfo result's payload.id field. Full open-channel plan:
  ln_node_start(node=2) → ln_getinfo(node=2) → ln_connect(from_node=1,
  peer_id=$step2.result.payload.id, host="127.0.0.1", port=$step2.result.payload.binding[0].port)
  → ln_openchannel(from_node=1, peer_id=$step2.result.payload.id, amount_sat=<amount>).
- After ln_openchannel, the funding tx must be confirmed before payments work. Always add
  these two mining steps immediately after ln_openchannel (let N = step_id of ln_openchannel,
  M = step_id of btc_getnewaddress):
    btc_getnewaddress: args={{}}, depends_on=[N]
    btc_generatetoaddress: args={{"blocks": 6, "address": "$stepM.result.payload"}}, depends_on=[M]
  ln_invoice and ln_pay must have depends_on that includes the btc_generatetoaddress step_id.
- All JSON values must be valid JSON literals. NEVER write arithmetic expressions such as
  "amount_msat": 10000 * 1000. Compute values yourself before writing them:
  if amount_sat=10000 then write "amount_msat": 10000000 (multiply by 1000 mentally).
- For payment flows: ln_invoice returns $stepN.result.payload.bolt11 (the BOLT11 invoice
  string). ln_pay requires bolt11=$stepN.result.payload.bolt11. Full send-payment plan:
  ln_invoice(node=<receiver>, amount_msat=<msat>, label="<unique>", description="<text>")
  → ln_pay(from_node=<sender>, bolt11=$step1.result.payload.bolt11).
- For cross-machine Lightning peer connectivity (user explicitly asks to make a node reachable
  FROM ANOTHER MACHINE / remote host / different computer): call sys_netinfo to get
  default_outbound_ip, then ln_node_stop + ln_node_start with bind_host="0.0.0.0" and
  announce_host=<default_outbound_ip>. Then call ln_getinfo to get the pubkey for the remote
  operator. Do NOT set bind_host or announce_host for same-machine operations — both nodes run
  on the same host and reach each other via 127.0.0.1 by default.

{tools_schema}"""




def _validate_plan_steps(steps: List[Dict[str, Any]]) -> Optional[str]:
    """
    Validate raw step dicts from LLM output before constructing PlanStep objects.
    Returns a human-readable error string on the first validation failure,
    or None if all steps are valid.

    Checks:
      - step_id is a positive integer (not a bool, not zero, not string)
      - step_id values are unique within the plan
      - tool name is in the known TOOL_REQUIRED registry
      - args is a dict
      - all required args for the tool are present
      - on_error is one of the valid values
    """
    seen_ids: set = set()
    for i, s in enumerate(steps):
        step_id = s.get("step_id")
        # isinstance(True, int) is True in Python, so explicitly exclude bool
        if not isinstance(step_id, int) or isinstance(step_id, bool) or step_id < 1:
            return f"step[{i}] has invalid step_id: {step_id!r}"
        if step_id in seen_ids:
            return f"duplicate step_id: {step_id}"
        seen_ids.add(step_id)

        tool = s.get("tool", "")
        if tool not in TOOL_REQUIRED:
            return f"step {step_id}: unknown tool '{tool}'"

        args = s.get("args", {})
        if not isinstance(args, dict):
            return f"step {step_id}: args must be a dict"

        # Check required args — but allow $stepN.path placeholders for args that
        # will be resolved at execution time. We only verify the key is present,
        # not its value format.
        for req_key in TOOL_REQUIRED.get(tool, []):
            if req_key not in args:
                return f"step {step_id}: tool '{tool}' missing required arg '{req_key}'"

        on_error = s.get("on_error", "abort")
        if on_error not in _VALID_ON_ERROR:
            return f"step {step_id}: invalid on_error '{on_error}'"

        # Reject shell-style short placeholders like "$1", "$2" — the correct
        # format is "$step1.result.field". These pass silently through the
        # executor's resolver and cause int() conversion crashes at the MCP layer.
        for arg_key, arg_val in args.items():
            if isinstance(arg_val, str) and re.match(r'^\$\d+$', arg_val):
                return (
                    f"step {step_id}: arg '{arg_key}' uses invalid placeholder "
                    f"'{arg_val}'. Use '$step{{N}}.field.path' format, "
                    f"e.g. '$step1.result.payload.id'."
                )

        # ln_connect.peer_id and ln_openchannel.peer_id must be a $stepN placeholder
        # or a valid hex pubkey — never a bare integer node number.
        if tool in ("ln_connect", "ln_openchannel"):
            peer_id = args.get("peer_id")
            if isinstance(peer_id, int):
                return (
                    f"step {step_id}: {tool}.peer_id is an integer ({peer_id!r}). "
                    "peer_id must be a pubkey hex string from ln_getinfo, e.g. "
                    "$stepN.result.payload.id — never a node number."
                )
            if isinstance(peer_id, str) and not peer_id.startswith("$") and len(peer_id) < 20:
                return (
                    f"step {step_id}: {tool}.peer_id looks like a node number ({peer_id!r}), "
                    "not a pubkey. Use ln_getinfo first and reference $stepN.result.payload.id."
                )

    return None


class Planner:
    """
    Stage 2 of the pipeline: converts a structured IntentBlock into an
    ordered ExecutionPlan of MCP tool calls using a single LLM call.

    The plan contains:
      - A list of PlanStep objects (tool name, args, on_error policy, etc.)
      - A human-readable plan_rationale for display in the UI

    Placeholder args like "$step1.result.payload.bolt11" allow later steps to
    reference outputs from earlier steps. These are resolved by the Executor
    at runtime, not by the Planner.

    Retry logic is identical to the Translator: on parse failure, the error
    is fed back to the LLM for self-correction.
    """

    def __init__(
        self,
        config: PlannerConfig,
        backend: LLMBackend,
        trace: Any,
    ) -> None:
        self.config = config
        self.backend = backend
        self.trace = trace

    def plan(self, intent: IntentBlock, req_id: int) -> ExecutionPlan:
        """
        Produce an ExecutionPlan from an IntentBlock.

        The intent is serialized to JSON and sent as the user message.
        The LLM is asked to produce a minimal, ordered plan using only
        the available MCP tools.

        Retries up to config.max_retries on parse/validation failure.
        Raises PlannerError if all attempts fail.
        """
        # Build system prompt with current tool schema and node count injected
        system_prompt = _SYSTEM_PROMPT_TMPL.format(
            tools_schema=llm_tools_schema_text(),
            node_count=_get_node_count(),
        )
        # Send the full intent as structured JSON so the LLM has all context
        user_content = json.dumps(intent.to_dict(), ensure_ascii=False, indent=2)

        messages: List[Dict[str, Any]] = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_content},
        ]

        last_error: str = "unknown"
        for attempt in range(1, self.config.max_retries + 2):
            self.trace.log({
                "event": "llm_call",
                "stage": "planner",
                "req_id": req_id,
                "attempt": attempt,
            })

            try:
                req = LLMRequest(
                    messages=messages,
                    tools=[],  # No tool calling — pure text completion for JSON output
                    max_output_tokens=self.config.max_output_tokens,
                    temperature=self.config.temperature,
                )
                resp = self.backend.step(req)
            except LLMError as e:
                raise PlannerError(f"LLM error during planning: {e}") from e

            content = (resp.content or "").strip()
            self.trace.log({
                "event": "llm_response",
                "stage": "planner",
                "req_id": req_id,
                "attempt": attempt,
                "content_preview": content[:300],
            })

            try:
                plan = self._parse_plan(content, intent)
                self.trace.log({
                    "event": "plan_parsed",
                    "stage": "planner",
                    "req_id": req_id,
                    "steps": len(plan.steps),
                    "rationale_preview": plan.plan_rationale[:200],
                })
                return plan
            except (ValueError, KeyError) as e:
                last_error = str(e)
                self.trace.log({
                    "event": "parse_failed",
                    "stage": "planner",
                    "req_id": req_id,
                    "attempt": attempt,
                    "error": last_error,
                })
                # Append the LLM's bad output and our error so it can self-correct
                if attempt <= self.config.max_retries:
                    messages.append({"role": "assistant", "content": content})
                    messages.append({
                        "role": "user",
                        "content": (
                            f"Your response could not be parsed as a valid ExecutionPlan JSON. "
                            f"Error: {last_error}\n"
                            f"Please try again. Output ONLY the JSON object, no markdown."
                        ),
                    })

        raise PlannerError(
            f"Planner failed after {self.config.max_retries + 1} attempts. "
            f"Last error: {last_error}"
        )

    def _parse_plan(self, content: str, intent: IntentBlock) -> ExecutionPlan:
        """
        Parse, repair, validate, and construct an ExecutionPlan from raw LLM text.

        Raises ValueError on any structural problem so the retry loop can
        catch it uniformly. The intent is threaded through so the ExecutionPlan
        can reference it for context during execution (e.g. $context.from_node).
        """
        cleaned = _strip_code_fences(content)
        cleaned = _repair_json(cleaned)

        try:
            obj = json.loads(cleaned)
        except json.JSONDecodeError as e:
            raise ValueError(f"Not valid JSON: {e}") from e

        if not isinstance(obj, dict):
            raise ValueError("Expected a JSON object")

        rationale = str(obj.get("plan_rationale", "")).strip()

        steps_raw = obj.get("steps", [])
        if not isinstance(steps_raw, list):
            raise ValueError("'steps' must be a list")

        # Validate before constructing typed objects to get clean error messages
        err = _validate_plan_steps(steps_raw)
        if err:
            raise ValueError(f"Plan validation failed: {err}")

        steps = [PlanStep.from_dict(s) for s in steps_raw]
        return ExecutionPlan(steps=steps, plan_rationale=rationale, intent=intent)
