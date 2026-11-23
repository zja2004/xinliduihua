"""Coordinate standby sensors, wake handover and one active robot encounter."""
import copy
import glob
import json
import queue
import threading
import time
from pathlib import Path

from .settings import ROOT
from .presence import VisionPresence, VoiceWakeListener, load_robot_config
from .robot_dialogue import LocalDialogue
from .camera import CameraHub
from .eye_tracking import EyeTracker


class RobotRuntime:
    def __init__(self, service, config=None, hardware=None):
        self.service = service
        self.config = config or load_robot_config()
        self.hardware = hardware
        self.camera = CameraHub(self.config)
        self.preview_requested = False
        self.tracker = EyeTracker(self.camera,hardware) if hardware else None
        self.dialogue = LocalDialogue(self.config.get('chat', {}))
        self.lock = threading.RLock()
        self.lifecycle_lock = threading.Lock()
        self.voice_reserved = False
        self.generation = 0
        self.enabled = False
        self.stop_event = threading.Event()
        self.triggers = queue.Queue(maxsize=4)
        self.monitor = None
        self.vision = None
        self.voice = None
        self.last_wake = None
        self.last_started = -float('inf')
        self.next_voice_retry = 0.
        self.next_vision_retry = 0.
        self.last_error = None
        self.manual_start_lock = threading.Lock()

    def _event(self, event, generation=None):
        if self.enabled and (generation is None or generation == self.generation):
            try:
                self.triggers.put_nowait(event)
            except queue.Full:
                pass

    def _target(self, x, y, visible):
        # The tracking worker consumes the exact YOLO box shown by the preview.
        # Avoid an independent Haar/smoothed target overwriting it.
        if self.hardware and not self.tracker:
            self.hardware.track_target(x, y, visible=visible > 0)

    def arm(self):
        with self.lifecycle_lock:
            with self.lock:
                if self.enabled:
                    return self.status()
                self.generation += 1
                generation = self.generation
                self.enabled = True
                self.last_error = None
                self.stop_event = stop_event = threading.Event()
                while not self.triggers.empty():
                    try:
                        self.triggers.get_nowait()
                    except queue.Empty:
                        break
                self.dialogue.start()
                if self.config.get('vision', {}).get('enabled', True):
                    self.vision = VisionPresence(self.config,
                        lambda event: self._event(event, generation),
                        lambda x,y,visible: self._target(x,y,visible)
                        if self.enabled and generation == self.generation else None, camera_hub=self.camera)
                    self.vision.start()
                self.monitor = threading.Thread(target=self._watch, args=(stop_event, generation),
                                                name='robot-standby', daemon=True)
                self.monitor.start()
        return self.status()

    def pause_voice(self):
        with self.lock:
            self.voice_reserved = True
            voice = self.voice
            if voice:
                voice.stop()
        if voice:
            voice.join(timeout=6)
            if voice.is_alive():
                with self.lock:
                    self.voice_reserved = False
                raise RuntimeError('待机麦克风尚未关闭，暂不开始访谈，防止同时占用音频')
            with self.lock:
                if self.voice is voice:
                    self.voice = None

    def release_voice(self):
        with self.lock:
            self.voice_reserved = False
            self.next_voice_retry = time.monotonic() + 1

    def _busy(self):
        active = self.service.active
        return bool(self.service.worker and self.service.worker.is_alive() or
                    active and active.read()['status'] == 'in_progress')

    def _watch(self, stop_event=None, generation=None):
        stop_event = stop_event or self.stop_event
        generation = self.generation if generation is None else generation
        while not stop_event.wait(.2):
            try:
                event = self.triggers.get_nowait()
            except queue.Empty:
                event = None
            if event and not self._busy() and time.monotonic() - self.last_started >= self.config.get('wake_cooldown_seconds', 15):
                try:
                    self.wake(event, automatic_generation=generation)
                except Exception as error:
                    self.last_error = str(error)
                    self.next_voice_retry = time.monotonic() + 10
            with self.lock:
                if (self.enabled and self.config.get('vision', {}).get('enabled', True)
                        and self.vision and not self.vision.is_alive() and glob.glob('/dev/video*')
                        and time.monotonic() >= self.next_vision_retry and not stop_event.is_set()):
                    generation_now = self.generation
                    self.vision = VisionPresence(self.config,
                        lambda event: self._event(event, generation_now),
                        lambda x,y,visible: self._target(x,y,visible)
                        if self.enabled and generation_now == self.generation else None, camera_hub=self.camera)
                    self.next_vision_retry = time.monotonic() + 5
                    self.vision.start()
                if (not self.voice_reserved and not self._busy() and self.enabled
                        and self.config.get('voice_wake', {}).get('enabled', True)
                        and time.monotonic() >= self.next_voice_retry and not stop_event.is_set()):
                    if self.voice is None:
                        generation = self.generation
                        self.voice = VoiceWakeListener(self.config,
                            lambda event: self._event(event, generation))
                        self.voice.start()
                    elif not self.voice.is_alive():
                        snapshot = self.voice.status()
                        if snapshot.get('error'):
                            self.last_error = snapshot['error']
                        self.voice = None
                        self.next_voice_retry = time.monotonic() + 10

    def wake(self, event=None, mode=None, automatic_generation=None, school_id=None):
        with self.manual_start_lock:
            if automatic_generation is not None and (
                    not self.enabled or self.stop_event.is_set() or automatic_generation != self.generation):
                return None
            self.last_wake = event or {'trigger': 'manual', 'monotonic_time': time.monotonic()}
            self.last_started = time.monotonic()
            self.dialogue.start()
            mode = mode or self.config.get('session_mode', 'voice')
            portal = getattr(self.service, 'portal', None)
            if portal and school_id is None:
                school_id = portal.access.station_school()
            context = {'school_id':school_id} if school_id else {}
            result = self.service.start(mode, robot=True, workflow=True, wake_event=self.last_wake, **context)
            self.last_error = None
            return result

    def disarm(self):
        with self.lifecycle_lock:
            with self.manual_start_lock:
                with self.lock:
                    self.enabled = False
                    self.generation += 1
                    self.stop_event.set()
            if self.vision:
                self.vision.stop()
                self.vision.join(timeout=5)
            self.pause_voice()
            self.release_voice()
            if (not self.preview_requested and not (self.tracker and self.tracker.is_alive())
                    and not self._recording_in_use()):
                self.camera.stop()
            if self.monitor and self.monitor is not threading.current_thread():
                self.monitor.join(timeout=8)
            while not self.triggers.empty():
                try:
                    self.triggers.get_nowait()
                except queue.Empty:
                    break
            if not self._busy():
                self.dialogue.stop()
        return self.status()

    def tracking_status(self):
        return self.tracker.status() if self.tracker else {'enabled':False,'phase':'unconfigured'}

    def tracking_action(self, action):
        if not self.tracker:
            raise ValueError('人头控制器未配置')
        if action == 'start':
            return self.tracker.start()
        if action == 'stop':
            return self.tracker.stop()
        raise ValueError('眼球跟随操作必须为 start 或 stop')

    def camera_status(self):
        snapshot = self.camera.status()
        snapshot.update(preview_requested=self.preview_requested,
                        vision_in_use=bool(self.vision and self.vision.is_alive()),
                        tracking_in_use=bool(self.tracker and self.tracker.is_alive()),
                        recording_in_use=self._recording_in_use())
        return snapshot

    def _recording_in_use(self):
        recorder = getattr(getattr(self.service, 'active', None), 'video_recorder', None)
        return bool(recorder and recorder.status().get('running'))

    def camera_action(self, action):
        if action == 'start':
            self.preview_requested = True
            self.camera.start()
        elif action == 'stop':
            self.preview_requested = False
            if (not (self.vision and self.vision.is_alive())
                    and not (self.tracker and self.tracker.is_alive())
                    and not self._recording_in_use()):
                self.camera.stop()
        else:
            raise ValueError('摄像头操作必须为 start 或 stop')
        return self.camera_status()

    def status(self):
        with self.lock:
            return {'enabled': self.enabled,
                    'phase': 'active' if self._busy() else ('standby' if self.enabled else 'disabled'),
                    'last_wake': copy.deepcopy(self.last_wake), 'error': self.last_error,
                    'vision': self.vision.status() if self.vision else {'phase': 'stopped'},
                    'voice_wake': self.voice.status() if self.voice else {'phase': 'stopped'},
                    'dialogue': self.dialogue.status(),
                    'hardware': self.hardware.status() if self.hardware else {'mode': 'unconfigured'},
                    'devices': {'cameras': glob.glob('/dev/video*'),
                                'serial': glob.glob('/dev/ttyACM*') + glob.glob('/dev/ttyUSB*')},
                    'camera': self.camera_status(),
                    'eye_tracking': self.tracking_status(),
                    'wake_phrases': self.config.get('voice_wake', {}).get('phrases', [])}

    def shutdown(self):
        if self.tracker:
            self.tracker.stop()
        self.disarm()
        self.dialogue.stop()
        self.camera.stop()
        if self.hardware:
            self.hardware.close()
