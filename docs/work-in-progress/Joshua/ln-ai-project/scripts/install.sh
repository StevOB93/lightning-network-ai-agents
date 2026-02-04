#!/usr/bin/env bash
###############################################################################
# Core Lightning AI Project — FINAL, VERIFIED INSTALLER
#
# PURPOSE
# -------
# Installs infrastructure dependencies for the Core Lightning AI Project:
#   - Bitcoin Core (binary, SHA256 verified)
#   - Core Lightning (source build, official method)
#
# DESIGN GOALS
# ------------
# - Safe to re-run (idempotent unless --repair is specified)
# - Never run as root
# - Explicit dependency installation
# - Robust Core Lightning PATH handling
###############################################################################

set -e -o pipefail

echo ">>> Core Lightning installer starting"

###############################################################################
# SAFETY: NEVER RUN AS ROOT
###############################################################################
if [ "$(id -u)" = "0" ]; then
  echo "ERROR: Do NOT run this installer with sudo."
  echo "Run as a normal user; sudo is used internally when required."
  exit 1
fi

###############################################################################
# CONFIGURATION
###############################################################################
PROJECT_DIR="$HOME/LN_AI_Project"
LOG_DIR="$PROJECT_DIR/logs"
LOG_FILE="$LOG_DIR/bootstrap.log"

BITCOIN_VERSION="26.1"
CLN_VERSION="v25.09"

BITCOIND_BIN="$PROJECT_DIR/bin/bitcoind"
CLN_PREFIX="$PROJECT_DIR/opt/cln"

###############################################################################
# FLAGS
###############################################################################
REPAIR=false
for arg in "$@"; do
  if [ "$arg" = "--repair" ]; then
    REPAIR=true
  fi
done

###############################################################################
# LOGGING
###############################################################################
mkdir -p "$LOG_DIR"
exec > >(tee -a "$LOG_FILE") 2>&1

echo "[INFO] Installer initialized"

###############################################################################
# OWNERSHIP CHECK
###############################################################################
if [ -d "$PROJECT_DIR" ]; then
  if find "$PROJECT_DIR" ! -user "$USER" | grep -q .; then
    echo "ERROR: Project directory contains root-owned files."
    echo "Fix with:"
    echo "  sudo chown -R $USER:$USER $PROJECT_DIR"
    exit 1
  fi
fi

###############################################################################
# DIRECTORY LAYOUT (PROJECT STRUCTURE)
###############################################################################
echo "[INFO] Ensuring project directory structure"

mkdir -p \
  "$PROJECT_DIR/bin" \
  "$PROJECT_DIR/opt" \
  "$PROJECT_DIR/logs" \
  "$PROJECT_DIR/scripts" \
  "$PROJECT_DIR/agents" \
  "$PROJECT_DIR/controllers" \
  "$PROJECT_DIR/data/bitcoind" \
  "$PROJECT_DIR/data/cln1" \
  "$PROJECT_DIR/data/cln2" \
  "$PROJECT_DIR/venv"

###############################################################################
# SYSTEM DEPENDENCIES (OFFICIAL CLN SET)
###############################################################################
echo "[INFO] Installing system dependencies"

sudo apt update
sudo apt install -y \
  autoconf automake libtool pkg-config \
  build-essential git \
  libevent-dev zlib1g-dev \
  libsqlite3-dev libpq-dev \
  libssl-dev gettext \
  protobuf-compiler libprotobuf-dev \
  python3 python3-pip python3-setuptools python3-mako \
  jq curl wget tar xz-utils

###############################################################################
# BITCOIN CORE (BINARY INSTALL, VERIFIED)
###############################################################################
if [ -x "$BITCOIND_BIN" ] && [ "$REPAIR" = false ]; then
  echo "[INFO] Bitcoin Core already installed"
else
  echo "[INFO] Installing Bitcoin Core $BITCOIN_VERSION"

  cd /tmp
  rm -rf bitcoin-* SHA256SUMS*

  wget "https://bitcoincore.org/bin/bitcoin-core-${BITCOIN_VERSION}/bitcoin-${BITCOIN_VERSION}-x86_64-linux-gnu.tar.gz"
  wget "https://bitcoincore.org/bin/bitcoin-core-${BITCOIN_VERSION}/SHA256SUMS"

  grep "bitcoin-${BITCOIN_VERSION}-x86_64-linux-gnu.tar.gz" SHA256SUMS | sha256sum -c -

  tar -xzf "bitcoin-${BITCOIN_VERSION}-x86_64-linux-gnu.tar.gz"
  cp bitcoin-${BITCOIN_VERSION}/bin/* "$PROJECT_DIR/bin/"
fi

###############################################################################
# CORE LIGHTNING (SOURCE BUILD — OFFICIAL METHOD)
###############################################################################
if [ -x "$CLN_PREFIX/bin/lightningd" ] || \
   [ -x "$CLN_PREFIX/usr/bin/lightningd" ]; then
  if [ "$REPAIR" = false ]; then
    echo "[INFO] Core Lightning already installed"
  fi
fi

if [ "$REPAIR" = true ] || \
   [ ! -x "$CLN_PREFIX/bin/lightningd" ] && \
   [ ! -x "$CLN_PREFIX/usr/bin/lightningd" ]; then

  echo "[INFO] Building Core Lightning $CLN_VERSION from source"

  cd /tmp
  rm -rf lightning
  git clone https://github.com/ElementsProject/lightning.git
  cd lightning

  git checkout "$CLN_VERSION"
  git submodule update --init --recursive

  ./configure --prefix="$CLN_PREFIX"
  make -j"$(nproc)"
  make install
fi

###############################################################################
# CORE LIGHTNING PATH (ROBUST)
###############################################################################
PROFILE="$HOME/.bashrc"

if [ -x "$CLN_PREFIX/bin/lightningd" ]; then
  CLN_BIN_DIR="$CLN_PREFIX/bin"
elif [ -x "$CLN_PREFIX/usr/bin/lightningd" ]; then
  CLN_BIN_DIR="$CLN_PREFIX/usr/bin"
else
  echo "ERROR: Core Lightning build completed but lightningd not found"
  exit 1
fi

grep -qxF "export PATH=\$PATH:$PROJECT_DIR/bin" "$PROFILE" || \
  echo "export PATH=\$PATH:$PROJECT_DIR/bin" >> "$PROFILE"

grep -qxF "export PATH=\$PATH:$CLN_BIN_DIR" "$PROFILE" || \
  echo "export PATH=\$PATH:$CLN_BIN_DIR" >> "$PROFILE"

###############################################################################
# BITCOIN REGTEST CONFIG
###############################################################################
CONF="$PROJECT_DIR/data/bitcoind/bitcoin.conf"
if [ ! -f "$CONF" ]; then
  echo "regtest=1" > "$CONF"
  echo "server=1" >> "$CONF"
  echo "txindex=1" >> "$CONF"
  echo "fallbackfee=0.0001" >> "$CONF"
fi

###############################################################################
# DONE
###############################################################################
echo "[SUCCESS] Installer completed successfully"
echo "Run: source ~/.bashrc"
echo "Verify: bitcoind --version && lightningd --version"
