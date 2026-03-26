// Lightning Agent UI — app.js
// Single-file frontend for the Lightning Network AI pipeline dashboard.
// Communicates with the Python ui_server.py via REST and Server-Sent Events (SSE).

// ---------------------------------------------------------------------------
// DOM refs
// ---------------------------------------------------------------------------
// Short alias for getElementById — keeps element lookups terse throughout the file.
const $ = (id) => document.getElementById(id);

// Status bar elements
const promptInput    = $("prompt-input");   // Textarea for user queries
const actionLog      = $("action-log");     // Single-line status/error feedback
const agentLockVal   = $("agent-lock-val"); // Displays pipeline lock status ("pid=...")
const lastRequestId  = $("last-request-id");// ID of the most recently completed request
const msgCount       = $("msg-count");      // Number of recent inbox messages shown
const pipelineBuild  = $("pipeline-build"); // Pipeline build string from outbox
const indAgent       = $("ind-agent");      // Online/offline indicator dot
const indSSE         = $("ind-sse");        // SSE connection indicator dot
const sseStatusVal   = $("sse-status-val"); // SSE status text

// Pipeline stage display panels
const intentDisplay  = $("intent-display"); // Rendered IntentBlock
const planDisplay    = $("plan-display");   // Rendered ExecutionPlan
const execDisplay    = $("exec-display");   // Rendered step results

// Per-stage status badges
const badgeTranslator = $("badge-translator");
const badgePlanner    = $("badge-planner");
const badgeExecutor   = $("badge-executor");

// Stage card containers (for color-coding ok/fail/skip)
const stageTranslator = $("stage-translator");
const stagePlanner    = $("stage-planner");
const stageExecutor   = $("stage-executor");

// Summary card elements
const summaryCard  = $("summary-card");  // The whole summary section
const summaryBody  = $("summary-body");  // Human-readable pipeline answer text
const summaryTs    = $("summary-ts");    // Timestamp of the result
const summaryIcon  = $("summary-icon");  // ✓ or ✗ success indicator

// Logs tab elements
const traceLog         = $("trace-log");          // Live trace event list
const archiveToggleBtn = $("archive-toggle-btn"); // "Archive ▾/▴" toggle button
const archivePanel     = $("archive-panel");      // Collapsible past-queries panel
const archiveList      = $("archive-list");       // List of archive entry cards

// Queue panel elements (Logs tab)
const inboxList        = $("inbox-list");
const outboxList       = $("outbox-list");
const inboxCount   = $("inbox-count");
const outboxCount  = $("outbox-count");

// Hide items older than these timestamps (Unix seconds). Set by clear buttons.
let _inboxClearTs  = 0;
let _outboxClearTs = 0;

// Network tab elements
const networkViz   = $("network-viz");   // D3 SVG container
const networkHint  = $("network-hint");  // "N nodes, M channels" hint text

// Strategy toggle
const strategyBtn    = $("strategy-btn");    // Cycles through strategy modes

// Settings tab elements
const settingsStatus = $("settings-status"); // Inline feedback next to save button

// ---------------------------------------------------------------------------
// Strategy mode toggle
// ---------------------------------------------------------------------------
// Cycles: cheap → fast → detailed → max_effort → cheap → ...
// Persisted in localStorage so the choice survives page refresh.

const _STRATEGIES = ["cheap", "fast", "detailed", "max_effort"];
const _STRATEGY_LABELS = { cheap: "Cheap", fast: "Fast", detailed: "Detailed", max_effort: "Max Effort" };

// ---------------------------------------------------------------------------
// Named constants — polling, timeouts, layout, and truncation
// ---------------------------------------------------------------------------

// Polling intervals (ms) — fallback when SSE is not active
const POLL_STATUS_MS       = 3000;
const POLL_PIPELINE_MS     = 4000;
const POLL_TRACE_MS        = 3000;
const POLL_NETWORK_MS      = 15000;

// SSE reconnection delays (ms)
const SSE_RECONNECT_MS     = 5000;

// UI feedback timeouts (ms)
const FEEDBACK_TIMEOUT_MS  = 2000;  // Copy button, crash kit, etc.
const SETTINGS_SAVE_MS     = 3000;  // Settings saved notice
const CLEAR_STEP1_MS       = 5000;  // Clear All step 1 revert
const CLEAR_STEP2_MS       = 10000; // Clear All step 2 revert
const CLEAR_COMPLETE_MS    = 3000;  // Clear All completion notice
const COPY_SUCCESS_MS      = 1500;  // Copy ✓ feedback

// String truncation lengths
const TRUNCATE_DEFAULT     = 120;
const TRUNCATE_QUEUE       = 140;
const TRUNCATE_EXEC_ARGS   = 160;
const TRUNCATE_TRACE       = 200;
const TRUNCATE_DEDUP_KEY   = 40;

// D3 network graph layout
const GRAPH_MIN_HEIGHT     = 320;
const GRAPH_MAX_HEIGHT     = 500;
const GRAPH_PX_PER_NODE    = 60;
const GRAPH_ZOOM_MIN       = 0.3;
const GRAPH_ZOOM_MAX       = 4;
const GRAPH_LINK_DISTANCE  = 120;
const GRAPH_CHARGE_STRENGTH = -300;
const GRAPH_COLLIDE_RADIUS = 30;
const GRAPH_NODE_RADIUS    = 16;
const GRAPH_ARROW_REF_X    = 18;   // node radius (16) + stroke (2)
const GRAPH_LABEL_OFFSET_Y = -6;
const GRAPH_CAPACITY_UNIT  = 1_000_000;  // satoshis per display unit (M)

/** Return the currently selected strategy mode. */
function getStrategy() {
  return strategyBtn ? strategyBtn.dataset.strategy : "fast";
}

/** Advance the strategy toggle to the next mode. */
function cycleStrategy() {
  if (!strategyBtn) return;
  const current = strategyBtn.dataset.strategy;
  const idx = _STRATEGIES.indexOf(current);
  const next = _STRATEGIES[(idx + 1) % _STRATEGIES.length];
  strategyBtn.dataset.strategy = next;
  strategyBtn.textContent = _STRATEGY_LABELS[next];
  try { localStorage.setItem("ln_ai_strategy", next); } catch (_) { /* private mode */ }
}

// Restore saved strategy on load
(function initStrategy() {
  try {
    const saved = localStorage.getItem("ln_ai_strategy");
    if (saved && _STRATEGIES.includes(saved) && strategyBtn) {
      strategyBtn.dataset.strategy = saved;
      strategyBtn.textContent = _STRATEGY_LABELS[saved];
    }
  } catch (_) { /* private mode */ }
})();

// ---------------------------------------------------------------------------
// Utilities
// ---------------------------------------------------------------------------

/**
 * Format a Unix timestamp (seconds) as a locale-aware time string.
 * Used for trace event timestamps where the date is implied by the session.
 */
function fmtTs(ts) {
  if (!ts) return "—";
  return new Date(ts * 1000).toLocaleTimeString();
}

/**
 * Format a Unix timestamp as a full locale-aware date+time string.
 * Used for the summary card timestamp where the date matters.
 */
function fmtDateTime(ts) {
  if (!ts) return "—";
  return new Date(ts * 1000).toLocaleString();
}

/**
 * HTML-escape a value for safe injection into innerHTML strings.
 * Prevents XSS from pipeline results, tool names, or user input.
 * ?? "" handles null/undefined gracefully.
 */
function esc(str) {
  return String(str ?? "").replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
}

/**
 * Truncate a string to n characters, appending "…" if it was cut.
 * Used throughout the UI to keep long JSON payloads from overflowing.
 */
function truncate(str, n = TRUNCATE_DEFAULT) {
  const s = String(str ?? "");
  return s.length > n ? s.slice(0, n) + "…" : s;
}

/**
 * Update the action log below the prompt input.
 * isError=true adds the "error" CSS class for red styling.
 */
function setLog(text, isError = false) {
  actionLog.textContent = text;
  actionLog.className = "action-log" + (isError ? " error" : "");
}

/**
 * POST JSON to a URL and return the parsed response.
 * Throws an Error with the server's error message on non-2xx status.
 */
async function postJson(url, payload) {
  const res = await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  const data = await res.json();
  if (!res.ok) throw new Error(data.error || "Request failed");
  return data;
}

// ---------------------------------------------------------------------------
// Status bar
// ---------------------------------------------------------------------------

/**
 * Update the top status bar from a runtime snapshot object.
 *
 * agent_lock is the raw lock file content ("pid=1234 started_ts=...").
 * We split on space and show only the first token (the pid part) to keep it
 * short. An empty lock string means no process is running → "no lock".
 *
 * indAgent gets the "online" class when a lock is present, driving the
 * green/red CSS transition.
 */
function updateStatusBar(status) {
  const lock = status.agent_lock || "";
  agentLockVal.textContent = lock ? lock.split(" ")[0] : "no lock";
  indAgent.classList.toggle("online", !!lock);

  const last = status.last_outbox;
  lastRequestId.textContent = last?.request_id ?? "—";
  msgCount.textContent = status.message_count ?? 0;

  // Update build string only when present (avoids blank display on startup)
  if (last?.pipeline_build) {
    pipelineBuild.textContent = last.pipeline_build;
  }
}

// ---------------------------------------------------------------------------
// Pipeline stages
// ---------------------------------------------------------------------------

/**
 * Update a stage badge and its parent card's visual state.
 *
 * state is one of: "ok" | "fail" | "skip" | (empty/unknown)
 * The badge text and CSS class are set together so they're always in sync.
 */
function setBadge(el, stageEl, state) {
  el.textContent = state === "ok" ? "✓ done" : state === "fail" ? "✗ failed" : state === "skip" ? "⊘ skipped" : "—";
  el.className = "stage-badge " + state;
  stageEl.className = "stage-card " + state;
}

