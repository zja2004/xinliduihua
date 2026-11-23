#!/usr/bin/env bash
# Open the staff page and keep the web service independent of the launching window.
set -euo pipefail
cd "$(dirname "$0")/.."
base=http://127.0.0.1:18088
unit=xinyu-school-workbench.service

ready() {
    curl --noproxy '*' --fail --silent --max-time 1 "$base/api/school/me" |
        python3 -c 'import json,sys; sys.exit("actor" not in json.load(sys.stdin))' 2>/dev/null
}

fail() {
    printf '%s\n' "$1" >&2
    zenity --error --title='心语访谈' --text="$1" || true
    exit 1
}

if ! ready; then
    if ! systemctl --user is-active --quiet "$unit"; then
        systemd-run --user --quiet --collect --unit="$unit" --working-directory="$PWD" \
            /bin/bash "$PWD/scripts/web.sh" --robot --chat-profile psyche --chat-port 18082 --chat-max-tokens 160 ||
            systemctl --user is-active --quiet "$unit" ||
            fail '工作台后台启动失败。可在终端运行 journalctl --user -u xinyu-school-workbench 查看原因。'
    fi
    for ((attempt=0; attempt<60; attempt++)); do
        ready && break
        systemctl --user is-active --quiet "$unit" || break
        sleep 0.5
    done
    ready || fail '学校工作台尚未就绪。可在终端运行 journalctl --user -u xinyu-school-workbench 查看启动日志。'
fi
gio open "$base/school" || fail "工作台已启动。请在浏览器中打开 $base/school"
