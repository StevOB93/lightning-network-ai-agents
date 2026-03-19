// Lightning Agent UI — app.js

// ---------------------------------------------------------------------------
// DOM refs
// ---------------------------------------------------------------------------
const $ = (id) => document.getElementById(id);

const promptInput    = $("prompt-input");
const actionLog      = $("action-log");
const agentLockVal   = $("agent-lock-val");
const lastRequestId  = $("last-request-id");
const msgCount       = $("msg-count");
const pipelineBuild  = $("pipeline-build");
const indAgent       = $("ind-agent");

const intentDisplay  = $("intent-display");
const planDisplay    = $("plan-display");
const execDisplay    = $("exec-display");

const badgeTranslator = $("badge-translator");
const badgePlanner    = $("badge-planner");
const badgeExecutor   = $("badge-executor");

const stageTranslator = $("stage-translator");
const stagePlanner    = $("stage-planner");
const stageExecutor   = $("stage-executor");

const summaryCard  = $("summary-card");
const summaryBody  = $("summary-body");
const summaryTs    = $("summary-ts");
const summaryIcon  = $("summary-icon");

const traceLog     = $("trace-log");
const inboxList    = $("inbox-list");
const outboxList   = $("outbox-list");
const inboxCount   = $("inbox-count");
const outboxCount  = $("outbox-count");
const networkViz   = $("network-viz");
const networkHint  = $("network-hint");

// ---------------------------------------------------------------------------
// Utilities
// ---------------------------------------------------------------------------
function fmtTs(ts) {
  if (!ts) return "—";
  return new Date(ts * 1000).toLocaleTimeString();
}

function fmtDateTime(ts) {
  if (!ts) return "—";
  return new Date(ts * 1000).toLocaleString();
}

function esc(str) {
  return String(str ?? "").replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;");
}

function truncate(str, n = 120) {
  const s = String(str ?? "");
  return s.length > n ? s.slice(0, n) + "…" : s;
}

function setLog(text, isError = false) {
  actionLog.textContent = text;
  actionLog.className = "action-log" + (isError ? " error" : "");
}

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
function updateStatusBar(status) {
  const lock = status.agent_lock || "";
  agentLockVal.textContent = lock ? lock.split(" ")[0] : "no lock";
  indAgent.classList.toggle("online", !!lock);

  const last = status.last_outbox;
  lastRequestId.textContent = last?.request_id ?? "—";
  msgCount.textContent = status.message_count ?? 0;

  if (last?.pipeline_build) {
    pipelineBuild.textContent = last.pipeline_build;
  }
}

// ---------------------------------------------------------------------------
// Pipeline stages
// ---------------------------------------------------------------------------
function setBadge(el, stageEl, state) {
  el.textContent = state === "ok" ? "✓ done" : state === "fail" ? "✗ failed" : state === "skip" ? "⊘ skipped" : "—";
  el.className = "stage-badge " + state;
  stageEl.className = "stage-card " + state;
}

function renderIntent(intent) {
  if (!intent) {
    intentDisplay.innerHTML = '<div class="empty-state">Intent not available.</div>';
    return;
  }
  const typeClass = {
    open_channel: "tag-blue", set_fee: "tag-orange", rebalance: "tag-purple",
    pay_invoice: "tag-green", noop: "tag-gray", freeform: "tag-teal",
  }[intent.intent_type] || "tag-gray";

  const ctxRows = Object.entries(intent.context || {})
    .map(([k, v]) => `<tr><td class="ctx-key">${esc(k)}</td><td class="ctx-val">${esc(JSON.stringify(v))}</td></tr>`)
    .join("");

  const criteria = (intent.success_criteria || [])
    .map(c => `<li>${esc(c)}</li>`).join("");

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

function renderPlan(plan) {
  if (!plan || !plan.steps?.length) {
    planDisplay.innerHTML = plan
      ? '<div class="empty-state">Empty plan (noop).</div>'
      : '<div class="empty-state">Plan not available.</div>';
    return;
  }

  const steps = plan.steps.map((s, i) => `
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

function renderExecution(stepResults, stageFailed) {
  if (!stepResults?.length) {
    execDisplay.innerHTML = '<div class="empty-state">No steps executed.</div>';
    return;
  }

  const rows = stepResults.map(sr => {
    const statusClass = sr.skipped ? "step-skipped" : sr.ok ? "step-ok" : "step-fail";
    const statusIcon = sr.skipped ? "⊘" : sr.ok ? "✓" : "✗";
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
        <div class="step-args muted">${esc(truncate(JSON.stringify(sr.args), 160))}</div>
        ${errRow}
      </div>
    `;
  }).join("");

  execDisplay.innerHTML = `<div class="exec-steps">${rows}</div>`;
}

