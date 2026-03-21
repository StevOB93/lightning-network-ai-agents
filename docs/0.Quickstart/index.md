---
title: Quickstart
---

# Quickstart

Get the Lightning Network AI agent running on your machine in minutes.

## Prerequisites

- Linux or WSL2 (Ubuntu recommended)
- `git`
- Python 3.10+
- An LLM API key (OpenAI / Gemini) **or** a local [Ollama](https://ollama.com) install

## Three steps

### 1 — Install (once)

```bash
./install.sh
```

Installs Bitcoin Core, Core Lightning, Python virtual environment, and all Python dependencies.

### 2 — Configure your LLM

```bash
cp ln-ai-network/.env.example ln-ai-network/.env
```

Edit `ln-ai-network/.env`:

| Backend | Settings |
|---------|----------|
| OpenAI (default) | `OPENAI_API_KEY=sk-…` and `ALLOW_LLM=1` |
| Ollama (local, free) | `LLM_BACKEND=ollama` and `ALLOW_LLM=1` |
| Gemini | `LLM_BACKEND=gemini`, `GEMINI_API_KEY=…`, and `ALLOW_LLM=1` |

### 3 — Start

```bash
./start.sh          # 2 Lightning nodes (default)
./start.sh 3        # or 3 nodes
```

The web UI opens automatically at `http://127.0.0.1:8008`.

To stop everything:

```bash
./stop.sh
```

## First prompt

In the web UI, type a prompt and press **Enter**:

```
Check the network health and report the status of all nodes.
```

```
Open a 500,000 sat channel from node 1 to node 2.
```

```
Have node 2 create an invoice for 10,000 msat and pay it from node 1.
```

Watch the Pipeline tab for live stage-by-stage progress.

## More detail

- [Detailed quickstart with E2E payment test](Quickstart.md)
- [Setup guide](../1.Setup/)
- [Architecture overview](../2.Architecture/)
- [Troubleshooting](../1.Setup/TROUBLESHOOTING.md)
