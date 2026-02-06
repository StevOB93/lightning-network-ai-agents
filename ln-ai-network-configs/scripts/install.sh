#!/usr/bin/env bash
set -euo pipefail

############################################
# lightning-network-ai-agents
# install.sh
#
# Host dependency installer & environment validator
# Safe to re-run. Does NOT start any services.
############################################

echo "▶ Installing dependencies for lightning-network-ai-agents"

# ---- Helpers -------------------------------------------------

require_cmd() {
  if ! command -v "$1" >/dev/null 2>&1; then
    return 1
  fi
  return 0
}

header() {
  echo
  echo "▶ $1"
}

# ---- Platform checks ----------------------------------------

header "Detecting platform"

if [[ "$(uname -s)" != "Linux" ]]; then
  echo "✖ This project requires Linux (WSL2 supported)"
  exit 1
fi

if grep -qi microsoft /proc/version; then
  echo "✔ WSL detected"
else
  echo "✔ Native Linux detected"
fi

# ---- Package manager ----------------------------------------

header "Checking package manager"

if require_cmd apt-get; then
  PKG_MGR="apt-get"
else
  echo "✖ Unsupported package manager (apt-get required)"
  exit 1
fi

# ---- Base system dependencies --------------------------------

header "Installing base dependencies"

sudo apt-get update -y

sudo apt-get install -y \
  curl \
  wget \
  jq \
  bc \
  lsof \
  netcat-openbsd \
  procps \
  ca-certificates \
  gnupg \
  software-properties-common

# ---- Bitcoin Core --------------------------------------------

header "Checking Bitcoin Core"

if ! require_cmd bitcoind; then
  echo "▶ Installing Bitcoin Core"
  sudo add-apt-repository -y ppa:bitcoin/bitcoin
  sudo apt-get update -y
  sudo apt-get install -y bitcoind bitcoin-cli
else
  echo "✔ Bitcoin Core already installed"
fi

# ---- Core Lightning ------------------------------------------

header "Checking Core Lightning"

if ! require_cmd lightningd; then
  echo "▶ Installing Core Lightning"
  sudo apt-get install -y lightningd
else
  echo "✔ Core Lightning already installed"
fi

# ---- Node.js (future AI / tooling support) -------------------

header "Checking Node.js (optional)"

if ! require_cmd node; then
  echo "▶ Installing Node.js LTS"
  curl -fsSL https://deb.nodesource.com/setup_lts.x | sudo -E bash -
  sudo apt-get install -y nodejs
else
  echo "✔ Node.js already installed"
fi

# ---- Python (future AI / orchestration support) --------------

header "Checking Python"

if ! require_cmd python3; then
  echo "▶ Installing Python 3"
  sudo apt-get install -y python3 python3-pip
else
  echo "✔ Python 3 already installed"
fi

# ---- Directory sanity ----------------------------------------

header "Validating repository structure"

REQUIRED_DIRS=(
  "scripts"
  "config"
  "runtime"
  "ai"
  "web"
  "external"
  "sandbox"
  "docs"
)

for dir in "${REQUIRED_DIRS[@]}"; do
  if [[ ! -d "$dir" ]]; then
    echo "✖ Missing directory: $dir"
    echo "  Run init-repo-structure.ps1 before install.sh"
    exit 1
  fi
done

echo "✔ Repository structure validated"

# ---- Permissions ---------------------------------------------

header "Setting script permissions"

chmod +x scripts/*.sh || true
chmod +x scripts/lib/*.sh || true

# ---- Final ---------------------------------------------------

echo
echo "✅ install.sh completed successfully"
echo
echo "Next steps:"
echo "  - Review config/network.defaults.yml (when created)"
echo "  - Run ./scripts/start.sh to launch managed nodes"
echo
