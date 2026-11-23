"""Local presence / wake detectors; do not open motors or claim person identity.

Vision emits a damped normalized face centre; the caller owns hardware limits.
Voice runs only while idle and closes its input stream before emitting a wake.
"""
from __future__ import annotations

import copy
import json
import math
import re
import threading
import time
from pathlib import Path
from typing import Callable

from .settings import ROOT


def load_robot_config(path=None):
    return json.loads(Path(path or ROOT / 'configs/robot.json').read_text(encoding='utf-8'))


def normalize_phrase(text):
    return re.sub(r'[\W_]+', '', str(text), flags=re.UNICODE).lower()


def match_wake_phrase(text, phrases):
    """Match a configured phrase inside a short spoken turn, never arbitrary names."""
    normalized = normalize_phrase(text)
    for phrase in phrases:
        phrase = normalize_phrase(phrase)
        if phrase and phrase in normalized:
            return phrase
    return None


class PresenceGate:
    """Require stable frames and departure before another visual wake.

    A brief face-detector dropout does not retrigger the same person. This gate
    has no biometrics: after a real absence, the next stable face is a new event.
    """
    def __init__(self, stable_frames=6, missing_reset_seconds=3, cooldown_seconds=15):
        if stable_frames < 1 or missing_reset_seconds < 0 or cooldown_seconds < 0:
            raise ValueError('Invalid presence timing')
        self.stable_frames = int(stable_frames)
        self.missing_reset_seconds = float(missing_reset_seconds)
        self.cooldown_seconds = float(cooldown_seconds)
        self.seen_frames = 0
        self.armed = True
        self.last_seen = None
        self.last_wake = -math.inf

    def update(self, present, now):
        if present:
            self.seen_frames += 1
            self.last_seen = now
            if self.armed and self.seen_frames >= self.stable_frames and now - self.last_wake >= self.cooldown_seconds:
                self.last_wake = now
                self.armed = False
                return True
        else:
            self.seen_frames = 0
            if self.last_seen is not None and now - self.last_seen >= self.missing_reset_seconds:
                self.armed = True
        return False

    def rearm(self):
        """Explicit rearm for an operator; runtime should normally wait for absence."""
        self.seen_frames = 0
        self.armed = True


class TargetSmoother:
    def __init__(self, alpha=.3, max_step=.12, deadband=.015):
        if not 0 < alpha <= 1 or not 0 < max_step <= 2 or deadband < 0:
            raise ValueError('Invalid target smoothing')
        self.alpha, self.max_step, self.deadband = alpha, max_step, deadband
        self.target = None

    def update(self, x, y):
        if not math.isfinite(x) or not math.isfinite(y):
            raise ValueError('Target coordinates must be finite')
        x, y = max(-1., min(1., x)), max(-1., min(1., y))
        if self.target is None:
            self.target = (0., 0.)
        values = []
        for previous, requested in zip(self.target, (x, y)):
            delta = (requested - previous) * self.alpha
            if abs(requested - previous) <= self.deadband:
                delta = 0.
            values.append(previous + max(-self.max_step, min(self.max_step, delta)))
        self.target = tuple(values)
        return self.target


def select_face(faces, width, height, previous=None):
    """Prefer continuity, then apparent size. No face identity is inferred."""
    candidates = [tuple(map(int, face)) for face in faces if face[2] > 0 and face[3] > 0]
    if not candidates:
        return None
    if previous is None:
        return max(candidates, key=lambda face: face[2] * face[3])
    px, py = previous
    def rank(face):
        x, y, w, h = face
        centre = ((x + w/2) / width, (y + h/2) / height)
        distance = (centre[0] - px)**2 + (centre[1] - py)**2
        return distance - .15 * w*h/(width*height)
    return min(candidates, key=rank)


class _Worker:
    def __init__(self, on_status=None):
        self._stop = threading.Event()
        self._thread = None
        self._lock = threading.Lock()
        self._state = {'phase': 'stopped', 'error': None}
        self.on_status = on_status

    def _status(self, **values):
        with self._lock:
            self._state.update(values)
            snapshot = copy.deepcopy(self._state)
        if self.on_status:
            self.on_status(snapshot)

    def status(self):
        with self._lock:
            return copy.deepcopy(self._state)

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._guard, name=type(self).__name__, daemon=True)
        self._thread.start()

    def _guard(self):
        try:
            self._run()
        except Exception as error:
            self._status(phase='error', error=str(error))

    def stop(self):
        self._stop.set()

    def join(self, timeout=None):
        if self._thread and self._thread is not threading.current_thread():
            self._thread.join(timeout)

    def is_alive(self):
        return bool(self._thread and self._thread.is_alive())


