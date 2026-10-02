#!/usr/bin/env bash
# Start self-hosted Laya and the LiteLLM gateway, then point the Antigravity CLI (agy) at the gateway.
# Usage:  source start.sh      (from the repo root; sourced, so the AGY_LLM_GATEWAY_* exports reach your shell)
# Works in bash and zsh. No `set -u`/`set -e`: they'd leak into your shell. Errors return instead.

_LAYA_HOOK_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" && pwd)"

_laya_hook_wait() {  # url name log timeout_s
  local start=$SECONDS
  until curl -s "$1" >/dev/null; do
    if (( SECONDS - start >= $4 )); then
      echo "ERROR: $2 didn't come up within $4 s. See $3" >&2
      return 1
    fi
    sleep 2
  done
}

_laya_hook_start() {
  if [ -z "${GOOGLE_CLOUD_PROJECT:-}" ]; then
    echo "ERROR: set GOOGLE_CLOUD_PROJECT first (see .env.example)." >&2
    return 1
  fi
  export LAYA_API_KEY="${LAYA_API_KEY:-local-laya}"

  if ! curl -s 127.0.0.1:8701/health >/dev/null; then
    echo "Starting laya-serve on :8701 (first run downloads ~800 MB)…"
    (LAYA_HOST=127.0.0.1 LAYA_PORT=8701 LAYA_MODELS=english LAYA_THREADS=4 \
       nohup uv run -q --python 3.13 --with 'laya[serve]>=0.3.22' laya-serve > /tmp/laya-serve.log 2>&1 &)
    _laya_hook_wait 127.0.0.1:8701/health laya-serve /tmp/laya-serve.log "${LAYA_START_TIMEOUT:-600}" || return 1
  fi
  # Warm Laya up, so the first routed request isn't slow
  local _
  for _ in 1 2; do curl -s -o /dev/null -X POST 127.0.0.1:8701/v1/systemone -H "Authorization: Bearer $LAYA_API_KEY" \
    -H 'Content-Type: application/json' -d '{"model":"laya","state":"warm up","questions":{"q":{"type":"noul","instructions":"Is this a test?"}}}'; done

  if ! curl -s 127.0.0.1:4000/health/liveliness >/dev/null; then
    echo "Starting LiteLLM gateway on :4000…"
    (cd "$_LAYA_HOOK_DIR" && LITELLM_LOG=INFO nohup uv tool run -q --python 3.13 --from 'litellm[proxy]>=1.103' \
       --with google-auth --with google-cloud-aiplatform litellm --config litellm_config.yaml \
       --host 127.0.0.1 --port 4000 > /tmp/litellm.log 2>&1 &)
    _laya_hook_wait 127.0.0.1:4000/health/liveliness LiteLLM /tmp/litellm.log "${LITELLM_START_TIMEOUT:-180}" || return 1
  fi

  # AGY's LLM gateway mode (undocumented): no Antigravity sign-in needed, every request goes through LiteLLM
  export AGY_LLM_GATEWAY_URL=http://127.0.0.1:4000
  export AGY_LLM_GATEWAY_API_KEY=local
  echo 'Ready. Watch routing + cost:  tail -F /tmp/litellm.log | grep --line-buffered -E "laya-(route|cost)"'
}

_laya_hook_start
_laya_hook_status=$?
unset -f _laya_hook_start _laya_hook_wait
unset _LAYA_HOOK_DIR
return $_laya_hook_status 2>/dev/null || exit $_laya_hook_status
