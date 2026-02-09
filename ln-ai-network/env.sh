#!/usr/bin/env bash

############################################
# Project root (MUST match actual repo)
############################################

export LN_ROOT="$HOME/lightning-network-ai-agents/ln-ai-network"

############################################
# Derived paths
############################################

export LN_RUNTIME="$LN_ROOT/runtime"
export LN_LOGS="$LN_ROOT/logs"
export LN_SCRIPTS="$LN_ROOT/scripts"

############################################
# Bitcoin Core
############################################

export BITCOIND="/usr/local/bin/bitcoind"
export BITCOIN_CLI="/usr/local/bin/bitcoin-cli"

############################################
# Core Lightning
############################################

export LIGHTNINGD="/usr/local/bin/lightningd"
export LIGHTNING_CLI="/usr/local/bin/lightning-cli"
