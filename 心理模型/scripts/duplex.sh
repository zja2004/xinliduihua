#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
if ! pactl info >/dev/null 2>&1; then
  echo '无法连接桌面音频服务；请在本机已登录的桌面终端运行。' >&2
  exit 1
fi
if ! .venv/bin/python -c 'import sherpa_onnx, sounddevice, soundfile, scipy' 2>/dev/null; then
  bash scripts/bootstrap.sh
fi
headset_mic=$(pactl list short sources | awk 'tolower($2) ~ /usb-.*me01tc/ && $2 !~ /\.monitor$/ {print $2; exit}')
headset_speaker=$(pactl list short sinks | awk 'tolower($2) ~ /usb-.*me01tc/ {print $2; exit}')
tw16_mic=$(pactl list short sources | awk '$2 ~ /usb-Generic_TW16_pro_/ && $2 !~ /\.monitor$/ {print $2; exit}')
tw16_speaker=$(pactl list short sinks | awk '$2 ~ /usb-Generic_TW16_pro_/ {print $2; exit}')
mic="${ORIN_MIC_SOURCE:-${headset_mic:-${tw16_mic:-$(pactl get-default-source)}}}"
speaker="${ORIN_SPEAKER_SINK:-${headset_speaker:-${tw16_speaker:-$(pactl get-default-sink)}}}"
printf '麦克风：%s\n扬声器：%s\n' "$mic" "$speaker"
case "$mic" in
  *.monitor|orin_aec_source) echo '请选择物理麦克风：ORIN_MIC_SOURCE=源名称' >&2; exit 1 ;;
esac
case "$speaker" in
  orin_aec_sink) echo '请选择物理扬声器：ORIN_SPEAKER_SINK=输出名称' >&2; exit 1 ;;
esac
mkdir -p .runtime
exec 9> .runtime/audio-route.lock
if ! flock -n 9; then
  echo '另一个访谈进程正在使用音频，请先结束旧访谈程序。' >&2
  exit 1
fi
route_suffix="_$$_$(.venv/bin/python -c 'import secrets; print(secrets.token_hex(4))')"
source_name="orin_aec_source$route_suffix"
sink_name="orin_aec_sink$route_suffix"
module_id=$(pactl load-module module-echo-cancel \
  "source_master=$mic" "sink_master=$speaker" \
  "source_name=$source_name" "sink_name=$sink_name" \
  aec_method=webrtc rate=48000 channels=1)
trap 'pactl unload-module "$module_id" >/dev/null 2>&1 || true' EXIT
export PULSE_SOURCE="$source_name" PULSE_SINK="$sink_name"
export PYTHONPATH="$PWD/src${PYTHONPATH:+:$PYTHONPATH}"
.venv/bin/python -m orin_voice.cli call --duplex "$@"