function renderPipelineResult(result) {
  if (!result) return;

  const stageFailed = result.stage_failed;

  // Translator stage
  if (result.intent) {
    setBadge(badgeTranslator, stageTranslator, "ok");
    renderIntent(result.intent);
  } else if (stageFailed === "translator") {
    setBadge(badgeTranslator, stageTranslator, "fail");
    intentDisplay.innerHTML = `<div class="stage-error">${esc(result.error || "Translation failed")}</div>`;
  }

  // Planner stage
  if (result.plan) {
    setBadge(badgePlanner, stagePlanner, "ok");
    renderPlan(result.plan);
  } else if (stageFailed === "planner") {
    setBadge(badgePlanner, stagePlanner, "fail");
    planDisplay.innerHTML = `<div class="stage-error">${esc(result.error || "Planning failed")}</div>`;
  } else if (result.intent && !result.plan) {
    setBadge(badgePlanner, stagePlanner, "skip");
  }

  // Executor stage
  if (result.step_results?.length) {
    const allOk = result.step_results.every(r => r.ok || r.skipped);
    setBadge(badgeExecutor, stageExecutor, stageFailed === "executor" ? "fail" : allOk ? "ok" : "fail");
    renderExecution(result.step_results, stageFailed);
  } else if (stageFailed === "executor") {
    setBadge(badgeExecutor, stageExecutor, "fail");
    execDisplay.innerHTML = `<div class="stage-error">${esc(result.error || "Execution failed")}</div>`;
  } else if (result.plan) {
    setBadge(badgeExecutor, stageExecutor, "skip");
  }

  // Summary card
  if (result.content || result.human_summary || result.error) {
    summaryCard.style.display = "";
    summaryBody.textContent = result.content || result.human_summary || result.error || "";
    summaryTs.textContent = fmtDateTime(result.ts);
    const ok = result.success !== false && !result.error;
    summaryIcon.textContent = ok ? "✓" : "✗";
    summaryIcon.className = "summary-icon " + (ok ? "ok" : "fail");
  }
}

// ---------------------------------------------------------------------------
// Trace log
// ---------------------------------------------------------------------------
const _seenTraceTs = new Set();

function renderTrace(events) {
  if (!events?.length) return;

  let appended = false;
  const wasAtBottom = traceLog.scrollHeight - traceLog.scrollTop <= traceLog.clientHeight + 40;

  // Clear empty state once
  if (traceLog.querySelector(".empty-state")) {
    traceLog.innerHTML = "";
  }

  for (const ev of events) {
    const key = `${ev.ts}-${ev.kind || ev.event || JSON.stringify(ev).slice(0, 40)}`;
    if (_seenTraceTs.has(key)) continue;
    _seenTraceTs.add(key);
    appended = true;

    const row = document.createElement("div");
    row.className = "trace-row";
    const kind = ev.kind || ev.event || ev.stage || "event";
    const ts = fmtTs(ev.ts);
    const detail = (() => {
      const copy = { ...ev };
      delete copy.ts; delete copy.kind; delete copy.event; delete copy.stage;
      const s = JSON.stringify(copy);
      return s === "{}" ? "" : truncate(s, 200);
    })();
    row.innerHTML = `<span class="trace-ts">${ts}</span><span class="trace-kind">${esc(kind)}</span>${detail ? `<span class="trace-detail">${esc(detail)}</span>` : ""}`;
    traceLog.appendChild(row);
  }

  if (appended && wasAtBottom) {
    traceLog.scrollTop = traceLog.scrollHeight;
  }
}

