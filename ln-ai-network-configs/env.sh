#!/usr/bin/env bash

# LN_AI_Project environment bootstrap
# Source this from anywhere to get consistent paths

export LN_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# Common paths (optional convenience)
export LN_RUNTIME="$LN_ROOT/runtime"
export LN_LOGS="$LN_ROOT/logs"
export LN_SCRIPTS="$LN_ROOT/scripts"