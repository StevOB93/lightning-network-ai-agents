#!/usr/bin/env bash
set -euo pipefail

############################################################
# pull_ollama_model.sh — Download an Ollama model
#
# Presents a menu of recommended models and pulls the chosen
# one. Also updates OLLAMA_MODEL in ln-ai-network/.env.
#
# Usage:
#   bash scripts/tools/pull_ollama_model.sh
############################################################

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
ENV_FILE="$PROJECT_ROOT/.env"

############################################################
# Helper: write/update a key in .env
############################################################
_set_env() {
  local key="$1"
  local val="$2"
  if [[ ! -f "$ENV_FILE" ]]; then touch "$ENV_FILE"; fi
  if grep -qE "^${key}=" "$ENV_FILE" 2>/dev/null; then
    sed -i "s|^${key}=.*|${key}=${val}|" "$ENV_FILE"
  else
    echo "${key}=${val}" >> "$ENV_FILE"
  fi
}

############################################################
# Check Ollama is installed
############################################################
if ! command -v ollama >/dev/null 2>&1; then
  echo "[ERROR] Ollama is not installed. Run setup_llm.sh or install from https://ollama.com"
  exit 1
fi

############################################################
# Model menu
# Format: "tag|display name|VRAM/RAM|speed|notes"
############################################################
echo "=================================================="
echo " Ollama Model Downloader"
echo "=================================================="
echo
echo "Recommended models (choose based on your hardware):"
echo
echo "  Fast / low resource:"
echo "    1) llama3.2:3b     — 2 GB    — Very fast, good for basic tasks"
echo "    2) qwen2.5:3b      — 2 GB    — Fast, strong multilingual support"
echo "    3) phi4-mini:3.8b  — 3 GB    — Fast, good reasoning for size"
echo
echo "  Balanced:"
echo "    4) llama3.1:8b     — 5 GB    — Great general-purpose model"
echo "    5) qwen2.5:7b      — 5 GB    — Strong coding + reasoning"
echo "    6) mistral:7b      — 5 GB    — Fast, good instruction following"
echo "    7) gemma3:9b       — 6 GB    — Good reasoning, Google-trained"
echo
echo "  High quality (requires 12+ GB RAM):"
echo "    8) llama3.1:70b    — 40 GB   — Near-GPT-4 quality (quantized)"
echo "    9) qwen2.5:14b     — 9 GB    — Excellent coding + math"
echo "   10) deepseek-r1:14b — 9 GB    — Strong chain-of-thought reasoning"
echo
echo "   11) Enter custom model tag"
echo

read -rp "Enter choice [1-11] (default: 4): " CHOICE
CHOICE="${CHOICE:-4}"

case "$CHOICE" in
  1)  MODEL="llama3.2:3b"      ;;
  2)  MODEL="qwen2.5:3b"       ;;
  3)  MODEL="phi4-mini:3.8b"   ;;
  4)  MODEL="llama3.1:8b"      ;;
  5)  MODEL="qwen2.5:7b"       ;;
  6)  MODEL="mistral:7b"       ;;
  7)  MODEL="gemma3:9b"        ;;
  8)  MODEL="llama3.1:70b"     ;;
  9)  MODEL="qwen2.5:14b"      ;;
  10) MODEL="deepseek-r1:14b"  ;;
  11)
    read -rp "Enter Ollama model tag (e.g. llama3.2:3b): " MODEL
    if [[ -z "$MODEL" ]]; then
      echo "[ERROR] No model tag entered. Exiting."
      exit 1
    fi
    ;;
  *)
    echo "[WARN] Unrecognized choice. Using llama3.1:8b."
    MODEL="llama3.1:8b"
    ;;
esac

echo
echo "[INFO] Pulling $MODEL ..."
echo "[INFO] This may take a while depending on your internet speed and model size."
echo

# Start Ollama server in background if not already running
OLLAMA_BASE_URL="${OLLAMA_BASE_URL:-http://127.0.0.1:11434}"
if ! curl -sf "${OLLAMA_BASE_URL}/" >/dev/null 2>&1; then
  echo "[INFO] Starting Ollama server in background..."
  ollama serve &
  # shellcheck disable=SC2034
  OLLAMA_PID=$!
  # Wait for it to be ready (up to 15s)
  for _ in $(seq 1 15); do
    sleep 1
    if curl -sf "${OLLAMA_BASE_URL}/" >/dev/null 2>&1; then
      break
    fi
  done
fi

ollama pull "$MODEL"

echo
echo "[INFO] $MODEL downloaded successfully."

_set_env "OLLAMA_MODEL" "$MODEL"
echo "[INFO] OLLAMA_MODEL=$MODEL saved to .env"
echo "=================================================="
