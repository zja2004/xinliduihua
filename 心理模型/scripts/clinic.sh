#!/usr/bin/env bash
# Run the shared robot-enabled application beside this original model project.
set -euo pipefail
project_dir="$(cd "$(dirname "$0")/.." && pwd)"
robot_project="$(dirname "$project_dir")/机器人头"
if [[ ! -f "$robot_project/scripts/clinic.sh" ]]; then
  echo "找不到集成项目：$robot_project" >&2
  exit 1
fi
exec bash "$robot_project/scripts/clinic.sh" "$@"
