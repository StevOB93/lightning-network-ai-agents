# LN AI Project — Installation Overview

This document explains **everything installed by `install.sh` and why**. The goal is that a new contributor can clone the repo, run the installer once, and never have to guess about missing dependencies.

---

## Design Principles

- **Deterministic installs** — same result on every machine
- **No PPAs / no Snap** — avoid brittle external repos
- **Explicit dependencies** — nothing installed “by accident”
- **Safe to rerun** — installer can be executed multiple times
- **WSL-friendly** — avoids common Windows/Linux pitfalls

---

## What Gets Installed and Why

### Base Utilities
Installed to support downloads, archives, JSON parsing, and secure networking.

- curl, wget
- ca-certificates, gnupg
- tar
- jq
- git

---

### Build Toolchain
Required to compile Core Lightning from source.

- build-essential
- pkg-config
- autoconf
- automake
- libtool

---

### Cryptography & Storage Libraries
Required by Core Lightning.

- libsodium-dev — cryptography & Noise protocol
- libssl-dev — cryptographic primitives
- libsqlite3-dev — wallet and state storage

---

### Documentation & Build-Time Tools
Required for a successful Core Lightning build.

- lowdown — generates man pages
- gettext — localization tooling
- python3-mako — template generation

---

### Python Runtime
Required for plugins and future AI orchestration.

- python3
- python3-pip
- python3-venv

---

### Networking & Event Infrastructure
Supports debugging and future orchestration.

- net-tools
- libzmq3-dev — ZeroMQ support for event-driven workflows

---

## Bitcoin Core

Installed using **official upstream binaries** from bitcoincore.org.

Why:
- Matches upstream exactly
- Avoids broken or outdated PPAs
- Works consistently in WSL

Installed binaries:
- bitcoind
- bitcoin-cli

---

## Core Lightning

Installed by **building from source**.

Why:
- Prebuilt binaries are inconsistent
- Source builds surface missing dependencies early
- Maximum flexibility for plugins and extensions

Core Lightning is intentionally **not pinned** to a specific version yet. Pinning will be introduced later once the system stabilizes.

---

## Filesystem Layout Created

The installer guarantees the following directories exist:

runtime/
  bitcoin/
  lightning/

logs/
temp/

These directories are contracts used by all runtime scripts.

---

## What install.sh Does NOT Do

- Start Bitcoin or Lightning processes
- Create node-specific runtime state
- Configure regtest or ports
- Perform funding or channel setup

Those responsibilities belong to `start.sh` and higher-level scripts.

---

## Summary

After running `install.sh`, the system guarantees:

- Bitcoin Core is installed and callable
- Core Lightning is installed and callable
- All required dependencies are present
- Directory layout is prepared
- No manual fixes are required

This creates a stable base for multi-node Lightning experiments and AI-driven orchestration.
