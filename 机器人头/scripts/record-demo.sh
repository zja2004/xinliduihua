#!/usr/bin/env bash
# Record the local X11 desktop, microphone and system playback for a narrated demonstration.
set -eu
umask 077

finish() {
    printf '\n%s\n' "$1"
    read -r -p '按回车关闭此窗口。' _ || true
    exit "${2:-0}"
}

[[ -n ${DISPLAY:-} && ${XDG_SESSION_TYPE:-x11} == x11 ]] ||
    finish '请在本机的 X11 图形桌面打开录屏。' 1
exec 9>"${XDG_RUNTIME_DIR:?}/xinyu-demo-record.lock"
flock -n 9 || finish '录屏窗口已经打开。请切回原窗口操作。' 1

sink=$(pactl get-default-sink) || finish '读取扬声器失败。请检查系统的声音设置。' 1
[[ "$sink" != xinyu_todesk_voice ]] || finish '请先在系统声音设置中选择用于播报的输出设备，再打开录屏。' 1
monitor="$sink.monitor"
sources=$(pactl list short sources | cut -f2)
if [[ -n ${ORIN_MIC_SOURCE:-} ]]; then
    mic="$ORIN_MIC_SOURCE"
elif grep -Fxq xinyu_todesk_mic <<<"$sources"; then
    mic=xinyu_todesk_mic
else
    mic=$(pactl get-default-source) || finish '读取麦克风失败。请检查系统的声音设置。' 1
fi
grep -Fxq -- "$monitor" <<<"$sources" || finish '找不到当前输出的声音监听设备。请检查系统的输出设备。' 1
[[ -n "$mic" && "$mic" != *.monitor ]] && grep -Fxq -- "$mic" <<<"$sources" ||
    finish '请先在「设置 → 声音 → 输入」选中可用麦克风，再打开录屏。' 1

folder="$(xdg-user-dir VIDEOS)/心语演示"
mkdir -p -- "$folder" || finish '无法创建视频保存目录。' 1
printf '将录制整个屏幕、讲解麦克风和电脑播报。\n讲解麦克风：%s\n保存位置：%s\n' "$mic" "$folder"
printf '请先登录工作台，关闭密码文件和通知，打开虚构学生的演示页面。\n'
printf '按回车后有 5 秒切回浏览器。结束时切回此窗口，按 Ctrl+C 一次。\n'
read -r -p '准备好后按回车开始。' _ || exit 0
for seconds in 5 4 3 2 1; do printf '%s ' "$seconds"; sleep 1; done
printf '\n正在录制。结束时切回这里按 Ctrl+C，等待保存提示。\n'

file="$folder/心语演示-$(date +%Y%m%d-%H%M%S)-$$.mp4"
audio_caps='audio/x-raw,format=S16LE,rate=48000,channels=2'
audio_input() {
    audio_streams+=(pulsesrc "device=$1" '!' queue '!' audioconvert '!' audioresample '!' "$audio_caps" '!' volume "volume=$2" '!' mix.)
}
audio_streams=()
audio_input "$mic" 1.0
audio_input "$monitor" 0.7

# gst-launch -e receives the terminal's SIGINT and finalizes the MP4 before returning.
trap ':' INT
if gst-launch-1.0 -q -e \
    mp4mux name=mux '!' filesink "location=$file.part" \
    ximagesrc use-damage=false show-pointer=true '!' video/x-raw,framerate=15/1 '!' \
    queue max-size-buffers=3 max-size-bytes=0 max-size-time=0 leaky=downstream '!' \
    videoconvert '!' video/x-raw,format=I420 '!' \
    x264enc tune=zerolatency speed-preset=ultrafast threads=2 bitrate=4500 '!' \
    video/x-h264,stream-format=avc,alignment=au '!' queue '!' mux. \
    audiomixer name=mix '!' audioconvert '!' "$audio_caps" '!' \
    lamemp3enc target=bitrate bitrate=160 cbr=true '!' mpegaudioparse '!' queue '!' mux. \
    "${audio_streams[@]}" && [[ -s "$file.part" ]]; then
    mv -- "$file.part" "$file" || finish "文件重命名失败。录制文件位于 $file.part" 1
    finish "录制已保存到 $file"
fi
finish "录制中断。若已生成部分内容，可在 $folder 查看 .part 文件。" 1
