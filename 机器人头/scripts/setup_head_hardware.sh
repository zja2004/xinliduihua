#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
HEAD_ROS_WS="${ORIN_HEAD_ROS_WS:-/home/nvidia/.local/share/orin-robot-head/ros_ws}"
ROS_PACKAGES="${ORIN_HEAD_ROS_PACKAGES:-$PWD/../robot-talk-ros2}"
REFERENCE_ROOT="$PWD/机器人头操作代码/解压/眼球追踪模块代码参考/眼球追踪模块代码参考"
MOUTH_SOURCE="$PWD/机器人头操作代码/解压/speak_mouth/speak_mouth"
for required in "$REFERENCE_ROOT/integrations/gimbal_core/CMakeLists.txt" "$MOUTH_SOURCE/package.xml" "$ROS_PACKAGES"/{robot_interfaces,robot_msgs,device_io}/package.xml; do
  if [[ ! -f "$required" ]]; then
    echo "缺少获授权的硬件依赖：$required。请按仓库 README 准备 ROS 包和参考源码。" >&2
    exit 1
  fi
done
mkdir -p "$HEAD_ROS_WS/src" .runtime/head-hardware
# ROS Humble's IDL generator fails with the Chinese project directory. Only the
# generated workspace uses this ASCII path; original uploaded sources stay here.
for PACKAGE_NAME in robot_interfaces robot_msgs device_io; do
  rsync -a --exclude='build/' --exclude='install/' --exclude='ros2/' \
    --exclude='build_logs/' --exclude='log/' --exclude='logs/' --exclude='run/' \
    --exclude='__pycache__/' "$ROS_PACKAGES/$PACKAGE_NAME" "$HEAD_ROS_WS/src/"
done
rsync -a --exclude='build/' --exclude='install/' --exclude='ros2/' \
  --exclude='results/' --exclude='build_logs/' --exclude='log/' --exclude='logs/' \
  --exclude='run/' --exclude='__pycache__/' "$MOUTH_SOURCE" "$HEAD_ROS_WS/src/"
cmake -S "$REFERENCE_ROOT/integrations/gimbal_core" \
  -B .runtime/head-hardware/gimbal-build -DCMAKE_BUILD_TYPE=Release
cmake --build .runtime/head-hardware/gimbal-build -j2
.venv/bin/python -m pip install pyserial==3.5
set +u
source /opt/ros/humble/setup.bash
set -u
cd "$HEAD_ROS_WS"
colcon build --merge-install --symlink-install \
  --packages-select robot_interfaces robot_msgs device_io speak_mouth \
  --cmake-args -DBUILD_TESTING=OFF
