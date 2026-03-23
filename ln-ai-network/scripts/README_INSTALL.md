# Installation — `0.install.sh`

`scripts/0.install.sh` is a **one-time** setup script. Run it once on a fresh machine (or WSL instance) before starting the system for the first time.

---

## Design principles

- **Deterministic** — same result on every machine; no environment-specific branches
- **No PPAs / no Snap** — only official upstream binaries and apt packages
- **Explicit dependencies** — nothing installed "by accident"
- **Safe to rerun** — all steps are idempotent; running twice causes no harm
- **WSL-friendly** — avoids common Windows/Linux pitfalls (systemd, snap, PATH issues)

---

## What gets installed and why

### Base utilities

Required for downloads, archive extraction, JSON parsing, and secure networking.

| Package | Why |
|---------|-----|
| `curl`, `wget` | Download Bitcoin Core binaries and source tarballs |
| `ca-certificates`, `gnupg` | Verify download signatures |
| `tar` | Extract tarballs |
| `jq` | Parse JSON in shell scripts |
| `git` | Clone Core Lightning source |

---

### Build toolchain

Required to compile Core Lightning from source.

| Package | Why |
|---------|-----|
| `build-essential` | C/C++ compiler, `make`, `ld` |
| `pkg-config` | Locate installed libraries during build |
| `autoconf`, `automake`, `libtool` | Build system generators |

---

### Cryptography and storage libraries

Required by Core Lightning.

| Package | Why |
|---------|-----|
| `libsodium-dev` | Noise protocol, cryptographic primitives |
| `libssl-dev` | TLS and additional cryptographic operations |
| `libsqlite3-dev` | Wallet and HTLC state storage |

---

### Documentation and build-time tools

Required for a successful Core Lightning build.

| Package | Why |
|---------|-----|
| `lowdown` | Generates man pages during `make install` |
| `gettext` | Internationalisation tooling used by the build |
| `python3-mako` | Template generation for build scripts |

---

### Python runtime

Required for the AI pipeline, MCP server, and web UI.

| Package | Why |
|---------|-----|
| `python3`, `python3-pip` | Python interpreter and package manager |
| `python3-venv` | Creates the isolated `.venv` used by all project code |

The project uses a virtual environment at `.venv/` so that no system Python packages are modified. All project dependencies are in `requirements.txt` and installed into the venv.

---

### Networking and event infrastructure

| Package | Why |
|---------|-----|
| `net-tools` | `netstat`, `ifconfig` for debugging connectivity |
| `libzmq3-dev` | ZeroMQ support (future event-driven workflow hooks) |

---

## Bitcoin Core

Installed from **official upstream binaries** at `bitcoincore.org`.

**Why binaries instead of source?**
- Bitcoin Core has a much longer build time than Core Lightning
- Official binaries are SHA256-verified and GPG-signed
- Binary releases are consistent across platforms
- Works reliably in WSL without build-environment quirks

Installed binaries:
- `bitcoind` — full node daemon
- `bitcoin-cli` — RPC client

The installer verifies the SHA256 hash and GPG signature before installing.

---

## Core Lightning

Installed by **building from source**.

**Why source instead of binaries?**
- Prebuilt packages are inconsistent across distributions
- Source builds surface missing dependencies early rather than at runtime
- Required for plugin development and custom extensions
- Allows pinning to a specific commit for reproducibility

After the build, `lightningd` and `lightning-cli` are placed on `PATH`.

---

## Python virtual environment and dependencies

After installing system packages, the installer:

1. Creates `.venv/` using `python3 -m venv`
2. Installs all packages listed in `requirements.txt` into the venv

Key Python dependencies include:
- `fastmcp` — MCP protocol implementation
- `anthropic`, `openai`, `google-generativeai` — LLM provider SDKs
- `requests` — HTTP client for Ollama
- `pytest` — test runner

---

## Filesystem layout created

The installer guarantees these directories exist before `1.start.sh` runs:

```
runtime/
  bitcoin/
  lightning/
logs/
```

These are contracts — all runtime scripts assume they exist. `1.start.sh` creates additional subdirectories (`runtime/agent/`, `runtime/bitcoin/shared/`, `runtime/lightning/node-N/`) when the system first starts.

---

## What `0.install.sh` does NOT do

- Start any processes (Bitcoin, Lightning, agent, UI)
- Create node-specific runtime state or wallets
- Configure `.env` or set LLM credentials
- Set up channels or fund wallets

Those responsibilities belong to `1.start.sh` and the startup subscripts.

---

## After installation

```bash
# 1. Configure your LLM provider
cp .env.example .env
# Edit .env: set LLM_BACKEND and the corresponding API key

# 2. Start the full system
./scripts/1.start.sh 2

# 3. Open the web UI
# → http://127.0.0.1:8008
```