/**
 * Render a structured IntentBlock into the translator stage panel.
 *
 * Shows: intent type tag, goal string, human summary, context key-value table,
 * success criteria list, and any clarifications needed.
 *
 * Each intent_type maps to a CSS tag color class (tag-blue, tag-green, etc.)
 * for quick visual identification.
 */
function renderIntent(intent) {
  if (!intent) {
    intentDisplay.innerHTML = '<div class="empty-state">Intent not available.</div>';
    return;
  }
  const typeClass = {
    open_channel: "tag-blue", set_fee: "tag-orange", rebalance: "tag-purple",
    pay_invoice: "tag-green", noop: "tag-gray", freeform: "tag-teal",
  }[intent.intent_type] || "tag-gray";

  // Build context table rows from the extracted key-value pairs
  const ctxRows = Object.entries(intent.context || {})
    .map(([k, v]) => `<tr><td class="ctx-key">${esc(k)}</td><td class="ctx-val">${esc(JSON.stringify(v))}</td></tr>`)
    .join("");

  const criteria = (intent.success_criteria || [])
    .map(c => `<li>${esc(c)}</li>`).join("");

  // Only render the clarifications block if there are items (should be rare)
  const clarifications = (intent.clarifications_needed || []).length
    ? `<div class="clarif-block">⚠ Clarifications needed:<ul>${(intent.clarifications_needed).map(c => `<li>${esc(c)}</li>`).join("")}</ul></div>`
    : "";

  intentDisplay.innerHTML = `
    <div class="intent-type-row">
      <span class="tag ${typeClass}">${esc(intent.intent_type)}</span>
      <span class="intent-goal">${esc(intent.goal)}</span>
    </div>
    <div class="intent-summary">${esc(intent.human_summary)}</div>
    ${ctxRows ? `<table class="ctx-table"><tbody>${ctxRows}</tbody></table>` : ""}
    ${criteria ? `<ul class="criteria-list">${criteria}</ul>` : ""}
    ${clarifications}
  `;
}

/**
 * Render an ExecutionPlan into the planner stage panel.
 *
 * Shows the plan_rationale as a muted italicised intro, followed by
 * one card per step with: step number, tool name, on_error badge,
 * args JSON, and expected_outcome string.
 */
function renderPlan(plan) {
  if (!plan || !plan.steps?.length) {
    planDisplay.innerHTML = plan
      ? '<div class="empty-state">Empty plan (noop).</div>'
      : '<div class="empty-state">Plan not available.</div>';
    return;
  }

  const steps = plan.steps.map((s) => `
    <div class="plan-step">
      <div class="plan-step-head">
        <span class="step-num">${s.step_id}</span>
        <code class="step-tool">${esc(s.tool)}</code>
        <span class="step-on-error on-error-${s.on_error}">${esc(s.on_error)}</span>
      </div>
      <div class="step-args">${esc(JSON.stringify(s.args))}</div>
      <div class="step-outcome muted">${esc(s.expected_outcome)}</div>
    </div>
  `).join("");

  const rationale = plan.plan_rationale
    ? `<div class="rationale muted"><em>${esc(plan.plan_rationale)}</em></div>`
    : "";

  planDisplay.innerHTML = `${rationale}<div class="plan-steps">${steps}</div>`;
}

/**
 * Render executor step results into the executor stage panel.
 *
 * Each step shows: status icon (✓/✗/⊘), tool name, retry count badge,
 * step number, truncated args, and error message if any.
 *
 * CSS classes step-ok / step-fail / step-skipped control the row color.
 */
function renderExecution(stepResults, _stageFailed) {
  if (!stepResults?.length) {
    execDisplay.innerHTML = '<div class="empty-state">No steps executed.</div>';
    return;
  }

  const rows = stepResults.map(sr => {
    const statusClass = sr.skipped ? "step-skipped" : sr.ok ? "step-ok" : "step-fail";
    const statusIcon = sr.skipped ? "⊘" : sr.ok ? "✓" : "✗";
    // Only show retry badge when retries were actually used (keeps UI clean)
    const retries = sr.retries_used > 0 ? `<span class="retry-badge">${sr.retries_used} retry</span>` : "";
    const errRow = sr.error ? `<div class="step-error">${esc(sr.error)}</div>` : "";
    return `
      <div class="exec-step ${statusClass}">
        <div class="exec-step-head">
          <span class="exec-status-icon">${statusIcon}</span>
          <code class="step-tool">${esc(sr.tool)}</code>
          ${retries}
          <span class="step-num muted">#${sr.step_id}</span>
        </div>
        <div class="step-args muted">${esc(truncate(JSON.stringify(sr.args), TRUNCATE_EXEC_ARGS))}</div>
        ${errRow}
      </div>
    `;
  }).join("");

  execDisplay.innerHTML = `<div class="exec-steps">${rows}</div>`;
}

/**
 * Update all three pipeline stage panels from a pipeline_report entry.
 *
 * Handles partial results gracefully:
 *   - If translator failed, show error in intent panel, skip planner+executor
 *   - If planner failed, show translator result + planner error
 *   - If executor failed, show all stages with the executor error
 *   - stage_failed=null means everything succeeded
 *
 * The summary card is always updated when content is available.
 */
function renderPipelineResult(result) {
  if (!result) return;

  const stageFailed = result.stage_failed;

  // Translator stage: show intent if present, error if translator failed
  if (result.intent) {
    setBadge(badgeTranslator, stageTranslator, "ok");
    renderIntent(result.intent);
  } else if (stageFailed === "translator") {
    setBadge(badgeTranslator, stageTranslator, "fail");
    intentDisplay.innerHTML = `<div class="stage-error">${esc(result.error || "Translation failed")}</div>`;
  }

  // Planner stage: show plan if present, skip badge if intent exists but plan doesn't,
  // or error if planner failed
  if (result.plan) {
    setBadge(badgePlanner, stagePlanner, "ok");
    renderPlan(result.plan);
  } else if (stageFailed === "planner") {
    setBadge(badgePlanner, stagePlanner, "fail");
    planDisplay.innerHTML = `<div class="stage-error">${esc(result.error || "Planning failed")}</div>`;
  } else if (result.intent && !result.plan) {
    // Intent was noop or plan was empty — planner ran but produced no steps
    setBadge(badgePlanner, stagePlanner, "skip");
  }

  // Executor stage: show results if any steps ran, or skip/fail based on context
  if (result.step_results?.length) {
    const allOk = result.step_results.every(r => r.ok || r.skipped);
    setBadge(badgeExecutor, stageExecutor, stageFailed === "executor" ? "fail" : allOk ? "ok" : "fail");
    renderExecution(result.step_results, stageFailed);
  } else if (stageFailed === "executor") {
    setBadge(badgeExecutor, stageExecutor, "fail");
    execDisplay.innerHTML = `<div class="stage-error">${esc(result.error || "Execution failed")}</div>`;
  } else if (result.plan) {
    // Plan existed but no steps were executed (plan was empty after validation)
    setBadge(badgeExecutor, stageExecutor, "skip");
  }

  // Summary card: shown whenever there's any answer text available
  if (result.content || result.human_summary || result.error) {
    summaryCard.style.display = "";
    summaryBody.textContent = result.content || result.human_summary || result.error || "";
    summaryTs.textContent = fmtDateTime(result.ts);
    // success=false OR error present → show ✗; otherwise ✓
    const ok = result.success !== false && !result.error;
    summaryIcon.textContent = ok ? "✓" : "✗";
    summaryIcon.className = "summary-icon " + (ok ? "ok" : "fail");
  }
}

// ---------------------------------------------------------------------------
// Trace log
// ---------------------------------------------------------------------------

// Dedup set: tracks "ts-kind" keys of events already rendered in the live trace.
// Prevents duplicate rows when the SSE stream resends the same tail of events
// on each file change (which may include events from the previous push).
// Note: this set is never cleared — it grows for the lifetime of the page session.
// This is intentional: we never want to re-render the same event even after a
// manual clear (which only clears the DOM, not this set).
const _seenTraceTs = new Set();

/**
 * Prepend new trace events to the live trace log (newest on top).
 *
 * Deduplication: each event gets a composite key "ts-kind" where kind is the
 * first available of: ev.kind, ev.event, or a JSON prefix of the whole object.
 * This handles events that share a timestamp (fast pipeline stages).
 *
 * Auto-scroll: if the user was at the top of the log (reading the latest events),
 * keep them there after prepending. If they've scrolled down to read older events,
 * don't interrupt them by jumping back to the top.
 */
function renderTrace(events) {
  if (!events?.length) return;

  let appended = false;
  // Check scroll position BEFORE prepending
  const wasAtTop = traceLog.scrollTop <= 40;

  // Clear the "No trace events yet." placeholder on first real event
  if (traceLog.querySelector(".empty-state")) {
    traceLog.innerHTML = "";
  }

  for (const ev of events) {
    // Build a dedup key: timestamp + first available event type discriminator
    const key = `${ev.ts}-${ev.kind || ev.event || JSON.stringify(ev).slice(0, TRUNCATE_DEDUP_KEY)}`;
    if (_seenTraceTs.has(key)) continue;
    _seenTraceTs.add(key);
    appended = true;

    const row = document.createElement("div");
    row.className = "trace-row";
    const kind = ev.kind || ev.event || ev.stage || "event";
    const ts = fmtTs(ev.ts);
    // Build the detail string: strip known display fields, serialize the rest
    const detail = (() => {
      const copy = { ...ev };
      delete copy.ts; delete copy.kind; delete copy.event; delete copy.stage;
      const s = JSON.stringify(copy);
      return s === "{}" ? "" : truncate(s, TRUNCATE_TRACE);
    })();
    row.innerHTML = `<span class="trace-ts">${ts}</span><span class="trace-kind">${esc(kind)}</span>${detail ? `<span class="trace-detail">${esc(detail)}</span>` : ""}`;
    // Prepend so the newest event appears at the top
    traceLog.insertBefore(row, traceLog.firstChild);
  }

  if (appended && wasAtTop) {
    traceLog.scrollTop = 0;
  }
}

