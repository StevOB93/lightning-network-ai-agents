---
title: Bitcoin Core + Core Lightning
---

# Bitcoin Core + Core Lightning

The harness requires two binaries on `PATH`: `bitcoind` (Bitcoin Core) and `lightningd` (Core Lightning). `./install.sh` handles this automatically on supported platforms.

## Automatic installation

`install.sh` (at the repo root) downloads and installs both binaries to `/usr/local/bin`:

```bash
./install.sh
```

After installation, verify:

```bash
bitcoind --version
lightningd --version
```

## Manual installation

If `install.sh` doesn't cover your platform, install the binaries manually.

### Bitcoin Core

```bash
# Ubuntu / Debian via apt
sudo add-apt-repository ppa:bitcoin/bitcoin
sudo apt update
sudo apt install bitcoind
```

Or download a pre-built binary from [bitcoincore.org](https://bitcoincore.org/en/download/) and place `bitcoind` and `bitcoin-cli` on `PATH`.

### Core Lightning

```bash
# Ubuntu via apt (Core Lightning PPA)
sudo add-apt-repository ppa:lightningnetwork/ppa
sudo apt update
sudo apt install lightningd
```

Or build from source: [github.com/ElementsProject/lightning](https://github.com/ElementsProject/lightning).

Verify:

```bash
lightningd --version
lightning-cli --version
```

## Regtest configuration

The harness runs entirely on `regtest` — an isolated local blockchain with no real Bitcoin. All configuration is generated automatically by `scripts/0.1.infra_boot.sh`:

- `runtime/bitcoin/shared/bitcoin.conf` — Bitcoin Core config
- `runtime/lightning/node-N/config` — per-node Core Lightning config

You do not need to edit these files manually.

## What the harness creates

When `./start.sh` runs, the infra boot script:

1. Starts `bitcoind` in daemon mode on `regtest` (RPC port 18443, P2P port 18444)
2. Creates the `miner` wallet
3. Starts one `lightningd` process per node (base port 9735, incremented per node)
4. Connects each node to every other node (full mesh)
5. Funds each node's on-chain wallet with regtest coins
6. Mines 101 blocks (coinbase maturity) + opens and confirms channels

All data lives under `ln-ai-network/runtime/` and is cleaned up by `./stop.sh`.

## Port reference

| Service | Default port | Config var |
|---------|-------------|------------|
| Bitcoin RPC | 18443 | `BITCOIN_RPC_PORT` |
| Bitcoin P2P | 18444 | `BITCOIN_P2P_PORT` |
| Lightning node 1 | 9736 | `LIGHTNING_BASE_PORT + 1` |
| Lightning node 2 | 9737 | `LIGHTNING_BASE_PORT + 2` |
| Web UI | 8008 | `UI_PORT` |
| MCP server | stdio | — |

Override any port in `ln-ai-network/.env`.
