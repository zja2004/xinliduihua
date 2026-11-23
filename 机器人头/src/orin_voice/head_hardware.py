"""Normalized head control; dry-run by default, optional calibrated ROS bridge.

The archive's mouth planner generates eight offsets and its eye reference
uses S7/S10. This adapter never writes a serial port or starts a motor driver.
"""
import base64
import copy
import json
import math
import os
from pathlib import Path
import queue
import subprocess
import threading
import time

from .settings import ROOT


class HeadHardware:
    def __init__(self, config=None):
        self.config = json.loads((ROOT / 'configs/head_hardware.json').read_text())
        if config:
            self.config.update(config)
        if self.config['backend'] not in {'dry_run', 'ros', 'serial'}:
            raise ValueError('head backend must be dry_run, ros or serial')
        self.lock = threading.RLock()
        self.controller = None
        self.process = None
        self.commands = queue.Queue(maxsize=128)
        self.closed = threading.Event()
        self.sequence = 0
        self._state = {'backend': self.config['backend'], 'running': False,
                       'connected': False, 'calibration_ready': False,
                       'motion_enabled': False, 'error': None,
                       'target': {'x': 0., 'y': 0., 'visible': False},
                       'utterance_id': None, 'audio_chunks': 0, 'dropped_commands': 0}

    def start(self):
        with self.lock:
            if self.config['backend'] == 'serial':
                from .head_serial import DirectHeadController
                if self.controller is None:
                    self.controller = DirectHeadController(self.config)
                return self.controller.start()
            if self._state['running']:
                return self.status()
            if self.config['backend'] == 'dry_run':
                self._state['running'] = True
                return self.status()
            if not self.config.get('calibration_confirmed'):
                self._state['error'] = 'ROS 运动未开启：请确认实时头板标定后设置 calibration_confirmed=true'
                return self.status()
            (ROOT / '.runtime/head-hardware').mkdir(parents=True, exist_ok=True)
            script = ROOT / 'scripts/head_ros_bridge.sh'
            (ROOT / '.runtime/head-hardware').mkdir(parents=True, exist_ok=True)
            self.closed.clear()
            self.process = subprocess.Popen(['bash', str(script)], cwd=ROOT, stdin=subprocess.PIPE,
                stdout=subprocess.PIPE, stderr=(ROOT / '.runtime/head-hardware/bridge-stderr.log').open('a'),
                text=True, bufsize=1, env={**os.environ, 'ROS_DOMAIN_ID': str(self.config['ros_domain_id']),
                    'ROS_LOCALHOST_ONLY': '1' if self.config.get('ros_localhost_only', True) else '0',
                    'ORIN_HEAD_CONFIG': json.dumps(self.config)})
            self._state.update(running=True, error=None)
            threading.Thread(target=self._writer, daemon=True, name='head-commands').start()
            threading.Thread(target=self._reader, daemon=True, name='head-status').start()
            return self.status()

    def _writer(self):
        while not self.closed.is_set():
            try:
                command = self.commands.get(timeout=.2)
            except queue.Empty:
                continue
            try:
                self.process.stdin.write(json.dumps(command, ensure_ascii=False) + '\n')
                self.process.stdin.flush()
            except (BrokenPipeError, OSError, ValueError) as error:
                with self.lock:
                    self._state.update(error=str(error), running=False, motion_enabled=False)
                return

    def _reader(self):
        for line in self.process.stdout:
            try:
                values = json.loads(line)
            except ValueError:
                continue
            with self.lock:
                for key in ('connected', 'calibration_ready', 'motion_enabled', 'error'):
                    if key in values:
                        self._state[key] = values[key]
        with self.lock:
            self._state.update(running=False, motion_enabled=False)
            if not self.closed.is_set():
                self._state['error'] = self._state['error'] or '头部 ROS 桥接进程已退出'

    def _send(self, command):
        if self.config['backend'] != 'ros' or not self._state['running']:
            return
        try:
            self.commands.put_nowait(command)
        except queue.Full:
            with self.lock:
                self._state['dropped_commands'] += 1
                self._state['error'] = '头部控制通道积压，已丢弃过期动作；语音继续独立运行'

    def track_target(self, x_norm, y_norm, visible=True):
        x, y = float(x_norm), float(y_norm)
        if not math.isfinite(x) or not math.isfinite(y):
            raise ValueError('face coordinates must be finite')
        with self.lock:
            self._state['target'] = {'x': max(-1., min(1., x)), 'y': max(-1., min(1., y)),
                                     'visible': bool(visible)}
            if self.config['backend'] == 'serial' and self.controller:
                self.controller.set_target(x,y,visible)
            self._send({'event': 'target', **self._state['target']})

    def begin_utterance(self, utterance_id, text):
        with self.lock:
            self._state['utterance_id'] = str(utterance_id)
            self._send({'event': 'begin', 'utterance_id': str(utterance_id), 'text': str(text)})

    def audio_chunk(self, utterance_id, samples, rate):
        import numpy as np
        audio = np.asarray(samples, dtype='<f4').reshape(-1)
        if rate <= 0 or len(audio) > rate:
            raise ValueError('mouth audio chunks must be at most one second')
        with self.lock:
            self._state['audio_chunks'] += 1
            self.sequence += 1
            self._send({'event': 'audio', 'utterance_id': str(utterance_id), 'sample_rate': int(rate),
                        'sequence': self.sequence, 'pcm_f32le': base64.b64encode(audio.tobytes()).decode()})

    def cancel_utterance(self, utterance_id=None):
        with self.lock:
            uid = str(utterance_id or self._state['utterance_id'] or '')
            self._send({'event': 'cancel', 'utterance_id': uid})
            self._state['utterance_id'] = None

    def end_utterance(self, utterance_id=None):
        self.cancel_utterance(utterance_id)

    def status(self):
        with self.lock:
            state = copy.deepcopy(self._state)
            if self.config['backend'] == 'serial' and self.controller:
                state.update(self.controller.status())
            return state

    def close(self):
        if self.config['backend'] == 'serial':
            if self.controller:
                self.controller.close()
            return
        self.cancel_utterance()
        self.closed.set()
        if self.process:
            try:
                self.process.stdin.close()
                self.process.wait(timeout=2)
            except (OSError, subprocess.TimeoutExpired):
                self.process.terminate()
                try:
                    self.process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    self.process.kill()
                    self.process.wait()
            self.process = None
        with self.lock:
            self._state.update(running=False, motion_enabled=False)
