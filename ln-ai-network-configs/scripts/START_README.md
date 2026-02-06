# LN AI Project — start.sh Overview

This document explains what `start.sh` does and what it assumes.

---

## Purpose

`start.sh` is responsible for **starting runtime processes**, not installing software.

Specifically, it:

- Starts a regtest Bitcoin node
- Starts a regtest Core Lightning node
- Uses deterministic runtime directories
- Avoids hardcoded global state

---

## Assumptions

Before running `start.sh`, you must have:

- Successfully run `install.sh`
- Bitcoin Core installed
- Core Lightning installed
- runtime/, logs/, and temp/ directories present

`start.sh` does **not** verify or install dependencies.

---

## Runtime Layout

When run, `start.sh` creates:

runtime/
  bitcoin/
    node-1/
  lightning/
    node-1/

This layout is intentionally structured so future versions can scale to N nodes.

---

## Behavior

- If a process is already running, it is not restarted
- Processes are launched in daemon mode
- regtest is always used

---

## What start.sh Does NOT Do

- Fund wallets
- Create channels
- Connect nodes
- Stop running processes

Those actions are handled by separate scripts such as funding or shutdown helpers.

---

## Next Steps

After running `start.sh`, typical next actions include:

- Mining blocks on regtest
- Funding the Lightning wallet
- Opening channels
- Running test or orchestration scripts

---

## Summary

`start.sh` is intentionally minimal.

Its job is to:
- Bring the system to a known-running state
- Establish a clean runtime baseline
- Stay predictable and scalable