// ---------------------------------------------------------------------------
// Inbox / Outbox
// ---------------------------------------------------------------------------
function renderQueue(container, countEl, items, labelPrefix) {
  countEl.textContent = items?.length ?? 0;
  if (!items?.length) {
    container.innerHTML = '<div class="empty-state">No entries yet.</div>';
    return;
  }
  container.innerHTML = items.slice().reverse().map(item => {
    const id = item.request_id ?? item.id ?? "?";
    const body = item.content ?? JSON.stringify(item.meta ?? item.summary ?? {});
    return `
      <div class="queue-item">
        <div class="queue-item-head">
          <span>${labelPrefix} ${id}</span>
          <span class="muted">${fmtTs(item.ts)}</span>
        </div>
        <div class="queue-item-body muted">${esc(truncate(body, 140))}</div>
      </div>`;
  }).join("");
}

// ---------------------------------------------------------------------------
// Network visualization (D3 force graph)
// ---------------------------------------------------------------------------
let _networkSvg = null;
let _simulation = null;

function nodeId(n) {
  return n.nodeid || n.node_id || n.id || n.pub_key || n.pubkey || String(n);
}

function nodeLabel(n) {
  const id = nodeId(n);
  const alias = n.alias || n.label || "";
  return alias ? alias : id.slice(0, 8) + "…";
}

function renderNetwork(data) {
  const rawNodes = data.nodes || [];
  const rawChannels = data.channels || [];

  if (!rawNodes.length && !rawChannels.length) {
    networkViz.innerHTML = '<div class="empty-state">Run a health check or node query to populate the graph.</div>';
    networkHint.textContent = "Populated from tool call results";
    if (_simulation) { _simulation.stop(); _simulation = null; }
    if (_networkSvg) { _networkSvg.remove(); _networkSvg = null; }
    return;
  }

  // Build node index
  const nodeMap = new Map();
  rawNodes.forEach(n => {
    const id = nodeId(n);
    nodeMap.set(id, { id, label: nodeLabel(n), raw: n });
  });

  // Build edges, auto-creating nodes for endpoints not in node list
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
      active: ch.active !== false,
    });
  });

  const nodes = Array.from(nodeMap.values());

  // Clear previous
  if (_simulation) _simulation.stop();
  networkViz.innerHTML = "";

  const W = networkViz.clientWidth || 700;
  const H = Math.max(320, Math.min(500, nodes.length * 60));

  const svg = d3.select(networkViz).append("svg")
    .attr("width", W)
    .attr("height", H)
    .attr("viewBox", `0 0 ${W} ${H}`);

  _networkSvg = svg.node();

  // Zoom/pan
  const g = svg.append("g");
  svg.call(d3.zoom().scaleExtent([0.3, 4]).on("zoom", e => g.attr("transform", e.transform)));

  // Arrow marker
  svg.append("defs").append("marker")
    .attr("id", "arrow").attr("viewBox", "0 -4 8 8").attr("refX", 18).attr("refY", 0)
    .attr("markerWidth", 6).attr("markerHeight", 6).attr("orient", "auto")
    .append("path").attr("d", "M0,-4L8,0L0,4").attr("fill", "var(--accent)");

  // Links
  const link = g.append("g").selectAll("line").data(links).join("line")
    .attr("stroke", d => d.active ? "var(--accent)" : "var(--line-strong)")
    .attr("stroke-width", d => d.active ? 2 : 1)
    .attr("stroke-opacity", 0.7)
    .attr("marker-end", "url(#arrow)");

  // Link capacity labels
  const linkLabel = g.append("g").selectAll("text").data(links).join("text")
    .attr("font-size", 9).attr("fill", "var(--muted)").attr("text-anchor", "middle")
    .attr("font-family", "IBM Plex Mono, monospace")
    .text(d => d.capacity ? `${(d.capacity / 1_000_000).toFixed(2)}M` : "");

  // Node circles
  const node = g.append("g").selectAll("g").data(nodes).join("g")
    .attr("cursor", "pointer")
    .call(d3.drag()
      .on("start", (e, d) => { if (!e.active) _simulation.alphaTarget(0.3).restart(); d.fx = d.x; d.fy = d.y; })
      .on("drag",  (e, d) => { d.fx = e.x; d.fy = e.y; })
      .on("end",   (e, d) => { if (!e.active) _simulation.alphaTarget(0); d.fx = null; d.fy = null; }));

  node.append("circle")
    .attr("r", 16)
    .attr("fill", "var(--accent-dim)")
    .attr("stroke", "var(--accent)")
    .attr("stroke-width", 2);

  node.append("text")
    .attr("text-anchor", "middle").attr("dy", "0.35em")
    .attr("font-size", 10).attr("font-weight", "700")
    .attr("fill", "var(--ink)").attr("font-family", "IBM Plex Mono, monospace")
    .text(d => d.label.slice(0, 6));

  node.append("title").text(d => d.id);

  // Force simulation
  _simulation = d3.forceSimulation(nodes)
    .force("link", d3.forceLink(links).id(d => d.id).distance(120))
    .force("charge", d3.forceManyBody().strength(-300))
    .force("center", d3.forceCenter(W / 2, H / 2))
    .force("collide", d3.forceCollide(30))
    .on("tick", () => {
      link
        .attr("x1", d => d.source.x).attr("y1", d => d.source.y)
        .attr("x2", d => d.target.x).attr("y2", d => d.target.y);
      linkLabel
        .attr("x", d => (d.source.x + d.target.x) / 2)
        .attr("y", d => (d.source.y + d.target.y) / 2 - 6);
      node.attr("transform", d => `translate(${d.x},${d.y})`);
    });

  networkHint.textContent = `${nodes.length} node${nodes.length !== 1 ? "s" : ""}, ${links.length} channel${links.length !== 1 ? "s" : ""}`;
}