class VisionPresence(_Worker):
    def __init__(self, config, on_wake: Callable, on_target: Callable, on_status=None, camera_hub=None):
        super().__init__(on_status)
        self.config = dict(config.get('vision', config))
        self.camera_hub = camera_hub
        self.on_wake, self.on_target = on_wake, on_target
        self.gate = PresenceGate(self.config.get('stable_frames', 6),
                                 self.config.get('missing_reset_seconds', 3),
                                 self.config.get('cooldown_seconds', 15))
        self.smoother = TargetSmoother(self.config.get('target_smoothing', .3),
                                       self.config.get('target_max_step', .12),
                                       self.config.get('target_deadband', .015))

    def _run(self):
        import cv2
        cfg = self.config
        if not cfg.get('enabled', True):
            self._status(phase='disabled')
            return
        cascade_path = cfg.get('cascade_path') or str(Path(cv2.data.haarcascades) / 'haarcascade_frontalface_default.xml')
        use_yolo = bool(self.camera_hub and cfg.get('detector') == 'yolo')
        cascade = None if use_yolo else cv2.CascadeClassifier(cascade_path)
        if cascade is not None and cascade.empty():
            raise RuntimeError('人脸模型无法加载：' + cascade_path)
        camera = cfg.get('camera', 0)
        if isinstance(camera, str) and camera.isdigit():
            camera = int(camera)
        if isinstance(camera, str) and not camera.startswith('/dev/video'):
            raise ValueError('摄像头必须是本机索引或 /dev/video*，不读取远端视频地址')
        self._status(phase='opening', camera=camera)
        capture = None if self.camera_hub else cv2.VideoCapture(camera, cv2.CAP_V4L2)
        target_visible = False
        try:
            if self.camera_hub:
                self.camera_hub.start()
            else:
                if not capture.isOpened():
                    raise RuntimeError(f'摄像头 {camera} 无法打开；请检查 USB/CSI 摄像头及 /dev/video*')
                capture.set(cv2.CAP_PROP_FRAME_WIDTH, int(cfg.get('width', 640)))
                capture.set(cv2.CAP_PROP_FRAME_HEIGHT, int(cfg.get('height', 480)))
            fps = max(1., min(30., float(cfg.get('fps', 12))))
            if capture is not None:
                capture.set(cv2.CAP_PROP_FPS, fps)
                capture.set(cv2.CAP_PROP_BUFFERSIZE, 1)
            self._status(phase='watching', error=None, face_present=False, frames=0)
            previous = None
            missing = 0
            frame_count = 0
            camera_sequence = -1
            while not self._stop.is_set():
                started = time.monotonic()
                if self.camera_hub:
                    snapshot = self.camera_hub.wait_frame(camera_sequence, timeout=.25)
                    if snapshot is None:
                        state = self.camera_hub.status()
                        if state['phase'] == 'error':
                            raise RuntimeError(state.get('error') or '摄像头读取失败')
                        if state['phase'] == 'stopped':
                            break
                        continue
                    camera_sequence = snapshot.sequence
                    frame = snapshot.frame
                    ok = frame is not None
                else:
                    ok, frame = capture.read()
                if self._stop.is_set():
                    break
                if not ok or frame is None:
                    missing += 1
                    if missing >= 10:
                        raise RuntimeError('摄像头连续读取失败，请检查连接')
                    self._stop.wait(.1)
                    continue
                missing = 0
                height, width = frame.shape[:2]
                confidence = 1.0
                if use_yolo:
                    selected = snapshot.target
                    face = tuple(selected['bbox']) if selected else None
                    confidence = selected['confidence'] if selected else 0.
                    faces = [face] if face else []
                else:
                    gray = cv2.equalizeHist(cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY))
                    faces = cascade.detectMultiScale(gray, scaleFactor=float(cfg.get('scale_factor', 1.15)),
                        minNeighbors=int(cfg.get('min_neighbors', 5)),
                        minSize=(int(cfg.get('minimum_face_pixels', 70)),) * 2)
                    face = select_face(faces, width, height, previous)
                now = time.monotonic()
                frame_count += 1
                wake = self.gate.update(face is not None, now)
                details = dict(phase='watching', face_present=face is not None, frames=frame_count,
                               armed=self.gate.armed, face_count=len(faces))
                if face is not None:
                    x, y, w, h = face
                    previous = ((x+w/2)/width, (y+h/2)/height)
                    nx, ny = previous[0]*2-1, previous[1]*2-1
                    if cfg.get('mirror_x', False):
                        nx = -nx
                    tx, ty = self.smoother.update(nx, ny)
                    # Haar has no calibrated probability; 1.0 signals a valid detection.
                    self.on_target(tx, ty, confidence)
                    target_visible = True
                    details.update(target_x=tx, target_y=ty, confidence=confidence, bbox=[x/width,y/height,w/width,h/height])
                    if wake:
                        self.on_wake(dict(trigger='vision', monotonic_time=now,
                                          bbox=details['bbox'], target=[tx, ty], detector='yolov8n_face' if use_yolo else 'haar_frontal_face', confidence=confidence))
                else:
                    if target_visible:
                        self.on_target(0., 0., 0.)
                        target_visible = False
                    if self.gate.armed:
                        previous = None
                self._status(**details)
                self._stop.wait(max(0., 1/fps - (time.monotonic()-started)))
        finally:
            if capture is not None:
                capture.release()
            if target_visible:
                self.on_target(0., 0., 0.)
            if self._stop.is_set() or self.camera_hub and self.camera_hub.status()['phase'] == 'stopped':
                self._status(phase='stopped')


