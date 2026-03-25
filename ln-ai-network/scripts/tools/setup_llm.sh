#!/usr/bin/env bash
set -euo pipefail

############################################################
# setup_llm.sh — Interactive LLM backend configuration
#
# Prompts the user to choose an LLM provider and configures
# LLM_BACKEND (and API key if needed) in ln-ai-network/.env.
#
# Supported backends:
#   claude  — Anthropic Claude (requires ANTHROPIC_API_KEY)
#   openai  — OpenAI GPT     (requires OPENAI_API_KEY)
#   gemini  — Google Gemini  (requires GEMINI_API_KEY)
#   ollama  — Local Ollama   (no API key; installs Ollama + pulls a model)
############################################################

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
ENV_FILE="$PROJECT_ROOT/.env"
ENV_EXAMPLE="$PROJECT_ROOT/.env.example"

echo "=================================================="
echo " LLM Backend Setup"
echo "=================================================="

# Ensure .env exists (copy from .env.example if not)
if [[ ! -f "$ENV_FILE" ]]; then
  if [[ -f "$ENV_EXAMPLE" ]]; then
    echo "[INFO] Creating $ENV_FILE from .env.example ..."
    cp "$ENV_EXAMPLE" "$ENV_FILE"
  else
    echo "[WARN] Neither .env nor .env.example found. Creating minimal .env ..."
    touch "$ENV_FILE"
  fi
fi

############################################################
# Helper: read/write a key in .env
############################################################
_get_env() {
  local key="$1"
  grep -E "^${key}=" "$ENV_FILE" 2>/dev/null | tail -1 | cut -d'=' -f2- || true
}

_set_env() {
  local key="$1"
  local val="$2"
  if grep -qE "^${key}=" "$ENV_FILE" 2>/dev/null; then
    sed -i "s|^${key}=.*|${key}=${val}|" "$ENV_FILE"
  else
    echo "${key}=${val}" >> "$ENV_FILE"
  fi
}

############################################################
# Show current backend
############################################################
CURRENT_BACKEND="$(_get_env LLM_BACKEND)"
if [[ -n "$CURRENT_BACKEND" ]]; then
  echo "[INFO] Current LLM_BACKEND: $CURRENT_BACKEND"
fi

############################################################
# Prompt for backend choice
############################################################
echo
echo "Choose your default LLM backend:"
echo "  1) claude  — Anthropic Claude (recommended, cloud API)"
echo "  2) openai  — OpenAI GPT       (cloud API)"
echo "  3) gemini  — Google Gemini    (cloud API)"
echo "  4) ollama  — Local Ollama     (free, runs on your machine)"
echo
read -rp "Enter choice [1-4] (default: 1): " CHOICE
CHOICE="${CHOICE:-1}"

case "$CHOICE" in
  1|claude)  BACKEND="claude"  ;;
  2|openai)  BACKEND="openai"  ;;
  3|gemini)  BACKEND="gemini"  ;;
  4|ollama)  BACKEND="ollama"  ;;
  *)
    echo "[WARN] Unrecognized choice '$CHOICE'. Defaulting to claude."
    BACKEND="claude"
    ;;
esac

echo "[INFO] Setting LLM_BACKEND=$BACKEND ..."
_set_env "LLM_BACKEND" "$BACKEND"
_set_env "ALLOW_LLM" "1"

