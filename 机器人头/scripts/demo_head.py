#!/usr/bin/env python3
"""Small, finite head test relative to fresh live pose; never starts homing.

Requires --execute before it sends motor commands. Other 23 axes remain at the
initial live pose; eye and jaw are tested sequentially, then restored.
"""
import argparse
from dataclasses import asdict
import fcntl
import json
import math
from pathlib import Path
import signal
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'src'))
DRIVER = Path('/home/nvidia/.local/share/orin-robot-head/ros_ws/src/device_io')
sys.path.insert(0, str(DRIVER))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--execute', action='store_true')
    parser.add_argument('--port')
    parser.add_argument('--sink', default='alsa_output.usb-BurrBrown_from_Texas_Instruments_USB_AUDIO_CODEC-00.analog-stereo')
    args = parser.parse_args()
    folder = ROOT / '.runtime/head-hardware'
    folder.mkdir(parents=True, exist_ok=True)
    from device_io.calibration import read_live_calibration
    from device_io.serial_transport import discover_transport
    from device_io.protocol_v25 import CMD_MOTOR_CONTROL, pack_positions_v25, decode_cmd1
    import numpy as np
    from orin_voice.audio import SpeechSynthesizer, save_wav
    ports = sorted(Path('/dev/serial/by-id').glob('*Head_Controller*'))
    if args.port is None and len(ports) != 1:
        raise RuntimeError('需要唯一的 Head Controller 串口')
    port = args.port or str(ports[0])
    audio, rate = SpeechSynthesizer(cache=True).synthesize('你好，我是小心，很高兴见到你。')
    wav = folder / 'demo-greeting.wav'
    save_wav(wav, audio, rate)
    trace = []
    report = {'port': port, 'executed': args.execute, 'trace': trace, 'audio_file': str(wav),
              'phrase': '你好，我是小心，很高兴见到你。', 'audio_sample_rate': rate,
              'audio_duration_s': len(audio) / rate, 'physical_observation': 'requires onsite confirmation'}
    transport = None
    player = None
    baseline = None
    sent_any = False
    lock = (folder / 'serial-demo.lock').open('a')
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    def terminate(signum, frame):
        raise KeyboardInterrupt('收到停止信号')
    signal.signal(signal.SIGTERM, terminate)
    try:
        # Refuse to compete with another process that already has the board open.
        in_use = subprocess.run(['fuser', str(Path(port).resolve())], capture_output=True)
        if in_use.returncode == 0:
            raise RuntimeError('串口已被其他进程占用，取消演示')
        transport = discover_transport(port=port, baud_candidates=(115200,), response_timeout_s=1.5)
        calibration = read_live_calibration(transport, timeout_s=1.5)
        if not calibration.calibrated:
            raise RuntimeError('板端标定标志无效，未发运动命令')
        def current():
            frame = transport.latest_cmd1()
            if frame is None or (time.monotonic_ns() - frame.host_monotonic_ns) > 500_000_000:
                raise RuntimeError('控制板状态超过500ms未更新，停止测试')
            state = decode_cmd1(frame)
            if state is None:
                raise RuntimeError('无法解析头板状态')
            return list(state.positions_deg)
        baseline = current()
        for i, (position, low, high) in enumerate(zip(baseline, calibration.minimum_deg, calibration.maximum_deg)):
            if not all(math.isfinite(x) for x in (position, low, high)) or not low <= position <= high:
                raise RuntimeError(f'S{i+1} 实时位置不在标定范围内，取消演示')
        report.update(calibration=asdict(calibration), baseline=baseline)
        print('实时标定读取成功；小动作基于当前姿态，不执行全头回零。', flush=True)
        if not args.execute:
            return
        last = list(baseline)
        def send(pose, stage):
            nonlocal sent_any, last
            measured = current()
            for i, (value, low, high) in enumerate(zip(pose, calibration.minimum_deg, calibration.maximum_deg)):
                if not low <= value <= high:
                    raise RuntimeError(f'S{i+1} 目标越界')
                if i not in (9, 21) and value != baseline[i]:
                    raise RuntimeError('不允许改变眼球/下颌以外的轴')
                if i in (9,21) and abs(value - baseline[i]) > 1.51:
                    raise RuntimeError('本次测试位移超过1.5度')
            transport.send(CMD_MOTOR_CONTROL, pack_positions_v25(pose, planning_mode=1))
            sent_any = True
            trace.append({'stage': stage, 'monotonic_time': time.monotonic(),
                          'command_s10': round(pose[9],2), 'command_s22': round(pose[21],2),
                          'reported_s10': measured[9], 'reported_s22': measured[21]})
            last = list(pose)
        # Horizontal eyes: 1 degree left/right, easing in 0.1 degree increments.
        print('开始：眼球小幅左右摆动。', flush=True)
        eye_value = baseline[9]
        for target in (baseline[9]+1., baseline[9]-1., baseline[9]):
            target = max(calibration.minimum_deg[9], min(calibration.maximum_deg[9], target))
            while abs(target-eye_value) > .005:
                eye_value += max(-.1, min(.1, target-eye_value))
                pose = list(baseline); pose[9] = round(eye_value,2)
                send(pose, 'eye');time.sleep(.05)
            time.sleep(.15)
        send(list(baseline), 'eye_restored')
        print('开始发声：你好，我是小心，很高兴见到你。', flush=True)
        player = subprocess.Popen(['paplay', '--device='+args.sink, str(wav)],
                                  stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        started = time.monotonic()
        jaw = baseline[21]
        while player.poll() is None:
            elapsed = time.monotonic()-started
            begin = round(max(0.,elapsed-.03)*rate)
            end = min(len(audio), begin+round(.05*rate))
            level = float(np.sqrt(np.mean(audio[begin:end]**2))) if end > begin else 0.
            target = max(calibration.minimum_deg[21], baseline[21] - min(1.5,level*22.))
            jaw += max(-.12,min(.12,target-jaw))
            pose = list(baseline);pose[21] = round(jaw,2)
            send(pose,'speech_energy_mouth');time.sleep(.05)
            if elapsed > len(audio)/rate+5:
                raise RuntimeError('音频播放超时')
        error = player.stderr.read().decode(errors='replace')
        report['audio_returncode'] = player.returncode
        if player.returncode != 0:
            raise RuntimeError('USB音频播放失败：'+error)
        # Gentle return of the lower jaw to the starting pose.
        while abs(jaw-baseline[21]) > .005:
            jaw += max(-.12,min(.12,baseline[21]-jaw))
            pose = list(baseline);pose[21] = round(jaw,2)
            send(pose,'jaw_return');time.sleep(.05)
        send(list(baseline),'restored');time.sleep(.4)
        report.update(success=True, final_reported_pose=current())
        print('演示完成：眼球/嘴部小动作与USB发声已发送，原姿态已恢复。',flush=True)
    except BaseException as error:
        report.update(success=False,error=f'{type(error).__name__}: {error}')
        raise
    finally:
        if player and player.poll() is None:
            player.terminate()
            try:player.wait(timeout=2)
            except subprocess.TimeoutExpired:player.kill();player.wait()
        if transport:
            if sent_any and baseline is not None and transport.connected:
                try:
                    frame=transport.latest_cmd1()
                    if frame and time.monotonic_ns()-frame.host_monotonic_ns < 500_000_000:
                        transport.send(CMD_MOTOR_CONTROL,pack_positions_v25(baseline,planning_mode=1))
                        report['restore_command_sent']=True
                except Exception as error:report['restore_error']=str(error)
            transport.close()
        (folder/'demo-result.json').write_text(json.dumps(report,ensure_ascii=False,indent=2))
        fcntl.flock(lock,fcntl.LOCK_UN);lock.close()


if __name__ == '__main__':main()