class VoiceWakeListener(_Worker):
    def __init__(self, config, on_wake: Callable, recognizer=None, on_status=None):
        super().__init__(on_status)
        self.config = dict(config.get('voice_wake', config))
        self.on_wake = on_wake
        self.recognizer = recognizer

    def _run(self):
        import numpy as np
        from .audio import SpeechRecognizer, sound_device
        from .endpoint import AdaptiveEnergy, Endpoint
        cfg = self.config
        if not cfg.get('enabled', True):
            self._status(phase='disabled')
            return
        phrases = cfg.get('phrases', ['你好小心', '小心小心', '你好机器人'])
        if not phrases or any(not normalize_phrase(p) for p in phrases):
            raise ValueError('至少配置一个非空唤醒词')
        self._status(phase='loading', phrases=list(phrases))
        recognizer = self.recognizer or SpeechRecognizer()
        self.recognizer = recognizer
        sd = sound_device()
        device = cfg.get('device')
        info = sd.query_devices(device, kind='input')
        name = str(info.get('name', ''))
        if cfg.get('reject_monitor_sources', True) and '.monitor' in name.lower():
            raise ValueError('待机唤醒不能使用扬声器 monitor 源')
        rate = int(cfg.get('sample_rate', 16000))
        block = round(rate * .03)
        frames = []
        errors = []
        completed = threading.Event()
        gate = AdaptiveEnergy(float(cfg.get('speech_threshold', .015)))
        def new_endpoint():
            return Endpoint(silence_ms=int(cfg.get('silence_ms', 500)), min_speech_ms=120,
                            max_seconds=max(2, int(cfg.get('max_seconds', 5))), onset_tolerance_ms=60)
        endpoint = new_endpoint()
        wake_event = None
        last_capture = [time.monotonic()]
        def capture(samples, count, timing, status):
            nonlocal endpoint
            last_capture[0] = time.monotonic()
            if self._stop.is_set() or completed.is_set():
                return
            if status:
                errors.append(str(status))
                completed.set()
                return
            frame = samples[:, 0].copy()
            rms = float(np.sqrt(np.mean(frame * frame)))
            segment = endpoint.push(frame, gate.voiced(rms, endpoint.started))
            if segment is not None:
                frames.extend(segment)
                completed.set()
        self._status(phase='listening', device=name, sample_rate=rate, error=None)
        with sd.InputStream(device=device, samplerate=rate, channels=1, dtype='float32',
                            blocksize=block, callback=capture):
            while not self._stop.is_set():
                if not completed.wait(.1):
                    if time.monotonic() - last_capture[0] > 3:
                        raise RuntimeError('待机麦克风已超过三秒没有传入数据，请检查输入设备。')
                    continue
                if errors:
                    raise RuntimeError('待机录音异常：' + '; '.join(errors))
                if not frames:
                    completed.clear()
                    continue
                self._status(phase='recognizing')
                text = recognizer.transcribe(np.concatenate(frames), rate)
                phrase = match_wake_phrase(text, phrases)
                if phrase:
                    wake_event = dict(trigger='voice', monotonic_time=time.monotonic(),
                                      phrase=phrase, transcript=text)
                    break
                frames.clear()
                endpoint = new_endpoint()
                completed.clear()
                self._status(phase='listening')
        # Stream is closed now. Runtime can start the interview without double capture.
        if wake_event and not self._stop.is_set():
            self._status(phase='awakened', phrase=wake_event['phrase'])
            self.on_wake(wake_event)
        elif self._stop.is_set():
            self._status(phase='stopped')
