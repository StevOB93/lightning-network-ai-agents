---
title: Setup
---

# Setup

This section covers everything needed to get the project running from scratch.

## Order of operations

1. **Install prerequisites** — OS packages, Bitcoin Core, Core Lightning, Python
2. **Configure LLM credentials** — API key or local Ollama
3. **Start the harness** — boot infrastructure, agent, and web UI

The top-level `./install.sh` handles steps 1 automatically on Ubuntu/WSL2.

## Pages

- [Bitcoin Core + Core Lightning](core-lightning.md) — binary installation and regtest overview
- [Local Harness (ln-ai-network)](ln-ai-network.md) — harness setup, `.env` config, and Python environment
- [Troubleshooting](TROUBLESHOOTING.md) — common failures and fixes

## Minimum requirements

| Requirement | Version |
|-------------|---------|
| OS | Linux or WSL2 (Ubuntu 22.04+ recommended) |
| Python | 3.10+ |
| Bitcoin Core | 25.0+ |
| Core Lightning | 23.11+ |
| RAM | 2 GB free (regtest is lightweight) |
| Disk | ~1 GB (binaries + runtime data) |

## Environment variables

All runtime configuration lives in `ln-ai-network/.env`. Copy the example file to start:

```bash
cp ln-ai-network/.env.example ln-ai-network/.env
```

The minimum required settings are:

```bash
ALLOW_LLM=1                          # must be 1 to enable LLM calls
LLM_BACKEND=openai                   # openai | ollama | gemini

# For OpenAI:
OPENAI_API_KEY=sk-...

# For Ollama (local, no API key needed):
# LLM_BACKEND=ollama
# OLLAMA_MODEL=llama3.2

# For Gemini:
# LLM_BACKEND=gemini
# GEMINI_API_KEY=...
```

See `ln-ai-network/.env.example` for the full list of tunable settings.
