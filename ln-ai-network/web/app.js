const reportContent = document.getElementById("report-content");
const lastTs = document.getElementById("last-ts");
const agentLock = document.getElementById("agent-lock");
const lastRequest = document.getElementById("last-request");
const inboxList = document.getElementById("inbox-list");
const outboxList = document.getElementById("outbox-list");
const actionLog = document.getElementById("action-log");
const promptInput = document.getElementById("prompt-input");

function formatTimestamp(ts) {
  if (!ts) return "Unknown";
  return new Date(ts * 1000).toLocaleString();
}

function setLog(text) {
  actionLog.textContent = text;
}

function renderItems(container, items, keyField) {
  container.innerHTML = "";
  if (!items || !items.length) {
    container.innerHTML = '<div class="item"><div class="item-body">No entries yet.</div></div>';
    return;
  }

  for (const item of items.slice().reverse()) {
    const el = document.createElement("div");
    el.className = "item";
    const idText = item.request_id ?? item.id ?? "n/a";
    const bodyText = item.content || JSON.stringify(item.meta || item.summary || item);
    el.innerHTML = `
      <div class="item-head">
        <span>${keyField} ${idText}</span>
        <span>${formatTimestamp(item.ts)}</span>
      </div>
      <div class="item-body">${String(bodyText).replaceAll("<", "&lt;")}</div>
    `;
    container.appendChild(el);
  }
}

async function fetchStatus() {
  const res = await fetch("/api/status");
  const data = await res.json();

  agentLock.textContent = data.agent_lock || "No lock file";
  lastRequest.textContent = data.last_outbox?.request_id ?? "None";
  lastTs.textContent = data.last_outbox?.ts ? `Updated ${formatTimestamp(data.last_outbox.ts)}` : "Waiting for data";
  reportContent.textContent = data.last_outbox?.content || "No agent output yet.";
  renderItems(inboxList, data.recent_inbox || [], "Request");
  renderItems(outboxList, data.recent_outbox || [], "Reply");
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

async function queueHealth() {
  setLog("Queueing deterministic health check...");
  const data = await postJson("/api/health", {});
  setLog(`Queued health check #${data.msg.id}. Waiting for agent...`);
  await fetchStatus();
}

async function queueAsk() {
  const text = promptInput.value.trim();
  if (!text) {
    setLog("Enter a prompt first.");
    return;
  }
  setLog("Queueing Gemini request...");
  const data = await postJson("/api/ask", { text });
  setLog(`Queued Gemini request #${data.msg.id}. Waiting for agent...`);
  await fetchStatus();
}

document.getElementById("health-btn").addEventListener("click", () => queueHealth().catch((err) => setLog(err.message)));
document.getElementById("ask-btn").addEventListener("click", () => queueAsk().catch((err) => setLog(err.message)));
document.getElementById("refresh-btn").addEventListener("click", () => fetchStatus().catch((err) => setLog(err.message)));

fetchStatus().catch((err) => setLog(err.message));
setInterval(() => {
  fetchStatus().catch(() => {});
}, 2500);
