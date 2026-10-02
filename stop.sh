#!/usr/bin/env bash
# Stop the background services started by start.sh.
# Usage:  source stop.sh      (sourced, so it can also unset the AGY_LLM_GATEWAY_* exports)
pkill -f "litellm --config" && echo "LiteLLM stopped"
pkill -f "laya-serve" && echo "laya-serve stopped"
unset AGY_LLM_GATEWAY_URL AGY_LLM_GATEWAY_API_KEY 2>/dev/null || true