// ---------------------------------------------------------------------------
// Archive (past queries)
// ---------------------------------------------------------------------------

/**
 * Render the archive list entries into #archive-list.
 *
 * Each entry is a clickable card showing:
 *   - Request ID (#N) in monospace
 *   - Color-coded status badge (green=ok, red=failed, yellow=partial)
 *   - Formatted datetime (YYYY-MM-DD HH:MM:SS)
 *   - File size in KB
 *   - Truncated user query preview
 *
 * The filename is stored in data-filename for the click handler to use
 * when loading the full trace.
 */
function renderArchiveList(entries) {
  if (!entries?.length) {
    archiveList.innerHTML = '<div class="empty-state">No archived queries yet.</div>';
    return;
  }
  archiveList.innerHTML = entries.map(e => {
    const statusClass = e.status === "ok" ? "arch-ok" : e.status === "failed" ? "arch-fail" : "arch-partial";
    // Reformat "20260319-143022" → "2026-03-19 14:30:22" for readability
    const dtFormatted = e.datetime
      ? e.datetime.replace(/(\d{4})(\d{2})(\d{2})-(\d{2})(\d{2})(\d{2})/, "$1-$2-$3 $4:$5:$6")
      : "—";
    return `<div class="archive-entry" data-filename="${esc(e.filename)}">
      <div class="archive-entry-head">
        <span class="archive-req-id">#${e.req_id}</span>
        <span class="archive-status ${statusClass}">${esc(e.status)}</span>
        <span class="archive-dt">${esc(dtFormatted)}</span>
        <span class="archive-size">${(e.size_bytes / 1024).toFixed(1)} KB</span>
      </div>
      <div class="archive-preview">${esc(e.user_text_preview || "—")}</div>
    </div>`;
  }).join("");
}

/**
 * Fetch the archive list from /api/logs and render it.
 * Called when the archive panel is opened and when a pipeline_result SSE
 * arrives while the panel is open (auto-refresh).
 *
 * Reads the current search input value and active status filter pill, then
 * passes them as query params to the server for server-side filtering.
 */
async function fetchAndRenderArchiveList() {
  try {
    const searchEl = $("archive-search");
    const q = searchEl ? searchEl.value.trim() : "";
    const activeBtn = document.querySelector(".arch-filter-btn.active");
    const status = activeBtn ? (activeBtn.dataset.status || "") : "";
    const params = new URLSearchParams();
    if (q) params.set("q", q);
    if (status) params.set("status", status);
    const url = "/api/logs" + (params.toString() ? "?" + params.toString() : "");
    const res = await fetch(url);
    const entries = await res.json();
    renderArchiveList(entries);
  } catch (_) {
    archiveList.innerHTML = '<div class="empty-state">Failed to load archive.</div>';
  }
}

/**
 * Fetch aggregate query metrics from /api/metrics and render stat tiles.
 * Called on page load and when a pipeline_result SSE event arrives.
 */
async function fetchAndRenderMetrics() {
  const metricsContent = $("metrics-content");
  if (!metricsContent) return;
  try {
    const res = await fetch("/api/metrics");
    const m = await res.json();
    if (m.total_queries === 0) {
      metricsContent.innerHTML = '<div class="empty-state">No queries yet.</div>';
      return;
    }
    const successPct = (m.success_rate * 100).toFixed(1);
    const avgDur = m.avg_duration_s != null ? m.avg_duration_s.toFixed(2) + "s" : "—";
    const sc = m.status_counts || {};
    const st = m.avg_stage_ms || {};
    const fmtMs = v => v != null ? Math.round(v) + "ms" : "—";
    metricsContent.innerHTML = `
      <div class="metric-tile">
        <div class="metric-value">${m.total_queries}</div>
        <div class="metric-label">Total Queries</div>
      </div>
      <div class="metric-tile">
        <div class="metric-value metric-ok">${successPct}%</div>
        <div class="metric-label">Success Rate</div>
      </div>
      <div class="metric-tile">
        <div class="metric-value metric-ok">${sc.ok ?? 0}</div>
        <div class="metric-label">ok</div>
      </div>
      <div class="metric-tile">
        <div class="metric-value metric-partial">${sc.partial ?? 0}</div>
        <div class="metric-label">partial</div>
      </div>
      <div class="metric-tile">
        <div class="metric-value metric-fail">${sc.failed ?? 0}</div>
        <div class="metric-label">failed</div>
      </div>
      <div class="metric-tile">
        <div class="metric-value">${avgDur}</div>
        <div class="metric-label">Avg Duration</div>
      </div>
      <div class="metric-tile">
        <div class="metric-value">${fmtMs(st.translator)}</div>
        <div class="metric-label">Avg Translate</div>
      </div>
      <div class="metric-tile">
        <div class="metric-value">${fmtMs(st.planner)}</div>
        <div class="metric-label">Avg Plan</div>
      </div>
      <div class="metric-tile">
        <div class="metric-value">${fmtMs(st.executor)}</div>
        <div class="metric-label">Avg Execute</div>
      </div>
      <div class="metric-tile">
        <div class="metric-value">${fmtMs(st.summarizer)}</div>
        <div class="metric-label">Avg Summarize</div>
      </div>
    `;
  } catch (_) {
    if (metricsContent) metricsContent.innerHTML = '<div class="empty-state">Failed to load metrics.</div>';
  }
}

/**
 * Fetch the full event list for a single archive file.
 * Returns the events array, or null on any error (404, network, parse).
 */
async function fetchArchiveFile(filename) {
  try {
    const res = await fetch(`/api/logs/${encodeURIComponent(filename)}`);
    if (!res.ok) return null;
    const { events } = await res.json();
    return events;
  } catch (_) {
    return null;
  }
}

/**
 * Render archive trace events into a given container element.
 *
 * Similar to renderTrace() but:
 *   - Writes to an arbitrary container (not the live #trace-log)
 *   - Uses innerHTML (batch render) instead of DOM append — archive traces
 *     are static snapshots loaded once, so we don't need incremental appending
 *   - No dedup set — all events are rendered since they're loaded fresh each time
 *   - Reuses the same .trace-row / .trace-ts / .trace-kind / .trace-detail CSS
 */
function renderArchiveTrace(container, events) {
  if (!events?.length) {
    container.innerHTML = '<div class="empty-state">No events.</div>';
    return;
  }
  container.innerHTML = events.map(ev => {
    const kind = ev.kind || ev.event || ev.stage || "event";
    const ts = fmtTs(ev.ts);
    const copy = { ...ev };
    delete copy.ts; delete copy.kind; delete copy.event; delete copy.stage;
    const s = JSON.stringify(copy);
    const detail = s === "{}" ? "" : truncate(s, TRUNCATE_TRACE);
    return `<div class="trace-row"><span class="trace-ts">${ts}</span><span class="trace-kind">${esc(kind)}</span>${detail ? `<span class="trace-detail">${esc(detail)}</span>` : ""}</div>`;
  }).join("");
}

// ---------------------------------------------------------------------------
// Inbox / Outbox
// ---------------------------------------------------------------------------

/**
 * Render a queue (inbox or outbox) into a container element.
 *
 * Items are shown in reverse-chronological order (newest first) because
 * the most recent entry is almost always what the user wants to see.
 * The server returns items in chronological order so we .reverse() the slice.
 *
 * Each item shows: ID, copy button, timestamp, and a truncated body preview.
 * The copy button uses data-copy to store the full text inline so the global
 * copy handler can access it without another fetch.
 */
function renderQueue(container, countEl, items, labelPrefix, hideBeforeTs = 0) {
  const visible = (items ?? []).filter(item => !hideBeforeTs || (item.ts ?? 0) >= hideBeforeTs);
  countEl.textContent = visible.length;
  if (!visible.length) {
    container.innerHTML = '<div class="empty-state">No entries yet.</div>';
    return;
  }
  container.innerHTML = visible.slice().reverse().map(item => {
    const id = item.request_id ?? item.id ?? "?";
    // Prefer content field; fall back to meta or summary JSON for non-pipeline entries
    const body = item.content ?? JSON.stringify(item.meta ?? item.summary ?? {});
    return `
      <div class="queue-item">
        <div class="queue-item-head">
          <span>${labelPrefix} ${id}</span>
          <button class="copy-btn copy-btn-inline" data-copy="${esc(body)}" title="Copy">⎘</button>
          <span class="muted">${fmtTs(item.ts)}</span>
        </div>
        <div class="queue-item-body muted">${esc(truncate(body, TRUNCATE_QUEUE))}</div>
      </div>`;
  }).join("");
}

function clearInbox()  { _inboxClearTs  = Date.now() / 1000; renderQueue(inboxList,  inboxCount,  [], "Req",  _inboxClearTs); }
function clearOutbox() { _outboxClearTs = Date.now() / 1000; renderQueue(outboxList, outboxCount, [], "Rep", _outboxClearTs); }

// ---------------------------------------------------------------------------
// Network visualization (D3 force graph)
// ---------------------------------------------------------------------------
// Module-level refs so we can stop/destroy the previous simulation and SVG
// when new network data arrives (prevents memory leaks from orphaned simulations).
let _networkSvg = null;
let _simulation = null;

/**
 * Extract a stable unique ID from a node object.
 * Tries multiple field names in priority order to handle CLN version differences.
 */
function nodeId(n) {
  return n.nodeid || n.node_id || n.id || n.pub_key || n.pubkey || String(n);
}

/**
 * Build a human-readable label for a node.
 * Uses alias if available, otherwise truncates the pubkey to 8 chars.
 */
function nodeLabel(n) {
  const id = nodeId(n);
  const alias = n.alias || n.label || "";
  return alias ? alias : id.slice(0, 8) + "…";
}

