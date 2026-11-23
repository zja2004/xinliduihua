#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
questions="${ORIN_QUESTIONS:-configs/primary21.zh.json}"
chat_profile="${ORIN_CHAT_PROFILE:-psyche}"
.venv/bin/python -u scripts/prepare_interview.py "$questions"
exec bash scripts/duplex.sh --silence-ms 600 --profile "$chat_profile" --max-tokens 160 --ctx-size 4096 --questions "$questions" "$@"