// ---------------------------------------------------------------------------
// API calls
// ---------------------------------------------------------------------------
async function fetchStatus() {
  const res = await fetch("/api/status");
  const data = await res.json();
  updateStatusBar(data);
  renderQueue(inboxList, inboxCount, data.recent_inbox, "Req");
  renderQueue(outboxList, outboxCount, data.recent_outbox, "Rep");
}

async function fetchPipelineResult() {
  const res = await fetch("/api/pipeline_result");
  const { result } = await res.json();
  if (result) renderPipelineResult(result);
}

async function fetchTrace() {
  const res = await fetch("/api/trace");
  const { events } = await res.json();
  renderTrace(events);
}

async function fetchNetwork() {
  const res = await fetch("/api/network");
  const data = await res.json();
  renderNetwork(data);
}

async function refreshAll() {
  await Promise.allSettled([fetchStatus(), fetchPipelineResult(), fetchTrace(), fetchNetwork()]);
}

async function queueAsk() {
  const text = promptInput.value.trim();
  if (!text) { setLog("Enter a prompt first.", true); return; }
  setLog("Queuing request…");
  const data = await postJson("/api/ask", { text });
  setLog(`Queued request #${data.msg.id}. Waiting for agent…`);
  await refreshAll();
}

async function queueHealth() {
  setLog("Queuing health check…");
  const data = await postJson("/api/health", {});
  setLog(`Queued health check #${data.msg.id}. Waiting for agent…`);
  await refreshAll();
}

// ---------------------------------------------------------------------------
// Event listeners
// ---------------------------------------------------------------------------
$("ask-btn").addEventListener("click",     () => queueAsk().catch(e => setLog(e.message, true)));
$("health-btn").addEventListener("click",  () => queueHealth().catch(e => setLog(e.message, true)));
$("refresh-btn").addEventListener("click", () => refreshAll().catch(e => setLog(e.message, true)));
$("network-refresh-btn").addEventListener("click", () => fetchNetwork().catch(() => {}));

promptInput.addEventListener("keydown", e => {
  if (e.key === "Enter" && (e.ctrlKey || e.metaKey)) {
    queueAsk().catch(err => setLog(err.message, true));
  }
});

// ---------------------------------------------------------------------------
// Polling
// ---------------------------------------------------------------------------
refreshAll().catch(e => setLog(e.message, true));
setInterval(() => fetchStatus().catch(() => {}), 2500);
setInterval(() => fetchPipelineResult().catch(() => {}), 3000);
setInterval(() => fetchTrace().catch(() => {}), 2000);
setInterval(() => fetchNetwork().catch(() => {}), 15000);
