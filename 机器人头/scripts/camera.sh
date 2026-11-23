#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
export PYTHONPATH="$PWD/src${PYTHONPATH:+:$PYTHONPATH}"
if .venv/bin/python - <<'PY'
import urllib.request
try:
    with urllib.request.urlopen('http://127.0.0.1:18088/api/camera/status', timeout=2) as result:
        if result.status != 200:
            raise RuntimeError('unsupported server')
except Exception:
    raise SystemExit(1)
import webbrowser
webbrowser.open('http://127.0.0.1:18088/camera')
PY
then
  exit 0
fi
exec .venv/bin/python -m orin_voice.web --camera --open-browser "$@"
