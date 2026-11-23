#!/usr/bin/env python3
"""Route a ToDesk voice call's playback to the interview microphone on request."""
import json
import os
import signal
import subprocess
import sys
import threading
from contextlib import ExitStack
from pathlib import Path


SINK = 'xinyu_todesk_voice'
MIC = 'xinyu_todesk_mic'
UNIT = 'xinyu-todesk-mic.service'


def pulse(*args, stderr=None):
    return subprocess.check_output(['pactl', *map(str, args)], text=True, timeout=5, stderr=stderr).strip()


def streams(kind):
    return json.loads(pulse('-f', 'json', 'list', kind))


def is_call(stream):
    props = stream.get('properties', {})
    binary = props.get('application.process.binary', '').casefold()
    app = props.get('application.name', '').casefold()
    return (binary in {'todesk', 'todesk_session', 'todesk_service'} or app == 'todesk_pulse') and (
        props.get('media.role') not in {'event', 'music', 'video'} and 'event.id' not in props)


def best_call(items):
    candidates = [s for s in items if is_call(s)]
    phone = [s for s in candidates if s['properties'].get('media.role') in {'phone', 'communication'}]
    matches = phone or candidates
    return matches[0] if len(matches) == 1 else None


def restore(*args):
    try:
        pulse(*args, stderr=subprocess.DEVNULL)
    except (subprocess.SubprocessError, OSError):
        # PulseAudio may already have removed a stream or ended the desktop session.
        pass


def create_devices(stack):
    sink = pulse('get-default-sink')
    source = pulse('get-default-source')
    if any(s['name'] == SINK for s in streams('sinks')) or any(s['name'] == MIC for s in streams('sources')):
        raise RuntimeError('同名虚拟音频设备已存在。请先关闭旧的远程麦克风。')
    # Some desktops automatically move running applications when a new device appears.
    playback = {s['index']: s['sink'] for s in streams('sink-inputs')}
    capture = {s['index']: s['source'] for s in streams('source-outputs')}
    try:
        module = pulse('load-module', 'module-null-sink', f'sink_name={SINK}', 'rate=48000',
                       'channels=1', 'channel_map=mono',
                       "sink_properties='device.description=ToDesk_Call_Input device.class=filter'")
        stack.callback(restore, 'unload-module', module)
        module = pulse('load-module', 'module-remap-source', f'master={SINK}.monitor', f'source_name={MIC}',
                       "source_properties='device.description=ToDesk_Remote_Microphone device.class=filter'")
        stack.callback(restore, 'unload-module', module)
    finally:
        if pulse('get-default-sink') == SINK:
            restore('set-default-sink', sink)
        if pulse('get-default-source') in {MIC, SINK + '.monitor'}:
            restore('set-default-source', source)
    virtual_sink = next(d['index'] for d in streams('sinks') if d['name'] == SINK)
    virtual_sources = {d['index'] for d in streams('sources') if d['name'] in {MIC, SINK + '.monitor'}}
    for s in streams('sink-inputs'):
        if s['sink'] == virtual_sink:
            restore('move-sink-input', s['index'], playback.get(s['index'], sink))
    for s in streams('source-outputs'):
        if s['source'] in virtual_sources and s['index'] in capture:
            restore('move-source-output', s['index'], capture[s['index']])
    return virtual_sink


def run():
    import fcntl
    stopped = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, lambda *_: stopped.set())
    with ExitStack() as stack:
        lock = stack.enter_context((Path(os.environ['XDG_RUNTIME_DIR']) / 'xinyu-todesk-mic.lock').open('a'))
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise RuntimeError('远程麦克风已在运行。') from None
        print('等待 Windows 的 ToDesk 语音通话。', flush=True)
        sink_index = None
        last = None
        previous_sink = None
        def return_call():
            if last is not None and previous_sink is not None:
                restore('move-sink-input', last, previous_sink)
        while not stopped.is_set():
            inputs = streams('sink-inputs')
            call = best_call(inputs)
            if call and sink_index is None:
                sink_index = create_devices(stack)
                stack.callback(return_call)
                continue  # Re-read after device creation and default restoration.
            if last is not None and (not call or last != call['index']):
                return_call()
                last, previous_sink = None, None
                print('通话声音已断开，等待重新接通。', flush=True)
            if call and (last != call['index'] or call['sink'] != sink_index):
                original = call['sink'] if call['sink'] != sink_index else pulse('get-default-sink')
                try:
                    pulse('move-sink-input', call['index'], SINK)
                except subprocess.CalledProcessError:
                    if any(s['index'] == call['index'] for s in streams('sink-inputs')):
                        raise
                else:
                    last, previous_sink = call['index'], original
                    print('通话已接到访谈麦克风。', flush=True)
            stopped.wait(.7)


def desktop():
    def active():
        return subprocess.run(['systemctl', '--user', 'is-active', '--quiet', UNIT]).returncode == 0
    if active():
        call = best_call(streams('sink-inputs'))
        connected = call and any(d['index'] == call['sink'] and d['name'] == SINK for d in streams('sinks'))
        status = '已接到 ToDesk 通话。请从老师工作台开始语音访谈。' if connected else '正在等待 Windows 的 ToDesk 语音通话。'
        answer = subprocess.run(['zenity', '--question', '--title=心语远程麦克风',
                                 '--text=' + status,
                                 '--ok-label=关闭远程麦克风', '--cancel-label=保持运行'])
        if answer.returncode == 0:
            subprocess.run(['systemctl', '--user', 'stop', UNIT], check=True)
        return
    subprocess.run(['systemd-run', '--user', '--collect', '--quiet', '--unit=' + UNIT,
                    sys.executable, str(Path(__file__).resolve())], check=True)
    subprocess.run(['zenity', '--info', '--title=心语远程麦克风',
                    '--text=请先在 Windows 的 ToDesk 发起语音通话，并在远程桌面接听。接通后，从老师工作台开始“语音与按钮”访谈。'])


if __name__ == '__main__':
    try:
        if '--desktop' in sys.argv:
            desktop()
        else:
            run()
    except (OSError, RuntimeError, subprocess.SubprocessError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        if '--desktop' in sys.argv:
            subprocess.run(['zenity', '--error', '--title=心语远程麦克风', '--text=无法切换远程麦克风。请检查用户服务状态。'])
        sys.exit(1)
