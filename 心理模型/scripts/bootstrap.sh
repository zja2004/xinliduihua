#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
if [ -f .venv/pyvenv.cfg ]; then
  if ! python3 - <<'PY'
import pathlib, platform, sys
root = pathlib.Path('.venv')
config = dict(line.split(' = ', 1) for line in (root / 'pyvenv.cfg').read_text().splitlines() if ' = ' in line)
version = '.'.join(map(str, sys.version_info[:2]))
foreign = 'x86_64' if platform.machine() == 'aarch64' else 'aarch64'
sys.exit(0 if config.get('version', '').startswith(version + '.') and not any(root.rglob('*' + foreign + '*.so')) else 1)
PY
  then
    backup=".venv.backup.$(date +%Y%m%d-%H%M%S).$$"
    mv .venv "$backup"
    printf 'Incompatible copied Python environment preserved at %s\n' "$backup"
  fi
fi
if ! python3 -m venv .venv; then
  python3 -m venv --without-pip .venv
  python3 scripts/bootstrap_pip.py
fi
.venv/bin/python -m pip install --upgrade pip
.venv/bin/python -m pip install -e '.[audio]'
printf '\nPython environment ready. Download assets with:\n'
printf '  .venv/bin/python -m orin_voice.assets\n'
