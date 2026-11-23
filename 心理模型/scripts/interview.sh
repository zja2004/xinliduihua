#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
questions="${ORIN_QUESTIONS:-configs/dass21.zh.json}"
.venv/bin/python -u scripts/prepare_interview.py "$questions"
exec bash scripts/duplex.sh --silence-ms 600 --profile baseline --max-tokens 160 --ctx-size 4096 --questions "$questions" "$@"
