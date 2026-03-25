#!/usr/bin/env bash
set -euo pipefail

############################################################
# LN_AI_Project :: 0.install.sh
# ----------------------------------------------------------
# Guarantees after success:
#   - Bitcoin Core installed (official binaries)
#   - Core Lightning installed (from source)
#   - All known CLN build dependencies satisfied
#   - Project venv created + Python deps installed (requirements.txt)
#   - Baseline runtime directories exist
#
# Explicitly NOT done here:
#   - Core Lightning version pinning
############################################################

echo "=================================================="
echo " LN_AI_Project :: 0.install.sh"
echo "=================================================="

############################################################
# Resolve paths
############################################################
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

echo "[INFO] Project root: $PROJECT_ROOT"

############################################################
# Normalize shell scripts (permissions + CRLF) — scripts only
############################################################
echo "[INFO] Normalizing shell scripts under scripts/ ..."
find "$PROJECT_ROOT/scripts" -type f -name "*.sh" -exec chmod +x {} \; || true
find "$PROJECT_ROOT/scripts" -type f -name "*.sh" -exec sed -i 's/\r$//' {} \; || true

############################################################
# System update
############################################################
echo "[INFO] Updating system..."
sudo apt update

############################################################
# System dependencies (FULL ANTICIPATED SET)
############################################################
echo "[INFO] Installing system dependencies..."
sudo apt install -y \
  curl \
  wget \
  ca-certificates \
  gnupg \
  tar \
  jq \
  git \
  build-essential \
  pkg-config \
  libtool \
  autoconf \
  automake \
  libsqlite3-dev \
  libssl-dev \
  libsodium-dev \
  lowdown \
  python3 \
  python3-pip \
  python3-venv \
  python3-mako \
  python3-dev \
  libffi-dev \
  gettext \
  libzmq3-dev \
  net-tools

############################################################
# Bitcoin Core (official binaries)
############################################################
BITCOIN_VERSION="26.0"
BITCOIN_URL="https://bitcoincore.org/bin/bitcoin-core-${BITCOIN_VERSION}/bitcoin-${BITCOIN_VERSION}-x86_64-linux-gnu.tar.gz"

if command -v bitcoind >/dev/null 2>&1; then
  echo "[INFO] Bitcoin Core already installed."
else
  echo "[INFO] Installing Bitcoin Core ${BITCOIN_VERSION}..."

  TMP_DIR="$(mktemp -d)"
  cd "$TMP_DIR"

  curl -fLO "$BITCOIN_URL"
  tar -xzf "bitcoin-${BITCOIN_VERSION}-x86_64-linux-gnu.tar.gz"

  sudo install -m 0755 -o root -g root \
    "bitcoin-${BITCOIN_VERSION}/bin/"* /usr/local/bin/

  cd /
  rm -rf "$TMP_DIR"
fi

############################################################
# Core Lightning (build from source, UNPINNED)
############################################################
if command -v lightningd >/dev/null 2>&1; then
  echo "[INFO] Core Lightning already installed."
else
  echo "[INFO] Installing Core Lightning from source..."

  TMP_DIR="$(mktemp -d)"
  cd "$TMP_DIR"

  git clone https://github.com/ElementsProject/lightning.git
  cd lightning

  ./configure
  make -j"$(nproc)"
  sudo make install

  cd /
  rm -rf "$TMP_DIR"
fi

############################################################
# Verify binaries (HARD CONTRACT)
############################################################
echo "[INFO] Verifying installed binaries..."

command -v bitcoind >/dev/null
command -v bitcoin-cli >/dev/null
command -v lightningd >/dev/null
command -v lightning-cli >/dev/null

bitcoind --version
lightningd --version

############################################################
# Project directory baseline
############################################################
echo "[INFO] Creating project directories..."

mkdir -p "$PROJECT_ROOT/runtime/bitcoin"
mkdir -p "$PROJECT_ROOT/runtime/lightning"
mkdir -p "$PROJECT_ROOT/logs"
mkdir -p "$PROJECT_ROOT/temp"

############################################################
# Python venv + deps (one-time)
############################################################
VENV_DIR="$PROJECT_ROOT/.venv"
VENV_PY="$VENV_DIR/bin/python"

if [[ ! -d "$VENV_DIR" ]]; then
  echo "[INFO] Creating Python venv at $VENV_DIR ..."
  python3 -m venv "$VENV_DIR"
fi

if [[ ! -x "$VENV_PY" ]]; then
  echo "[FATAL] venv python not found/executable at $VENV_PY"
  exit 127
fi

if [[ -f "$PROJECT_ROOT/requirements.txt" ]]; then
  echo "[INFO] Installing Python dependencies from requirements.txt ..."
  "$VENV_PY" -m pip install --quiet --upgrade pip
  "$VENV_PY" -m pip install --quiet -r "$PROJECT_ROOT/requirements.txt"
else
  echo "[WARN] requirements.txt not found at $PROJECT_ROOT/requirements.txt; skipping pip install."
fi

############################################################
# WSL stability notes (non-fatal)
############################################################
if grep -qi microsoft /proc/version; then
  echo "[INFO] WSL detected."
  echo "[INFO] Consider increasing inotify watches if scaling nodes:"
  echo "       fs.inotify.max_user_watches=524288"
fi

############################################################
# LLM backend setup (interactive prompt)
############################################################
SETUP_LLM="$SCRIPT_DIR/tools/setup_llm.sh"
if [[ -x "$SETUP_LLM" ]]; then
  echo
  bash "$SETUP_LLM"
else
  echo "[WARN] $SETUP_LLM not found or not executable — skipping LLM setup."
  echo "[INFO] Run scripts/tools/setup_llm.sh later to configure your LLM backend."
fi

############################################################
# Done
############################################################
echo "=================================================="
echo " Install complete ✔"
echo
echo " Next steps:"
REPO_ROOT="$(cd "$PROJECT_ROOT/.." && pwd)"
echo "   source $PROJECT_ROOT/env.sh   # load env vars"
echo "   cd $REPO_ROOT"
echo "   ./start.sh 2"
echo "   ./ln-ai-network/scripts/network_test.sh"
echo "   ./stop.sh 2"
echo "=================================================="