/**
 * Render a Lightning Network topology graph using D3 force simulation.
 *
 * Data shape expected: { nodes: [{id, nodeid, alias, running}], channels: [{source, destination, capacity, active}] }
 *
 * Features:
 *   - Force-directed layout with charge, link distance, center, and collision forces
 *   - Arrow markers on edges showing payment direction
 *   - Channel capacity labels in the middle of each edge (in M sat)
 *   - Draggable nodes (pinned while dragging, released on mouseup)
 *   - Zoom and pan on the SVG canvas
 *   - Auto-creates node entries for channel endpoints not in the node list
 *
 * SVG height is adaptive: min 320px, max 500px, scales with node count.
 */
function renderNetwork(data) {
  const rawNodes = data.nodes || [];
  const rawChannels = data.channels || [];

  if (!rawNodes.length && !rawChannels.length) {
    networkViz.innerHTML = '<div class="empty-state">Run a health check or node query to populate the graph.</div>';
    networkHint.textContent = "Populated from tool call results";
    // Clean up any running simulation to avoid memory leak
    if (_simulation) { _simulation.stop(); _simulation = null; }
    if (_networkSvg) { _networkSvg.remove(); _networkSvg = null; }
    return;
  }

  // Build a Map from node ID → display object for O(1) lookup during edge building
  const nodeMap = new Map();
  rawNodes.forEach(n => {
    const id = nodeId(n);
    nodeMap.set(id, { id, label: nodeLabel(n), raw: n });
  });

  // Build edge list; auto-create stub nodes for channel endpoints not in node list
  // (can happen when channel data comes from a node that knows its peer's pubkey
  // but that peer wasn't returned by network_health)
  const links = [];
  rawChannels.forEach(ch => {
    const src = ch.source || ch.node1_pub || ch.local_alias || null;
    const dst = ch.destination || ch.node2_pub || ch.remote_alias || null;
    if (!src || !dst) return;
    if (!nodeMap.has(src)) nodeMap.set(src, { id: src, label: src.slice(0, 8) + "…", raw: {} });
    if (!nodeMap.has(dst)) nodeMap.set(dst, { id: dst, label: dst.slice(0, 8) + "…", raw: {} });
    links.push({
      source: src,
      target: dst,
      capacity: ch.capacity || ch.satoshis || 0,
      active: ch.active !== false,  // Default to active if field is missing
    });
  });

  const nodes = Array.from(nodeMap.values());

  // Destroy previous simulation before creating a new one
  if (_simulation) _simulation.stop();
  networkViz.innerHTML = "";

  const W = networkViz.clientWidth || 700;
  const H = Math.max(GRAPH_MIN_HEIGHT, Math.min(GRAPH_MAX_HEIGHT, nodes.length * GRAPH_PX_PER_NODE));

  const svg = d3.select(networkViz).append("svg")
    .attr("width", W)
    .attr("height", H)
    .attr("viewBox", `0 0 ${W} ${H}`);

  _networkSvg = svg.node();

  // Container group for zoom/pan transform — all graph elements go inside <g>
  const g = svg.append("g");
  svg.call(d3.zoom().scaleExtent([GRAPH_ZOOM_MIN, GRAPH_ZOOM_MAX]).on("zoom", e => g.attr("transform", e.transform)));

  // Arrow marker definition — placed in <defs> so it can be referenced by URL
  // refX positions the arrowhead at the node circle's edge (radius + stroke)
  svg.append("defs").append("marker")
    .attr("id", "arrow").attr("viewBox", "0 -4 8 8").attr("refX", GRAPH_ARROW_REF_X).attr("refY", 0)
    .attr("markerWidth", 6).attr("markerHeight", 6).attr("orient", "auto")
    .append("path").attr("d", "M0,-4L8,0L0,4").attr("fill", "var(--accent)");

  // Edge lines — active channels use the accent color; inactive use a muted grey
  const link = g.append("g").selectAll("line").data(links).join("line")
    .attr("stroke", d => d.active ? "var(--accent)" : "var(--line-strong)")
    .attr("stroke-width", d => d.active ? 2 : 1)
    .attr("stroke-opacity", 0.7)
    .attr("marker-end", "url(#arrow)");

  // Capacity labels centered on each edge — shown in millions of sat (e.g. "1.00M")
  const linkLabel = g.append("g").selectAll("text").data(links).join("text")
    .attr("font-size", 9).attr("fill", "var(--muted)").attr("text-anchor", "middle")
    .attr("font-family", "IBM Plex Mono, monospace")
    .text(d => d.capacity ? `${(d.capacity / GRAPH_CAPACITY_UNIT).toFixed(2)}M` : "");

  // Node groups — each contains a circle + label text + tooltip <title>
  const node = g.append("g").selectAll("g").data(nodes).join("g")
    .attr("cursor", "pointer")
    .call(d3.drag()
      // alphaTarget(0.3) re-heats the simulation during drag so the layout adjusts
      .on("start", (e, d) => { if (!e.active) _simulation.alphaTarget(0.3).restart(); d.fx = d.x; d.fy = d.y; })
      // fx/fy pins the node at the drag position (overrides force layout)
      .on("drag",  (e, d) => { d.fx = e.x; d.fy = e.y; })
      // Release pin on mouseup so the node can drift with the layout again
      .on("end",   (e, d) => { if (!e.active) _simulation.alphaTarget(0); d.fx = null; d.fy = null; }));

  node.append("circle")
    .attr("r", GRAPH_NODE_RADIUS)
    .attr("fill", "var(--accent-dim)")
    .attr("stroke", "var(--accent)")
    .attr("stroke-width", 2);

  // Show first 6 chars of the label inside the circle (enough to identify the node)
  node.append("text")
    .attr("text-anchor", "middle").attr("dy", "0.35em")
    .attr("font-size", 10).attr("font-weight", "700")
    .attr("fill", "var(--ink)").attr("font-family", "IBM Plex Mono, monospace")
    .text(d => d.label.slice(0, 6));

  // Full node ID as a native browser tooltip (shown on hover)
  node.append("title").text(d => d.id);

  // Force simulation — four forces work together to produce a readable layout:
  //   link:    pulls connected nodes toward each other (distance=120px)
  //   charge:  repels all nodes from each other (prevents overlap)
  //   center:  pulls all nodes toward the canvas center (prevents drift)
  //   collide: prevents node circles from overlapping (radius=30)
  _simulation = d3.forceSimulation(nodes)
    .force("link", d3.forceLink(links).id(d => d.id).distance(GRAPH_LINK_DISTANCE))
    .force("charge", d3.forceManyBody().strength(GRAPH_CHARGE_STRENGTH))
    .force("center", d3.forceCenter(W / 2, H / 2))
    .force("collide", d3.forceCollide(GRAPH_COLLIDE_RADIUS))
    .on("tick", () => {
      // Update all element positions on each simulation tick
      link
        .attr("x1", d => d.source.x).attr("y1", d => d.source.y)
        .attr("x2", d => d.target.x).attr("y2", d => d.target.y);
      linkLabel
        .attr("x", d => (d.source.x + d.target.x) / 2)
        .attr("y", d => (d.source.y + d.target.y) / 2 + GRAPH_LABEL_OFFSET_Y);
      node.attr("transform", d => `translate(${d.x},${d.y})`);
    });

  networkHint.textContent = `${nodes.length} node${nodes.length !== 1 ? "s" : ""}, ${links.length} channel${links.length !== 1 ? "s" : ""}`;
}

// ---------------------------------------------------------------------------
// API calls
// ---------------------------------------------------------------------------

/** Fetch runtime status and update the status bar + queue panels. */
async function fetchStatus() {
  const res = await fetch("/api/status");
  const data = await res.json();
  updateStatusBar(data);
  renderQueue(inboxList, inboxCount, data.recent_inbox, "Req", _inboxClearTs);
  renderQueue(outboxList, outboxCount, data.recent_outbox, "Rep", _outboxClearTs);
}

/** Fetch the latest pipeline result and render all stage panels. */
async function fetchPipelineResult() {
  const res = await fetch("/api/pipeline_result");
  const { result } = await res.json();
  if (result) renderPipelineResult(result);
}

/** Fetch the live trace tail and append new events to the trace log. */
async function fetchTrace() {
  const res = await fetch("/api/trace");
  const { events } = await res.json();
  renderTrace(events);
}

/** Fetch network topology and re-render the D3 force graph. */
async function fetchNetwork() {
  const res = await fetch("/api/network");
  const data = await res.json();
  renderNetwork(data);
}

/**
 * Fetch all data sources in parallel and update all panels.
 * Promise.allSettled ensures a failure in one fetch doesn't block the others.
 */
async function refreshAll() {
  await Promise.allSettled([fetchStatus(), fetchPipelineResult(), fetchTrace(), fetchNetwork()]);
}

/** Enqueue the current prompt text and refresh all panels on completion. */
async function queueAsk() {
  const text = promptInput.value.trim();
  if (!text) { setLog("Enter a prompt first.", true); return; }
  const btn = $("ask-btn");
  btn.disabled = true;
  setLog("Queuing request…");
  try {
    const data = await postJson("/api/ask", { text, strategy: getStrategy() });
    setLog(`Queued request #${data.msg.id} [${_STRATEGY_LABELS[getStrategy()]}]. Waiting for agent…`);
    await refreshAll();
  } finally {
    btn.disabled = false;
  }
}

/** Enqueue a health check and refresh all panels on completion. */
async function queueHealth() {
  const btn = $("health-btn");
  btn.disabled = true;
  setLog("Queuing health check…");
  try {
    const data = await postJson("/api/health", {});
    setLog(`Queued health check #${data.msg.id}. Waiting for agent…`);
    await refreshAll();
  } finally {
    btn.disabled = false;
  }
}

// ---------------------------------------------------------------------------
// Crash kit
// ---------------------------------------------------------------------------

/**
 * Format a crash kit JSON payload into a readable plain-text debug report.
 *
 * Sections:
 *   SYSTEM    — platform, Python version, node count
 *   CONFIG    — non-sensitive env var values
 *   RUNTIME   — agent lock status, queue message count
 *   LAST PIPELINE RESULT — request ID, stage failed, error, summary
 *   METRICS   — aggregate query stats
 *   TRACE     — last 100 events in ISO timestamp + kind + detail format
 */
