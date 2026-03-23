# Quickstart — Detailed (LN AI Network, Regtest)

This guide covers installation, configuration, and running an end-to-end payment test. For the short version, see [the quickstart index](index.md).

> Core rule: the AI agent **acts only via MCP tools**. The MCP server is the execution boundary between the LLM and the infrastructure.

---

## Prerequisites

- Linux or WSL2 (Ubuntu 22.04+ recommended)
- Python 3.10+
- `git`
- An LLM API key (OpenAI / Gemini) **or** a local [Ollama](https://ollama.com) install

---

## 1. Clone and install

```bash
git clone <YOUR_REPO_URL> lightning-network-ai-agents
cd lightning-network-ai-agents

# One-time install: Bitcoin Core, Core Lightning, Python venv, dependencies
./install.sh
```

The install log is written to `ln-ai-network/logs/install.log`.

---

## 2. Configure

```bash
cp ln-ai-network/.env.example ln-ai-network/.env
```

Edit `ln-ai-network/.env` and set at minimum:

```bash
ALLOW_LLM=1
```

And your LLM credentials:

| Backend | Settings |
|---------|----------|
| OpenAI (default) | `OPENAI_API_KEY=sk-…` |
| Ollama (local) | `LLM_BACKEND=ollama`, `OLLAMA_MODEL=llama3.2` |
| Gemini | `LLM_BACKEND=gemini`, `GEMINI_API_KEY=…` |

Recommended for deterministic tool-calling:

```bash
LLM_TEMPERATURE=0
```

---

## 3. Start the system

```bash
./start.sh          # 2 Lightning nodes (default)
./start.sh 3        # or 3 nodes
```

The system starts all components in sequence:
1. Bitcoin Core (regtest)
2. Core Lightning nodes (funded, connected, channels opened)
3. MCP tool server
4. AI agent pipeline
5. Web UI (auto-opens at `http://127.0.0.1:8008`)

Full startup typically takes 30–60 seconds on first run (mining 101 + 6 blocks).

---

## 4. Smoke test

In the web UI, type a prompt and press **Enter**:

```
Check the network health and tell me the status of all nodes.
```

Watch the Pipeline tab — you should see:
- Translator card: parsed goal and intent
- Planner card: 1–2 tool steps
- Executor card: `network_health` result
- Summary card: agent's answer

---

## 5. End-to-end payment test

Try the full workflow:

```
Have node 2 create an invoice for 10,000 msat, then pay it from node 1. Verify the payment succeeded.
```

The agent will call the full golden path:
1. `network_health` — check infrastructure
2. `ln_node_status` + `ln_node_start` — ensure node 2 is running
3. `ln_getinfo(node=2)` — get pubkey and address
4. `ln_connect` — connect node 1 to node 2
5. `btc_wallet_ensure` + `ln_newaddr` × 2 + `btc_sendtoaddress` × 2 + mine blocks — fund nodes
6. `ln_openchannel` + mine blocks — open and confirm channel
7. `ln_invoice(node=2, amount_msat=10000, ...)` — create invoice
8. `ln_pay(from_node=1, bolt11=...)` — pay it
9. `ln_listfunds(node=2)` — verify balance increased

The Summary card shows the final answer and a ✓/✗ success indicator.

---

## 6. Stop the system

```bash
./stop.sh
```

Shutdown runs in reverse order: agent → MCP server → Lightning nodes → Bitcoin Core → UI server.

---

## Debugging

**Trace log** — every tool call, LLM response, and parse event:
```bash
tail -n 120 ln-ai-network/runtime/agent/trace.log
```

**Last pipeline result:**
```bash
tail -n 1 ln-ai-network/runtime/agent/outbox.jsonl
```

**Agent boot log:**
```bash
tail -n 50 ln-ai-network/logs/system/0.3.agent_boot.log
```

Or use the **Copy Crash Kit** button in the Logs tab of the web UI.

See [Troubleshooting](../1.Setup/TROUBLESHOOTING.md) for common failure modes.

---

## Restart agent only (after code changes)

```bash
cd ln-ai-network
./scripts/restart_agent.sh          # keep inbox/outbox
./scripts/restart_agent.sh fresh    # clear queue state
```

This restarts only the AI pipeline — Bitcoin and Lightning keep running.
