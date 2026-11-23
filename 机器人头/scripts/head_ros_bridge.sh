#!/usr/bin/env bash
set -eo pipefail
cd "$(dirname "$0")/.."
source /opt/ros/humble/setup.bash
HEAD_ROS_WS="${ORIN_HEAD_ROS_WS:-/home/nvidia/.local/share/orin-robot-head/ros_ws}"
source "$HEAD_ROS_WS/install/local_setup.bash"
export PYTHONPATH="$HEAD_ROS_WS/src/speak_mouth/deps${PYTHONPATH:+:$PYTHONPATH}"
export ORIN_HEAD_ROS_WS="$HEAD_ROS_WS"
exec /usr/bin/python3 -u scripts/head_ros_bridge.py
