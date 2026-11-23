#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
export PYTHONPATH="$PWD/src${PYTHONPATH:+:$PYTHONPATH}"
if [ ! -x .venv/bin/python ] || ! .venv/bin/python -c 'import sherpa_onnx, sounddevice, soundfile, scipy' 2>/dev/null; then
  printf '请先安装本机语音依赖：bash scripts/bootstrap.sh\n' >&2
  exit 1
fi
exec .venv/bin/python -m orin_voice.cli call "$@"
