---
title: Runbooks
---

# Runbooks

Practical guides for operating and debugging the system.

## Start / stop

```bash
# Start with 2 Lightning nodes (default)
./start.sh

# Start with 3 nodes
./start.sh 3

# Stop everything cleanly
./stop.sh
```

`NODE_COUNT` is persisted to `ln-ai-network/runtime/node_count` so `stop.sh` always shuts down the right number of nodes without needing the argument again.

## Restart the agent only

Use this when you change AI pipeline code without touching the Bitcoin/Lightning infrastructure:

```bash
cd ln-ai-network

# Keep inbox/outbox state
./scripts/restart_agent.sh

# Clear queue state (fresh start)
./scripts/restart_agent.sh fresh
```

## Web UI restart / shutdown

The status bar in the web UI has two buttons:

- **↺ Restart** — stops everything and starts it again (runs `stop.sh && start.sh` in the background). The page briefly disconnects and reconnects.
- **⏻ Shutdown** — stops everything and exits. The UI goes offline.

Both buttons require confirmation.

## Check system health

Use the **Health** button in the web UI (Agent tab), or send a prompt:

> "Check the network health and tell me the status of all nodes."

The agent calls `network_health()` and reports the result.

## Run an end-to-end payment test

Ask the agent:

> "Have node 2 create an invoice for 10,000 msat and pay it from node 1. Verify the payment succeeded."

The agent will call `ln_getinfo`, `ln_connect`, fund both nodes, `ln_openchannel`, `ln_invoice`, `ln_pay`, and `ln_listfunds` — the full golden path.

## Make a node reachable from another machine

Ask the agent:

> "Make node 1 reachable from another machine on the LAN."

The agent will:
1. Call `sys_netinfo()` to detect the machine's LAN IP
2. Stop node 1: `ln_node_stop(node=1)`
3. Restart with routable binding: `ln_node_start(node=1, bind_host="0.0.0.0", announce_host=<detected_ip>)`
4. Return the node's pubkey and address for the remote operator to connect to

## Logs

| File | Contents |
|------|---------|
| `ln-ai-network/runtime/agent/trace.log` | Per-prompt trace (resets each request) |
| `ln-ai-network/runtime/agent/outbox.jsonl` | All pipeline results |
| `ln-ai-network/logs/system/0.3.agent_boot.log` | Pipeline process startup log |
| `ln-ai-network/logs/system/0.4.ui_server.log` | Web UI server log |
| `ln-ai-network/logs/system/0.1.infra_boot.log` | Infrastructure boot log |
| `ln-ai-network/logs/system/shutdown.log` | Shutdown log |

The Logs tab in the web UI shows a live trace stream and an archive panel for past pipeline runs.

## Save a trace before it resets

Trace logs reset on every new prompt. To preserve a run:

```bash
mkdir -p ln-ai-network/runtime/agent/archive
cp ln-ai-network/runtime/agent/trace.log \
   ln-ai-network/runtime/agent/archive/trace.$(date +%Y%m%d_%H%M%S).log
```

## Collect a debug snapshot (Crash Kit)

In the web UI, go to the **Logs tab** and click **Copy Crash Kit**. This copies a formatted plain-text report to your clipboard containing:

- System info and configuration (non-sensitive)
- Runtime status (lock file, last request ID, queue counts)
- Last pipeline result
- Recent trace events
- Metrics

Paste this into a bug report or debugging session.

## Troubleshooting

See [Troubleshooting](../1.Setup/TROUBLESHOOTING.md) for common failure modes and fixes.