function formatCrashKit(data) {
  const ts = new Date((data.generated_at || Date.now() / 1000) * 1000).toISOString();
  const lines = [`=== LIGHTNING AI CRASH KIT ===`, `Generated: ${ts}`, ``];

  // System
  const sys = data.system || {};
  lines.push(`--- SYSTEM ---`);
  lines.push(`Platform:   ${sys.platform || "unknown"}`);
  lines.push(`Python:     ${sys.python || "unknown"}`);
  lines.push(`Node count: ${sys.node_count ?? "unknown"}`);
  lines.push(``);

  // Config (non-sensitive)
  const cfg = data.config || {};
  lines.push(`--- CONFIG ---`);
  for (const [k, v] of Object.entries(cfg)) {
    lines.push(`  ${k}=${v || "(unset)"}`);
  }
  lines.push(``);

  // Runtime
  const rt = data.runtime || {};
  lines.push(`--- RUNTIME ---`);
  lines.push(`Agent lock:    ${rt.agent_lock || "none (not running)"}`);
  lines.push(`Message count: ${rt.message_count ?? 0}`);
  lines.push(``);

  // Last pipeline result
  const pr = data.pipeline_result;
  lines.push(`--- LAST PIPELINE RESULT ---`);
  if (pr) {
    lines.push(`Request ID:  #${pr.request_id ?? "—"}`);
    lines.push(`Stage failed: ${pr.stage_failed || "none (success)"}`);
    if (pr.error)   lines.push(`Error:        ${pr.error}`);
    const summary = pr.content || pr.human_summary;
    if (summary)    lines.push(`Summary:      ${summary.slice(0, 300)}${summary.length > 300 ? "…" : ""}`);
  } else {
    lines.push(`(no pipeline results yet)`);
  }
  lines.push(``);

  // Metrics
  const m = data.metrics || {};
  if (m.total_queries > 0) {
    lines.push(`--- METRICS ---`);
    const sc = m.status_counts || {};
    lines.push(`Total:    ${m.total_queries} | ok: ${sc.ok ?? 0} | partial: ${sc.partial ?? 0} | failed: ${sc.failed ?? 0}`);
    lines.push(`Success:  ${((m.success_rate || 0) * 100).toFixed(1)}%`);
    if (m.avg_duration_s != null) lines.push(`Avg dur:  ${m.avg_duration_s.toFixed(2)}s`);
    lines.push(``);
  }

  // Trace
  const trace = data.trace || [];
  lines.push(`--- RECENT TRACE (${trace.length} events) ---`);
  for (const ev of trace) {
    const evTs = ev.ts ? new Date(ev.ts * 1000).toISOString() : "?";
    const kind = ev.kind || ev.event || ev.stage || "event";
    const copy = { ...ev };
    delete copy.ts; delete copy.kind; delete copy.event; delete copy.stage;
    const detail = JSON.stringify(copy);
    lines.push(`${evTs}  ${kind}${detail !== "{}" ? "  " + detail : ""}`);
  }

  return lines.join("\n");
}

/**
 * Fetch /api/crash_kit, format the result, show a preview, and copy to clipboard.
 */
async function copyCrashKit() {
  const btn   = $("crash-kit-btn");
  const preview = $("crash-kit-preview");
  btn.textContent = "Generating…";
  btn.disabled = true;
  try {
    const res  = await fetch("/api/crash_kit");
    const data = await res.json();
    const text = formatCrashKit(data);
    preview.textContent = text;
    await navigator.clipboard.writeText(text);
    btn.textContent = "✓ Copied!";
    setTimeout(() => { btn.textContent = "⎘ Copy Crash Kit"; btn.disabled = false; }, FEEDBACK_TIMEOUT_MS);
  } catch (e) {
    preview.textContent = "Error generating crash kit: " + e.message;
    btn.textContent = "⎘ Copy Crash Kit";
    btn.disabled = false;
  }
}

// ---------------------------------------------------------------------------
// Settings — LLM model registry
// ---------------------------------------------------------------------------

/**
 * Maps each LLM backend to its env key and available models.
 * The first model in each list is the default for that backend.
 * "custom" is always appended at render time to allow free-text input.
 */
const _LLM_MODELS = {
  openai: {
    envKey: "OPENAI_MODEL",
    models: [
      { value: "gpt-4o",        label: "GPT-4o" },
      { value: "gpt-4o-mini",   label: "GPT-4o Mini" },
      { value: "gpt-4.1",       label: "GPT-4.1" },
      { value: "gpt-4.1-mini",  label: "GPT-4.1 Mini" },
      { value: "gpt-4.1-nano",  label: "GPT-4.1 Nano" },
      { value: "o3-mini",       label: "o3-mini" },
    ],
  },
  ollama: {
    envKey: "OLLAMA_MODEL",
    models: [
      { value: "llama3.2:3b",     label: "Llama 3.2 3B" },
      { value: "llama3.1:8b",     label: "Llama 3.1 8B" },
      { value: "llama3.1:70b",    label: "Llama 3.1 70B" },
      { value: "qwen2.5:3b",      label: "Qwen 2.5 3B" },
      { value: "qwen2.5:7b",      label: "Qwen 2.5 7B" },
      { value: "qwen2.5:14b",     label: "Qwen 2.5 14B" },
      { value: "mistral:7b",      label: "Mistral 7B" },
      { value: "gemma3:9b",       label: "Gemma 3 9B" },
      { value: "phi4-mini:3.8b",  label: "Phi-4 Mini 3.8B" },
      { value: "deepseek-r1:14b", label: "DeepSeek-R1 14B" },
    ],
  },
  gemini: {
    envKey: "GEMINI_MODEL",
    models: [
      { value: "gemini-2.5-flash",  label: "Gemini 2.5 Flash" },
      { value: "gemini-2.5-pro",    label: "Gemini 2.5 Pro" },
      { value: "gemini-2.0-flash",  label: "Gemini 2.0 Flash" },
    ],
  },
  anthropic: {
    envKey: "CLAUDE_MODEL",
    models: [
      { value: "claude-opus-4-6",   label: "Claude Opus 4.6" },
      { value: "claude-sonnet-4-6", label: "Claude Sonnet 4.6" },
      { value: "claude-haiku-4-5-20251001", label: "Claude Haiku 4.5" },
    ],
  },
};

// Track the currently active model env key so save knows which key to write
let _activeModelEnvKey = "OPENAI_MODEL";

// Maps each backend to its API key env var and DOM element IDs.
// Ollama is local-only and has no API key.
const _API_KEY_MAP = {
  openai:    { envKey: "OPENAI_API_KEY",    statusId: "key-status-openai" },
  gemini:    { envKey: "GEMINI_API_KEY",    statusId: "key-status-gemini" },
  anthropic: { envKey: "ANTHROPIC_API_KEY", statusId: "key-status-anthropic" },
};

// Track which API key inputs the user has actually modified — prevents
// overwriting real keys with masked placeholders on save.
const _dirtyApiKeys = new Set();

/**
 * Populate the model dropdown based on the selected LLM backend.
 * If currentValue matches a known model, select it; otherwise add it as a
 * custom entry. Always appends a "Custom…" option at the end.
 */
function populateModelDropdown(backend, currentValue) {
  const modelSelect = $("cfg-llm-model");
  if (!modelSelect) return;

  const entry = _LLM_MODELS[backend] || _LLM_MODELS.openai;
  _activeModelEnvKey = entry.envKey;

  // Clear existing options
  modelSelect.innerHTML = "";

  // Add known models
  let matched = false;
  for (const m of entry.models) {
    const opt = document.createElement("option");
    opt.value = m.value;
    opt.textContent = m.label;
    if (currentValue === m.value) {
      opt.selected = true;
      matched = true;
    }
    modelSelect.appendChild(opt);
  }

  // Add separator + custom option
  const customOpt = document.createElement("option");
  customOpt.value = "__custom__";
  customOpt.textContent = "Custom…";
  modelSelect.appendChild(customOpt);

  // If the current value doesn't match any known model, insert it as a custom entry
  if (currentValue && !matched) {
    const customEntry = document.createElement("option");
    customEntry.value = currentValue;
    customEntry.textContent = `${currentValue} (custom)`;
    customEntry.selected = true;
    modelSelect.insertBefore(customEntry, customOpt);
  }

  // Show/hide the Ollama URL field
  const ollamaUrlField = $("ollama-url-field");
  if (ollamaUrlField) {
    ollamaUrlField.style.display = backend === "ollama" ? "" : "none";
  }

  // Show/hide API key fields based on backend
  updateApiKeyVisibility(backend);
}

/**
 * Show only the API key field matching the selected backend; hide the others.
 * Ollama has no API key, so all key fields are hidden when ollama is selected.
 */
function updateApiKeyVisibility(backend) {
  document.querySelectorAll(".api-key-field").forEach(field => {
    field.style.display = field.dataset.backend === backend ? "" : "none";
  });
}

// ---------------------------------------------------------------------------
// Per-stage LLM override helpers
// ---------------------------------------------------------------------------

// Maps backend value → the env var name pattern for the stage-specific model key.
// E.g. role="TRANSLATOR", backend="openai" → "TRANSLATOR_OPENAI_MODEL"
const _STAGE_MODEL_KEY = (role, backend) =>
  `${role}_${backend.toUpperCase()}_MODEL`;

/**
 * Populate a per-stage model dropdown based on the selected backend for that stage.
 * If backend is "" (global default), clear the dropdown and disable it.
 */