############################################################
# Backend-specific setup
############################################################
case "$BACKEND" in

  # --------------------------------------------------------
  claude)
    echo
    echo "Anthropic Claude requires an API key."
    echo "Get one at: https://console.anthropic.com → API Keys"
    echo
    CURRENT_KEY="$(_get_env ANTHROPIC_API_KEY)"
    if [[ -n "$CURRENT_KEY" && "$CURRENT_KEY" != "__REPLACE_WITH_REAL_KEY__" && "$CURRENT_KEY" != "__PASTE_YOUR_ANTHROPIC_KEY_HERE__" ]]; then
      echo "[INFO] ANTHROPIC_API_KEY is already set."
      read -rp "Replace it? [y/N]: " REPLACE
      if [[ "${REPLACE,,}" != "y" ]]; then
        echo "[INFO] Keeping existing key."
      else
        read -rsp "Paste your Anthropic API key: " NEW_KEY
        echo
        [[ -n "$NEW_KEY" ]] && _set_env "ANTHROPIC_API_KEY" "$NEW_KEY"
      fi
    else
      read -rsp "Paste your Anthropic API key: " NEW_KEY
      echo
      if [[ -n "$NEW_KEY" ]]; then
        _set_env "ANTHROPIC_API_KEY" "$NEW_KEY"
        echo "[INFO] ANTHROPIC_API_KEY saved to .env"
      else
        echo "[WARN] No key entered. Set ANTHROPIC_API_KEY in .env before running the agent."
      fi
    fi

    # Suggest model
    CURRENT_MODEL="$(_get_env CLAUDE_MODEL)"
    if [[ -z "$CURRENT_MODEL" || "$CURRENT_MODEL" == "__REPLACE_WITH_REAL_KEY__" ]]; then
      _set_env "CLAUDE_MODEL" "claude-opus-4-6"
    fi
    echo "[INFO] CLAUDE_MODEL=$(  _get_env CLAUDE_MODEL)"
    ;;

  # --------------------------------------------------------
  openai)
    echo
    echo "OpenAI requires an API key."
    echo "Get one at: https://platform.openai.com → API Keys"
    echo
    CURRENT_KEY="$(_get_env OPENAI_API_KEY)"
    if [[ -n "$CURRENT_KEY" && "$CURRENT_KEY" != "__REPLACE_WITH_REAL_KEY__" ]]; then
      echo "[INFO] OPENAI_API_KEY is already set."
      read -rp "Replace it? [y/N]: " REPLACE
      if [[ "${REPLACE,,}" == "y" ]]; then
        read -rsp "Paste your OpenAI API key: " NEW_KEY
        echo
        [[ -n "$NEW_KEY" ]] && _set_env "OPENAI_API_KEY" "$NEW_KEY"
      fi
    else
      read -rsp "Paste your OpenAI API key: " NEW_KEY
      echo
      if [[ -n "$NEW_KEY" ]]; then
        _set_env "OPENAI_API_KEY" "$NEW_KEY"
        echo "[INFO] OPENAI_API_KEY saved to .env"
      else
        echo "[WARN] No key entered. Set OPENAI_API_KEY in .env before running the agent."
      fi
    fi

    CURRENT_MODEL="$(_get_env OPENAI_MODEL)"
    [[ -z "$CURRENT_MODEL" ]] && _set_env "OPENAI_MODEL" "gpt-4o"
    echo "[INFO] OPENAI_MODEL=$( _get_env OPENAI_MODEL)"
    ;;

  # --------------------------------------------------------
  gemini)
    echo
    echo "Google Gemini requires an API key."
    echo "Get one at: https://aistudio.google.com → Get API Key"
    echo
    CURRENT_KEY="$(_get_env GEMINI_API_KEY)"
    if [[ -n "$CURRENT_KEY" && "$CURRENT_KEY" != "__REPLACE_WITH_REAL_KEY__" ]]; then
      echo "[INFO] GEMINI_API_KEY is already set."
      read -rp "Replace it? [y/N]: " REPLACE
      if [[ "${REPLACE,,}" == "y" ]]; then
        read -rsp "Paste your Gemini API key: " NEW_KEY
        echo
        [[ -n "$NEW_KEY" ]] && _set_env "GEMINI_API_KEY" "$NEW_KEY"
      fi
    else
      read -rsp "Paste your Gemini API key: " NEW_KEY
      echo
      if [[ -n "$NEW_KEY" ]]; then
        _set_env "GEMINI_API_KEY" "$NEW_KEY"
        echo "[INFO] GEMINI_API_KEY saved to .env"
      else
        echo "[WARN] No key entered. Set GEMINI_API_KEY in .env before running the agent."
      fi
    fi

    CURRENT_MODEL="$(_get_env GEMINI_MODEL)"
    [[ -z "$CURRENT_MODEL" ]] && _set_env "GEMINI_MODEL" "gemini-2.5-flash"
    echo "[INFO] GEMINI_MODEL=$( _get_env GEMINI_MODEL)"
    ;;

  # --------------------------------------------------------
  ollama)
    echo
    echo "Ollama runs LLMs locally — no API key required."
    echo

    # Install Ollama if not present
    if ! command -v ollama >/dev/null 2>&1; then
      echo "[INFO] Installing Ollama..."
      curl -fsSL https://ollama.com/install.sh | sh
    else
      echo "[INFO] Ollama already installed: $(ollama --version 2>/dev/null || true)"
    fi

    # Set base URL
    _set_env "OLLAMA_BASE_URL" "http://127.0.0.1:11434"

    # Pull a model
    echo
    echo "Would you like to pull an Ollama model now?"
    read -rp "Pull a model? [Y/n]: " DO_PULL
    if [[ "${DO_PULL,,}" != "n" ]]; then
      bash "$SCRIPT_DIR/pull_ollama_model.sh"
      # pick up whatever model was set by the pull script
      CURRENT_MODEL="$(_get_env OLLAMA_MODEL)"
    else
      CURRENT_MODEL="$(_get_env OLLAMA_MODEL)"
      [[ -z "$CURRENT_MODEL" ]] && _set_env "OLLAMA_MODEL" "llama3.2:3b"
    fi
    echo "[INFO] OLLAMA_MODEL=$( _get_env OLLAMA_MODEL)"
    ;;
esac

echo
echo "[INFO] LLM backend configuration saved to $ENV_FILE"
echo "[INFO] Run 'source env.sh' to load the new settings into your shell."
echo "=================================================="
