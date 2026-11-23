#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
export PYTHONPATH="$PWD/src${PYTHONPATH:+:$PYTHONPATH}"
chat_args=()
chat_profile="${ORIN_CHAT_PROFILE:-psyche}"
chat_args+=(--chat-profile "$chat_profile")
if [[ -n "${ORIN_CHAT_PORT:-}" ]]; then
  chat_args+=(--chat-port "$ORIN_CHAT_PORT")
fi
if [[ -n "${ORIN_CHAT_MAX_TOKENS:-}" ]]; then
  chat_args+=(--chat-max-tokens "$ORIN_CHAT_MAX_TOKENS")
fi
exec .venv/bin/python -m orin_voice.web --robot --track --open-browser "${chat_args[@]}" "$@"