function populateStageModelDropdown(role, backend, currentValue) {
  const modelSelect = document.querySelector(`.stage-model[data-role="${role}"]`);
  if (!modelSelect) return;

  modelSelect.innerHTML = "";

  if (!backend) {
    // Global default — no model override
    const opt = document.createElement("option");
    opt.value = "";
    opt.textContent = "(uses global model)";
    modelSelect.appendChild(opt);
    modelSelect.disabled = true;
    return;
  }

  modelSelect.disabled = false;
  const entry = _LLM_MODELS[backend] || _LLM_MODELS.openai;

  let matched = false;
  for (const m of entry.models) {
    const opt = document.createElement("option");
    opt.value = m.value;
    opt.textContent = m.label;
    if (currentValue === m.value) { opt.selected = true; matched = true; }
    modelSelect.appendChild(opt);
  }

  // If current value is a custom model not in the list, insert it
  if (currentValue && !matched) {
    const customEntry = document.createElement("option");
    customEntry.value = currentValue;
    customEntry.textContent = `${currentValue} (custom)`;
    customEntry.selected = true;
    modelSelect.insertBefore(customEntry, modelSelect.firstChild);
  }
}

/**
 * Load per-stage overrides from config data into the stage override rows.
 */
function loadStageOverrides(configData) {
  document.querySelectorAll(".stage-override-row").forEach(row => {
    const role = row.dataset.role;
    const backendSelect = row.querySelector(".stage-backend");
    const backendVal = configData[`${role}_LLM_BACKEND`] || "";
    if (backendSelect) backendSelect.value = backendVal;

    // Determine model key based on selected backend
    const effectiveBackend = backendVal || "";
    let currentModel = "";
    if (effectiveBackend) {
      currentModel = configData[_STAGE_MODEL_KEY(role, effectiveBackend)] || "";
    }
    populateStageModelDropdown(role, effectiveBackend, currentModel);
  });
}

/**
 * Collect per-stage override values for saving.
 * Returns an object of env key → value pairs.
 */
function collectStageOverrides() {
  const updates = {};
  document.querySelectorAll(".stage-override-row").forEach(row => {
    const role = row.dataset.role;
    const backendSelect = row.querySelector(".stage-backend");
    const modelSelect = row.querySelector(".stage-model");
    const backendVal = backendSelect ? backendSelect.value : "";

    // Always include the backend key (empty string clears the override)
    updates[`${role}_LLM_BACKEND`] = backendVal;

    // Include model key only when a specific backend is selected
    if (backendVal && modelSelect && modelSelect.value) {
      updates[_STAGE_MODEL_KEY(role, backendVal)] = modelSelect.value;
    }
  });
  return updates;
}

/**
 * Handle model dropdown change — if "Custom…" is selected, prompt for input.
 */
function onModelChange() {
  const modelSelect = $("cfg-llm-model");
  if (!modelSelect || modelSelect.value !== "__custom__") return;
  const custom = prompt("Enter custom model name:");
  if (custom && custom.trim()) {
    const opt = document.createElement("option");
    opt.value = custom.trim();
    opt.textContent = `${custom.trim()} (custom)`;
    // Insert before the "Custom…" option
    modelSelect.insertBefore(opt, modelSelect.querySelector('option[value="__custom__"]'));
    opt.selected = true;
  } else {
    // Revert to first option if cancelled
    modelSelect.selectedIndex = 0;
  }
}

// ---------------------------------------------------------------------------
// Settings — load / save
// ---------------------------------------------------------------------------

/**
 * Load current config values from /api/config and populate the settings form.
 * Called when the Settings tab is opened.
 */
async function loadSettings() {
  try {
    const res = await fetch("/api/config");
    const data = await res.json();
    document.querySelectorAll("[data-key]").forEach(el => {
      // Skip API key inputs — they get special handling below
      if (el.classList.contains("api-key-input")) return;
      const val = data[el.dataset.key];
      if (val !== undefined && val !== null) {
        el.value = val;
      }
    });

    // Populate the model dropdown based on the loaded backend
    const backendSelect = $("cfg-llm-backend");
    const backend = backendSelect ? backendSelect.value : "openai";
    const entry = _LLM_MODELS[backend] || _LLM_MODELS.openai;
    const currentModel = data[entry.envKey] || "";
    populateModelDropdown(backend, currentModel);

    // Update API key fields: show masked placeholder + status badge
    _dirtyApiKeys.clear();
    for (const [bk, info] of Object.entries(_API_KEY_MAP)) {
      const isSet = data[info.envKey + "__set"] === true;
      const masked = data[info.envKey] || "";

      // Update the password input with the masked value (non-editable placeholder feel)
      const input = document.querySelector(`.api-key-input[data-key="${info.envKey}"]`);
      if (input) {
        input.value = masked;
        input.dataset.masked = "true";
      }

      // Update the status badge
      const badge = $(info.statusId);
      if (badge) {
        badge.textContent = isSet ? "Set" : "Not set";
        badge.className = "key-status " + (isSet ? "key-set" : "key-missing");
      }
    }

    // Load per-stage LLM overrides
    loadStageOverrides(data);

    // Update attention indicator on Settings tab
    updateSettingsAttention(data);
  } catch (_) {
    // Non-fatal: form fields keep their defaults if load fails
  }
}

/**
 * Collect all settings form fields and POST to /api/config.
 * Only fields with non-empty values are included in the payload.
 */
async function saveSettings() {
  const updates = {};
  document.querySelectorAll("[data-key]").forEach(el => {
    // Only include API key inputs if the user actually typed a new value
    if (el.classList.contains("api-key-input")) {
      if (_dirtyApiKeys.has(el.dataset.key)) {
        updates[el.dataset.key] = el.value.trim();
      }
      return;
    }
    const val = el.value.trim();
    // Send the field even if empty — allows clearing a previously set value
    updates[el.dataset.key] = val;
  });

  // Add the model selection using the correct env key for the active backend
  const modelSelect = $("cfg-llm-model");
  if (modelSelect && modelSelect.value && modelSelect.value !== "__custom__") {
    updates[_activeModelEnvKey] = modelSelect.value;
  }

  // Include per-stage LLM override selections
  Object.assign(updates, collectStageOverrides());

  const data = await postJson("/api/config", updates);
  settingsStatus.textContent = `Saved: ${(data.saved || []).join(", ") || "nothing changed"}`;
  settingsStatus.className = "settings-status ok";

  // Show restart button so user can apply changes
  const restartBtn = $("restart-agent-btn");
  if (restartBtn) restartBtn.style.display = "";

  setTimeout(() => { settingsStatus.textContent = ""; settingsStatus.className = "settings-status"; }, SETTINGS_SAVE_MS);

  // Reload settings to refresh masked values and status badges
  await loadSettings();
}

// ---------------------------------------------------------------------------
// Settings — attention indicator + restart agent
// ---------------------------------------------------------------------------

/**
 * Show a red dot on the Settings tab if the active backend needs an API key
 * that isn't set.  Called from loadSettings() with the config response data.
 */
function updateSettingsAttention(configData) {
  const settingsTab = document.querySelector('[data-tab="settings"]');
  if (!settingsTab) return;
  const backend = (configData.LLM_BACKEND || "openai").toLowerCase();
  const info = _API_KEY_MAP[backend];
  // Ollama is local — never needs an API key
  const needsKey = info && configData[info.envKey + "__set"] === false;
  settingsTab.classList.toggle("needs-attention", !!needsKey);
}

/**
 * Restart just the AI agent process (preserves inbox/outbox).
 * Shows a brief status message while the script runs.
 */
async function restartAgent() {
  const restartBtn = $("restart-agent-btn");
  if (restartBtn) restartBtn.disabled = true;
  settingsStatus.textContent = "Restarting agent...";
  settingsStatus.className = "settings-status";
  try {
    await postJson("/api/restart_agent", {});
    settingsStatus.textContent = "Agent restarting — changes will take effect shortly.";
    settingsStatus.className = "settings-status ok";
    if (restartBtn) restartBtn.style.display = "none";
  } catch (e) {
    settingsStatus.textContent = "Restart failed: " + e.message;
    settingsStatus.className = "settings-status error";
  } finally {
    if (restartBtn) restartBtn.disabled = false;
    setTimeout(() => { settingsStatus.textContent = ""; settingsStatus.className = "settings-status"; }, SETTINGS_SAVE_MS);
  }
}

// ---------------------------------------------------------------------------
// SSE indicator
// ---------------------------------------------------------------------------

function updateSSEIndicator(state) {
  // state: "connected" | "disconnected" | "reconnecting"
  if (indSSE) indSSE.classList.toggle("online", state === "connected");
  if (sseStatusVal) sseStatusVal.textContent = state;
}

// ---------------------------------------------------------------------------
// System overlay (shutdown / restart)
// ---------------------------------------------------------------------------

function showOverlay(mode) {
  hideOverlay();
  const overlay = document.createElement("div");
  overlay.id = "system-overlay";
  overlay.className = "system-overlay";
  if (mode === "shutdown") {
    overlay.innerHTML = `
      <div class="overlay-content">
        <div class="overlay-icon">&#x23FB;</div>
        <h2>System Stopped</h2>
        <p>The Lightning AI system has been shut down.</p>
        <p class="overlay-hint">Close this tab or restart the server to continue.</p>
      </div>`;
  } else if (mode === "restarting") {
    overlay.innerHTML = `
      <div class="overlay-content">
        <div class="overlay-icon spinner">&#x21BA;</div>
        <h2>Restarting…</h2>
        <p>The system is cycling. Reconnecting shortly.</p>
      </div>`;
  }
  document.body.appendChild(overlay);
}

function hideOverlay() {
  const existing = document.getElementById("system-overlay");
  if (existing) existing.remove();
}

// ---------------------------------------------------------------------------
// Clear All (multi-step confirmation)
// ---------------------------------------------------------------------------

/**
 * Three-step confirmation flow for the destructive "Clear All" operation.
 *
 * Step 0 (initial): "Clear All Data" button.
 * Step 1: Click -> button becomes "Are you sure? Click again to confirm".
 *           Auto-reverts after 5 seconds.
 * Step 2: Click again -> replaced with text input + Confirm/Cancel.
 *           User must type "CLEAR" to enable Confirm. Auto-reverts after 10s.
 * Step 3: Final confirm -> POST /api/fresh, clear local UI state.
 */
let _clearStep = 0;
let _clearTimeout = null;

