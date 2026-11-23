"""One local camera reader shared by preview and face detection.

The camera thread owns VideoCapture for its whole lifetime. Consumers only wait
for a new immutable snapshot; opening a preview does not arm wake or motion.
"""
from __future__ import annotations

import copy
from dataclasses import dataclass
from datetime import datetime, timezone
import threading
import time
from typing import Any


@dataclass(frozen=True)
class CameraFrame:
    sequence: int
    captured_at: str
    monotonic_time: float
    jpeg: bytes
    frame: Any
    target: Any = None
    detection_count: int = 0


class CameraHub:
    def __init__(self, config=None, *, capture_factory=None, jpeg_encoder=None, detector_factory=None):
        config = config or {}
        self.config = dict(config.get('vision', config))
        source = self.config.get('camera', 0)
        if isinstance(source, str) and source.isdigit():
            source = int(source)
        if isinstance(source, bool) or not isinstance(source, (int, str)):
            raise ValueError('摄像头必须是本机索引或 /dev/video*')
        if isinstance(source, int) and source < 0:
            raise ValueError('摄像头索引不能为负数')
        if isinstance(source, str) and not source.startswith('/dev/video'):
            raise ValueError('摄像头必须是本机索引或 /dev/video*，不读取远端视频地址')
        self.source = source
        self.width = max(160, min(1920, int(self.config.get('width', 640))))
        self.height = max(120, min(1080, int(self.config.get('height', 480))))
        self.fps = max(1., min(30., float(self.config.get('fps', 12))))
        self.rotation = int(self.config.get('rotation', 0))
        if self.rotation not in (0, 90, 180, 270):
            raise ValueError('摄像头旋转角度必须是 0、90、180 或 270')
        self.quality = max(30, min(95, int(self.config.get('jpeg_quality', 80))))
        self.capture_factory = capture_factory
        self.jpeg_encoder = jpeg_encoder
        self.detector_factory = detector_factory
        self.detector_kind = self.config.get('detector', 'none')
        self._condition = threading.Condition(threading.RLock())
        self._stop = threading.Event()
        self._thread = None
        self._latest = None
        self._sequence = 0
        self._state = {'phase': 'stopped', 'error': None, 'camera': self.source,
                       'width': self.width, 'height': self.height, 'requested_fps': self.fps,
                       'fps': 0., 'frames': 0, 'sequence': 0, 'rotation': self.rotation,
                       'detector': self.detector_kind, 'detector_error': None, 'selected_head': None,
                       'detection_count': 0, 'inference_ms': None}

    def _update(self, **fields):
        with self._condition:
            self._state.update(fields)
            self._condition.notify_all()

    def status(self):
        with self._condition:
            result = copy.deepcopy(self._state)
            result['measured_fps'] = result['fps']
            result['running'] = bool(self._thread and self._thread.is_alive())
            result['last_frame_age_ms'] = (round((time.monotonic() - self._latest.monotonic_time)*1000, 1)
                                           if self._latest else None)
            return result

    def is_alive(self):
        with self._condition:
            return bool(self._thread and self._thread.is_alive())

    def start(self):
        with self._condition:
            if self._thread and self._thread.is_alive():
                # Never open a second camera while the previous reader is stopping.
                return self.status()
            self._stop = threading.Event()
            self._latest = None
            self._state.update(phase='opening', error=None, frames=0, fps=0.,
                               selected_head=None, detection_count=0, inference_ms=None, detector_error=None)
            self._thread = threading.Thread(target=self._run, name='shared-camera', daemon=True)
            self._thread.start()
            return self.status()

    def stop(self, timeout=3):
        with self._condition:
            self._stop.set()
            self._latest = None
            if self._thread and self._thread.is_alive():
                self._state.update(phase='stopping')
            else:
                self._state.update(phase='stopped')
            self._condition.notify_all()
            thread = self._thread
        if thread and thread is not threading.current_thread():
            thread.join(timeout=max(0., timeout))
        return self.status()

    def get_frame(self):
        with self._condition:
            return self._latest

    def wait_frame(self, after_sequence=-1, timeout=1):
        deadline = time.monotonic() + max(0., timeout)
        with self._condition:
            while True:
                if self._latest is not None and self._latest.sequence > after_sequence:
                    return self._latest
                if self._stop.is_set() or self._state['phase'] in {'stopped', 'error'}:
                    return None
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return None
                self._condition.wait(remaining)

    def _run(self):
        capture = None
        try:
            import cv2
            detector = None
            if self.detector_kind == 'yolo':
                try:
                    from .yolo_head import YoloHeadDetector
                    factory = self.detector_factory or YoloHeadDetector
                    detector = factory(self.config.get('yolo', {}))
                except Exception as error:
                    self._update(detector_error=str(error))
            factory = self.capture_factory or cv2.VideoCapture
            capture = factory(self.source, cv2.CAP_V4L2)
            if not capture.isOpened():
                raise RuntimeError(f'摄像头 {self.source} 无法打开；请检查 USB/CSI 摄像头及 /dev/video*')
            # Prefer compressed USB transport; unsupported properties are optional.
            capture.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*'MJPG'))
            capture.set(cv2.CAP_PROP_FRAME_WIDTH, self.width)
            capture.set(cv2.CAP_PROP_FRAME_HEIGHT, self.height)
            capture.set(cv2.CAP_PROP_FPS, self.fps)
            capture.set(cv2.CAP_PROP_BUFFERSIZE, 1)
            failures = 0
            frames = 0
            first_frame_at = None
            while not self._stop.is_set():
                started = time.monotonic()
                ok, image = capture.read()
                if self._stop.is_set():
                    break
                if not ok or image is None:
                    failures += 1
                    if failures >= 10:
                        raise RuntimeError('摄像头连续读取失败，请检查连接')
                    self._stop.wait(.1)
                    continue
                failures = 0
                rotations = {90: cv2.ROTATE_90_CLOCKWISE, 180: cv2.ROTATE_180, 270: cv2.ROTATE_90_COUNTERCLOCKWISE}
                if self.rotation:
                    image = cv2.rotate(image, rotations[self.rotation])
                target, detection_count, inference_ms = None, 0, None
                display_image = image
                if detector is not None:
                    try:
                        inference_started = time.monotonic()
                        detections = detector.detect(image)
                        inference_ms = round((time.monotonic() - inference_started) * 1000, 1)
                        detection_count = len(detections)
                        if detections:
                            target = max(detections, key=lambda item: item['confidence'])
                            target = {'bbox': [int(v) for v in target['bbox']],
                                      'confidence': float(target['confidence'])}
                            x, y, width, height = target['bbox']
                            display_image = image.copy()
                            color = (105, 226, 135)
                            cv2.rectangle(display_image, (x,y), (x+width,y+height), color, 2)
                            label = f"HEAD {target['confidence']:.2f}"
                            label_y = max(21, y)
                            label_x = max(0, min(x, image.shape[1]-139))
                            cv2.rectangle(display_image,(label_x,label_y-21),(label_x+139,label_y),color,-1)
                            cv2.putText(display_image,label,(label_x+5,label_y-6),cv2.FONT_HERSHEY_SIMPLEX,.5,(18,38,22),1,cv2.LINE_AA)
                        self._update(detector_error=None)
                    except Exception as error:
                        self._update(detector_error=str(error))
                if self.jpeg_encoder:
                    jpeg = self.jpeg_encoder(display_image, self.quality)
                    if not isinstance(jpeg, bytes) or not jpeg:
                        raise RuntimeError('摄像头 JPEG 编码失败')
                else:
                    encoded_ok, encoded = cv2.imencode('.jpg', display_image,
                        [int(cv2.IMWRITE_JPEG_QUALITY), self.quality])
                    if not encoded_ok:
                        raise RuntimeError('摄像头 JPEG 编码失败')
                    jpeg = encoded.tobytes()
                # No consumer mutates the shared frame. Overlay users should copy it.
                image = image.copy()
                image.setflags(write=False)
                now = time.monotonic()
                first_frame_at = now if first_frame_at is None else first_frame_at
                frames += 1
                height, width = image.shape[:2]
                with self._condition:
                    self._sequence += 1
                    self._latest = CameraFrame(self._sequence,
                        datetime.now(timezone.utc).isoformat(), now, jpeg, image, target, detection_count)
                    self._state.update(phase='streaming', error=None, width=width, height=height,
                        frames=frames, sequence=self._sequence, selected_head=target,
                        detection_count=detection_count, inference_ms=inference_ms,
                        fps=round((frames-1)/max(.001, now-first_frame_at), 1) if frames > 1 else 0.)
                    self._condition.notify_all()
                self._stop.wait(max(0., 1/self.fps-(time.monotonic()-started)))
        except Exception as error:
            with self._condition:
                self._latest = None
                self._state.update(phase='error', error=str(error))
                self._condition.notify_all()
        finally:
            if capture is not None:
                capture.release()
            with self._condition:
                if self._stop.is_set():
                    self._latest = None
                    self._state.update(phase='stopped', error=None, selected_head=None,
                                       detection_count=0, inference_ms=None)
                self._condition.notify_all()
