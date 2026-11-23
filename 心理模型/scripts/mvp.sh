#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
export PYTHONPATH="$PWD/src${PYTHONPATH:+:$PYTHONPATH}"
# Text MVP uses the standard library; copied Python environments are not needed.
python3 - <<'PY'
import socket
try:
    with socket.socket() as probe:
        probe.bind(('127.0.0.1', 0))
except OSError as error:
    raise SystemExit(f'无法创建本地服务，请在本机终端运行本脚本：{error}')
PY
if ! python3 - <<'PY'
from orin_voice.server import binary_path
try:
    binary_path()
except (OSError, RuntimeError) as error:
    print(error)
    raise SystemExit(1)
PY
then
  bash scripts/build_jetson.sh
fi
exec python3 -m orin_voice.cli chat "$@"
