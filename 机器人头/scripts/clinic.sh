#!/usr/bin/env bash
# The integrated intake, interview, tasks, tracking and teacher workstation.
set -euo pipefail
cd "$(dirname "$0")/.."
exec bash scripts/web.sh --track --open-browser "$@"