function resetClearAll() {
  _clearStep = 0;
  if (_clearTimeout) { clearTimeout(_clearTimeout); _clearTimeout = null; }
  const action = $("clear-all-action");
  if (!action) return;
  action.innerHTML = '<button id="clear-all-btn" class="btn-danger-sm">Clear All Data</button>';
  $("clear-all-btn").addEventListener("click", advanceClearAll);
}

function advanceClearAll() {
  if (_clearTimeout) { clearTimeout(_clearTimeout); _clearTimeout = null; }

  if (_clearStep === 0) {
    _clearStep = 1;
    const btn = $("clear-all-btn");
    btn.textContent = "Are you sure? Click again to confirm";
    btn.classList.add("btn-danger-sm-active");
    _clearTimeout = setTimeout(resetClearAll, CLEAR_STEP1_MS);
  } else if (_clearStep === 1) {
    _clearStep = 2;
    const action = $("clear-all-action");
    action.innerHTML = `
      <div class="clear-confirm-row">
        <label class="clear-confirm-label">Type <code>CLEAR</code> to confirm:</label>
        <input id="clear-confirm-input" class="settings-input clear-confirm-input"
               type="text" placeholder="CLEAR" autocomplete="off">
        <button id="clear-confirm-btn" class="btn-danger-sm" disabled>Confirm</button>
        <button id="clear-cancel-btn" class="btn-ghost small">Cancel</button>
      </div>`;
    const input = $("clear-confirm-input");
    const confirmBtn = $("clear-confirm-btn");
    input.addEventListener("input", () => {
      confirmBtn.disabled = input.value.trim() !== "CLEAR";
    });
    confirmBtn.addEventListener("click", executeClearAll);
    $("clear-cancel-btn").addEventListener("click", resetClearAll);
    input.focus();
    _clearTimeout = setTimeout(resetClearAll, CLEAR_STEP2_MS);
  }
}

async function executeClearAll() {
  if (_clearTimeout) { clearTimeout(_clearTimeout); _clearTimeout = null; }
  const action = $("clear-all-action");
  action.innerHTML = '<span class="clear-status">Clearing all data…</span>';
  try {
    await postJson("/api/fresh", {});
    action.innerHTML = '<span class="clear-status ok">All data cleared. Agent restarting…</span>';
    // Clear local UI state
    traceLog.innerHTML = '<div class="empty-state">No trace events yet.</div>';
    _seenTraceTs.clear();
    archiveList.innerHTML = '<div class="empty-state">No archived queries yet.</div>';
    summaryCard.style.display = "none";
    intentDisplay.innerHTML = '<div class="empty-state">No pipeline run yet.</div>';
    planDisplay.innerHTML = '<div class="empty-state">No pipeline run yet.</div>';
    execDisplay.innerHTML = '<div class="empty-state">No pipeline run yet.</div>';
    const mc = $("metrics-content");
    if (mc) mc.innerHTML = '<div class="empty-state">No queries yet.</div>';
    setBadge(badgeTranslator, stageTranslator, "");
    setBadge(badgePlanner, stagePlanner, "");
    setBadge(badgeExecutor, stageExecutor, "");
    setLog("System cleared. Agent restarting fresh…");
    setTimeout(resetClearAll, CLEAR_COMPLETE_MS);
  } catch (e) {
    action.innerHTML = `<span class="clear-status error">Error: ${esc(e.message)}</span>`;
    setTimeout(resetClearAll, CLEAR_COMPLETE_MS);
  }
}

// ---------------------------------------------------------------------------
// Shutdown
// ---------------------------------------------------------------------------

/**
 * Ask for confirmation then POST /api/shutdown.
 * Uses a native confirm() dialog so there is no accidental trigger.
 */
async function shutdownSystem() {
  if (!confirm("Shut down the Lightning AI system?\n\nThis will stop all Lightning nodes, the AI agent, and this UI.")) {
    return;
  }
  setLog("Shutting down…");
  try {
    await postJson("/api/shutdown", {});
    _shutdownRequested = true;
    setLog("Shutdown initiated. The system is stopping…");
    // Disable controls to prevent confused clicks after shutdown
    $("ask-btn").disabled = true;
    $("health-btn").disabled = true;
    $("restart-btn").disabled = true;
    $("shutdown-btn").disabled = true;
    promptInput.disabled = true;
  } catch (e) {
    setLog("Shutdown request failed: " + e.message, true);
  }
}

/**
 * Ask for confirmation then POST /api/restart.
 * Runs shutdown.sh → 1.start.sh in a detached background process.
 * The UI will briefly disconnect while the system restarts.
 */
async function restartSystem() {
  if (!confirm("Restart the Lightning AI system?\n\nThis will stop and restart all Lightning nodes, the AI agent, and this UI. The page will briefly disconnect.")) {
    return;
  }
  setLog("Restarting…");
  try {
    await postJson("/api/restart", {});
    _restartRequested = true;
    setLog("Restart initiated. The system is cycling — reconnecting shortly…");
  } catch (e) {
    setLog("Restart request failed: " + e.message, true);
  }
}

// ---------------------------------------------------------------------------
// Event listeners
// ---------------------------------------------------------------------------
$("strategy-btn").addEventListener("click", cycleStrategy);
$("ask-btn").addEventListener("click",     () => queueAsk().catch(e => setLog(e.message, true)));
$("health-btn").addEventListener("click",  () => queueHealth().catch(e => setLog(e.message, true)));
$("refresh-btn").addEventListener("click", () => refreshAll().catch(e => setLog(e.message, true)));
$("network-refresh-btn").addEventListener("click", () => fetchNetwork().catch(() => {}));
$("restart-btn").addEventListener("click",  () => restartSystem().catch(e => setLog(e.message, true)));
$("shutdown-btn").addEventListener("click", () => shutdownSystem().catch(e => setLog(e.message, true)));
$("crash-kit-btn").addEventListener("click", () => copyCrashKit().catch(() => {}));
$("inbox-clear-btn").addEventListener("click",  clearInbox);
$("outbox-clear-btn").addEventListener("click", clearOutbox);
$("settings-save-btn").addEventListener("click", () =>
  saveSettings().catch(e => {
    settingsStatus.textContent = "Save failed: " + e.message;
    settingsStatus.className = "settings-status error";
  })
);

// Backend dropdown → repopulate model list when backend changes
$("cfg-llm-backend").addEventListener("change", () => {
  const backend = $("cfg-llm-backend").value;
  populateModelDropdown(backend, "");
});

// Model dropdown → handle "Custom…" selection
$("cfg-llm-model").addEventListener("change", onModelChange);

// Initialize model dropdown on page load (default to openai until settings load)
populateModelDropdown($("cfg-llm-backend").value, "");

// Collapsible toggle for per-stage overrides section
$("stage-overrides-toggle").addEventListener("click", () => {
  const body = $("stage-overrides-body");
  const arrow = document.querySelector(".collapse-arrow");
  const open = !body.hidden;
  body.hidden = open;
  if (arrow) arrow.textContent = open ? "\u25B6" : "\u25BC"; // ▶ / ▼
});

// Per-stage backend dropdowns → repopulate model list when backend changes
document.querySelectorAll(".stage-backend").forEach(sel => {
  sel.addEventListener("change", () => {
    const role = sel.closest(".stage-override-row").dataset.role;
    populateStageModelDropdown(role, sel.value, "");
  });
});

// Initialize per-stage model dropdowns (all disabled until settings load)
document.querySelectorAll(".stage-override-row").forEach(row => {
  populateStageModelDropdown(row.dataset.role, "", "");
});

// Restart Agent button
$("restart-agent-btn").addEventListener("click", () =>
  restartAgent().catch(() => {})
);

// API key inputs: clear masked value on focus, track dirty state on input
document.querySelectorAll(".api-key-input").forEach(input => {
  input.addEventListener("focus", () => {
    if (input.dataset.masked === "true") {
      input.value = "";
      input.dataset.masked = "false";
    }
  });
  input.addEventListener("input", () => {
    _dirtyApiKeys.add(input.dataset.key);
  });
});

// API key show/hide toggle buttons
document.querySelectorAll(".key-toggle").forEach(btn => {
  btn.addEventListener("click", () => {
    const input = $(btn.dataset.target);
    if (!input) return;
    const showing = input.type === "text";
    input.type = showing ? "password" : "text";
    btn.textContent = showing ? "Show" : "Hide";
  });
});

$("clear-all-btn").addEventListener("click", advanceClearAll);

// Enter submits; Shift+Enter inserts a newline (default textarea behavior)
promptInput.addEventListener("keydown", e => {
  if (e.key === "Enter" && !e.shiftKey) {
    e.preventDefault();
    queueAsk().catch(err => setLog(err.message, true));
  }
});

// ---------------------------------------------------------------------------
// Server-Sent Events (primary) + polling fallback
// ---------------------------------------------------------------------------

// Tracks whether the SSE connection is currently active.
// The polling intervals check this flag to avoid double-fetching when SSE is working.
let _sseActive = false;

// Set by shutdownSystem() / restartSystem() after a successful POST.
// The SSE onerror handler checks these to decide overlay vs reconnect behavior.
let _shutdownRequested = false;
let _restartRequested = false;

/**
 * Open a Server-Sent Events connection to /api/stream.
 *
 * The server pushes three event types:
 *   "status"          → updateStatusBar + renderQueue (inbox/outbox)
 *   "pipeline_result" → renderPipelineResult + optional archive refresh
 *   "trace"           → renderTrace (live trace log)
 *
 * Error handling: on any connection error, the SSE connection is closed and
 * a reconnect is scheduled after 5 seconds. This handles server restarts,
 * network interruptions, and browser tab restore.
 *
 * Returns false if EventSource is not supported (very old browser).
 */
function startSSE() {
  if (typeof EventSource === "undefined") return false;
  const es = new EventSource("/api/stream");

  es.addEventListener("status", e => {
    try {
      const data = JSON.parse(e.data);
      updateStatusBar(data);
      renderQueue(inboxList, inboxCount, data.recent_inbox, "Req", _inboxClearTs);
      renderQueue(outboxList, outboxCount, data.recent_outbox, "Rep", _outboxClearTs);
    } catch (_) {}
  });

  es.addEventListener("pipeline_result", e => {
    try {
      const { result } = JSON.parse(e.data);
      if (result) renderPipelineResult(result);
      // Auto-refresh the archive list when a query completes, if the panel is open.
      // This ensures newly archived entries appear without the user needing to
      // manually click the refresh button.
      if (!archivePanel.hasAttribute("hidden")) fetchAndRenderArchiveList().catch(() => {});
      // Always refresh metrics when a new pipeline result arrives
      fetchAndRenderMetrics().catch(() => {});
    } catch (_) {}
  });

  es.addEventListener("trace", e => {
    try {
      const { events } = JSON.parse(e.data);
      renderTrace(events);
    } catch (_) {}
  });

  es.onopen = () => {
    _sseActive = true;
    updateSSEIndicator("connected");
    if (_restartRequested) {
      _restartRequested = false;
      hideOverlay();
    }
  };

  es.onerror = () => {
    _sseActive = false;
    es.close();
    updateSSEIndicator("disconnected");

    if (_shutdownRequested) {
      // Server is gone permanently — show overlay, don't reconnect.
      try { window.close(); } catch (_) {}
      showOverlay("shutdown");
      return;
    }

    if (_restartRequested) {
      showOverlay("restarting");
    }

    updateSSEIndicator("reconnecting");
    setTimeout(startSSE, SSE_RECONNECT_MS);
  };

  return true;
}

// ---------------------------------------------------------------------------
// Token streaming SSE (summarizer live output)
// ---------------------------------------------------------------------------

/**
 * Open an SSE connection to /api/tokens for streaming LLM summary tokens.
 *
 * The server tails runtime/agent/stream.jsonl and emits one SSE event per
 * line. Three event types are handled:
 *
 *   stream_start → show summary card with empty body + blinking cursor
 *   token        → append the token text to summaryBody
 *   stream_end   → remove blinking cursor (pipeline_result SSE will follow
 *                  with the authoritative final content shortly after)
 *
 * The streaming preview is intentionally overwritten when the pipeline_result
 * SSE arrives — it is a live preview, not the final result.
 */
function startTokenSSE() {
  if (typeof EventSource === "undefined") return;
  const es = new EventSource("/api/tokens");

  es.addEventListener("token", e => {
    try {
      const obj = JSON.parse(e.data);
      if (obj.event === "stream_start") {
        // A new summarizer run is starting — show the card and clear old content
        summaryCard.style.display = "";
        summaryIcon.textContent = "…";
        summaryIcon.className = "summary-icon";
        summaryTs.textContent = fmtDateTime(obj.ts);
        summaryBody.textContent = "";
        summaryBody.classList.add("streaming");
      } else if (obj.event === "token") {
        summaryBody.textContent += obj.text;
      } else if (obj.event === "stream_end") {
        summaryBody.classList.remove("streaming");
      }
    } catch (_) {}
  });

  es.onerror = () => {
    es.close();
    if (_shutdownRequested) return;  // Don't reconnect after shutdown
    setTimeout(startTokenSSE, SSE_RECONNECT_MS);
  };
}

// ---------------------------------------------------------------------------
// Tab switching
// ---------------------------------------------------------------------------

/**
 * Wire up the tab bar buttons to show/hide the corresponding panels.
 *
 * Each tab button has data-tab="<name>" and each panel has id="tab-<name>".
 * Clicking a button activates it and shows the matching panel; all other
 * buttons/panels are deactivated/hidden.
 */
function initTabs() {
  const buttons = document.querySelectorAll(".tab-btn");
  const panels  = document.querySelectorAll(".tab-panel");

  buttons.forEach(btn => {
    btn.addEventListener("click", () => {
      const target = btn.dataset.tab;
      buttons.forEach(b => b.classList.toggle("active", b === btn));
      panels.forEach(p => { p.hidden = p.id !== "tab-" + target; });
      // Populate settings form with live values whenever the tab is opened
      if (target === "settings") loadSettings().catch(() => {});
    });
  });
}

// ---------------------------------------------------------------------------
// Initialization
// ---------------------------------------------------------------------------

initTabs();

// Load initial data immediately so the UI isn't blank on first render.
// Any fetch error is shown in the action log (non-blocking).
refreshAll().catch(e => setLog(e.message, true));

// Load metrics on page load
fetchAndRenderMetrics().catch(() => {});

// Open the SSE stream for live updates.
startSSE();

// Open the token streaming SSE for live summarizer output.
startTokenSSE();

// Polling fallback intervals — only fire when SSE is not delivering updates.
// This handles browsers that don't support EventSource or networks that block SSE.
setInterval(() => { if (!_sseActive && !_shutdownRequested) fetchStatus().catch(() => {}); }, POLL_STATUS_MS);
setInterval(() => { if (!_sseActive && !_shutdownRequested) fetchPipelineResult().catch(() => {}); }, POLL_PIPELINE_MS);
setInterval(() => { if (!_sseActive && !_shutdownRequested) fetchTrace().catch(() => {}); }, POLL_TRACE_MS);

// Network graph always polls independently — it's infrequent and not
// pushed via SSE because network topology rarely changes mid-session.
setInterval(() => { if (!_shutdownRequested) fetchNetwork().catch(() => {}); }, POLL_NETWORK_MS);

// ---------------------------------------------------------------------------
// Copy buttons (global delegated listener)
// ---------------------------------------------------------------------------
// A single delegated listener on document handles all copy buttons regardless
// of when they were added to the DOM (works for dynamically rendered queue items).
document.addEventListener('click', e => {
  const btn = e.target.closest('.copy-btn');
  if (!btn) return;
  let text;
  if (btn.dataset.copy) {
    // Inline copy: data-copy attribute contains the text to copy directly
    text = btn.dataset.copy;
  } else {
    // Target copy: data-target contains the ID of the element to copy from
    const target = document.getElementById(btn.dataset.target);
    if (!target) return;
    text = target.innerText;
  }
  navigator.clipboard.writeText(text).then(() => {
    // Visual feedback: briefly show ✓ before reverting to ⎘
    btn.textContent = '✓';
    btn.classList.add('copied');
    setTimeout(() => { btn.textContent = '⎘'; btn.classList.remove('copied'); }, COPY_SUCCESS_MS);
  });
});

// ---------------------------------------------------------------------------
// Clear buttons (global delegated listener)
// ---------------------------------------------------------------------------
document.addEventListener('click', e => {
  const btn = e.target.closest('.clear-btn');
  if (!btn) return;
  if (btn.dataset.clear === 'trace-log') {
    // Reset the trace log DOM — the _seenTraceTs dedup set is intentionally
    // NOT cleared so previously seen events won't be re-appended by SSE.
    document.getElementById('trace-log').innerHTML = '<div class="empty-state">No trace events yet.</div>';
  }
});

// ---------------------------------------------------------------------------
// Archive panel controls
// ---------------------------------------------------------------------------

/**
 * Toggle the archive panel open/closed.
 *
 * On open: remove the "hidden" attribute and fetch the archive list.
 * On close: restore "hidden" and flip the arrow indicator.
 *
 * The HTML "hidden" attribute is used (not CSS display) so keyboard
 * navigation and screen readers also skip the hidden content.
 */
archiveToggleBtn.addEventListener("click", async () => {
  const isHidden = archivePanel.hasAttribute("hidden");
  if (isHidden) {
    archivePanel.removeAttribute("hidden");
    archiveToggleBtn.textContent = "Archive ▴";  // Arrow up = panel is open
    await fetchAndRenderArchiveList();
  } else {
    archivePanel.setAttribute("hidden", "");
    archiveToggleBtn.textContent = "Archive ▾";  // Arrow down = panel is closed
  }
});

// Manual refresh button inside the archive panel header
$("archive-refresh-btn").addEventListener("click", () =>
  fetchAndRenderArchiveList().catch(() => {})
);

// Archive search input — re-fetch filtered list as the user types
$("archive-search").addEventListener("input", () =>
  fetchAndRenderArchiveList().catch(() => {})
);

// Archive status filter pills — one active at a time; re-fetch on click
document.addEventListener("click", e => {
  const btn = e.target.closest(".arch-filter-btn");
  if (!btn) return;
  document.querySelectorAll(".arch-filter-btn").forEach(b => b.classList.remove("active"));
  btn.classList.add("active");
  fetchAndRenderArchiveList().catch(() => {});
});

/**
 * Click-to-expand archive entries.
 *
 * Uses event delegation on the archiveList container so it works for entries
 * added after the initial render.
 *
 * Toggle behaviour:
 *   - If the entry already has an .archive-trace-inline child: remove it (collapse)
 *   - Otherwise: create the child, show a loading placeholder, fetch the trace,
 *     and render it (expand)
 *
 * Only one entry can be partially loaded at a time — clicking another entry
 * while one is loading shows two expanded entries simultaneously, which is
 * acceptable (the loading placeholder is replaced by real data when it arrives).
 */
archiveList.addEventListener("click", async (e) => {
  const entry = e.target.closest(".archive-entry");
  if (!entry) return;
  const filename = entry.dataset.filename;
  if (!filename) return;

  // Toggle: if already expanded, collapse by removing the inline trace div
  const existing = entry.querySelector(".archive-trace-inline");
  if (existing) {
    existing.remove();
    entry.classList.remove("expanded");
    return;
  }

  // Expand: add loading placeholder immediately for visual feedback
  entry.classList.add("expanded");
  const placeholder = document.createElement("div");
  placeholder.className = "archive-trace-inline";
  placeholder.innerHTML = '<div class="empty-state">Loading…</div>';
  entry.appendChild(placeholder);

  // Fetch and render (placeholder is replaced when data arrives)
  const events = await fetchArchiveFile(filename);
  renderArchiveTrace(placeholder, events || []);
});
