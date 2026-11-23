"""Record browser-playable VP8/WebM from the shared CameraHub.

Only CameraHub owns VideoCapture.  This consumer samples its frames on a
constant-rate monotonic timeline, repeating the last frame through short camera
stalls instead of shortening the recorded encounter during playback.
"""
from __future__ import annotations

import json
import math
import os
import threading
import time
from pathlib import Path


class CameraVideoRecorder:
    def __init__(self, camera, directory, *, fps=12, width=640, height=480,
                 clock=None, wall_clock=None, writer_factory=None,
                 max_frame_gap_s=3., origin=None):
        self.camera = camera
        self.directory = Path(directory)
        self.video_dir = self.directory / 'video'
        self.video_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.fps = float(fps)
        if not math.isfinite(self.fps) or not 1 <= self.fps <= 30:
            raise ValueError('录像帧率必须为 1–30')
        self.width, self.height = int(width), int(height)
        if not 1 <= self.width <= 1920 or not 1 <= self.height <= 1080:
            raise ValueError('录像分辨率无效')
        self.max_frame_gap_s = float(max_frame_gap_s)
        if not math.isfinite(self.max_frame_gap_s) or self.max_frame_gap_s <= 0:
            raise ValueError('摄像头超时必须大于零')
        self.clock, self.wall_clock = clock or time.monotonic, wall_clock or time.time
        self.writer_factory = writer_factory
        self.origin = origin
        self.lock = threading.RLock()
        self.stop_event = threading.Event()
        self.thread = None
        self.error = None
        self.frames = 0
        self.source_frames = 0
        self.duplicated_frames = 0
        self.started_at = self.ended_at = None
        self._started_monotonic = self._ended_monotonic = self._stop_monotonic = None
        self._first_frame_monotonic = None
        self._closed = False
        self.path = self.video_dir / 'session.webm'

    def start(self):
        with self.lock:
            # A recorder belongs to exactly one encounter. Repeated calls never
            # truncate an already completed or currently active recording.
            if self.started_at is not None:
                return self.status()
            self.started_at, self._started_monotonic = self.wall_clock(), self.clock()
            if self.origin is None:
                self.origin = self._started_monotonic
            try:
                self.camera.start()
            except Exception as error:
                self.error = str(error)
                self.ended_at, self._ended_monotonic = self.wall_clock(), self.clock()
                self._closed = True
                try:
                    self._write_manifest()
                except OSError as manifest_error:
                    self.error += '；无法保存录像清单：' + str(manifest_error)
                return self.status()
            self.thread = threading.Thread(target=self._run, name='session-video-recorder', daemon=True)
            self.thread.start()
            return self.status()

    def _run(self):
        writer = None
        sequence = -1
        latest_image = None
        latest_used = False
        last_capture = None
        try:
            import cv2
            factory = self.writer_factory or cv2.VideoWriter
            # VP8/WebM is directly playable in Chromium. The local OpenCV
            # FFmpeg backend was verified to encode VP80 without ffmpeg CLI.
            writer = factory(str(self.path), cv2.VideoWriter_fourcc(*'VP80'),
                             self.fps, (self.width, self.height))
            if not writer.isOpened():
                raise RuntimeError('无法创建 VP8/WebM 录像，请检查 OpenCV 编码器')
            if self.path.exists():
                self.path.chmod(0o600)

            def write_until(timestamp, *, inclusive):
                nonlocal latest_used
                if latest_image is None:
                    return
                limit = (timestamp - self._first_frame_monotonic) * self.fps
                while self.frames < limit - 1e-7 or inclusive and self.frames <= limit + 1e-7:
                    if writer.write(latest_image) is False:
                        raise RuntimeError('录像写入失败')
                    with self.lock:
                        self.frames += 1
                        if latest_used:
                            self.duplicated_frames += 1
                    latest_used = True

            while not self.stop_event.is_set():
                now = self.clock()
                next_frame = (self._first_frame_monotonic + self.frames / self.fps
                              if self._first_frame_monotonic is not None else now + .25)
                timeout = min(.25, max(.001, next_frame - now))
                snapshot = self.camera.wait_frame(after_sequence=sequence, timeout=timeout)
                now = self.clock()
                if self.stop_event.is_set():
                    break
                state = self.camera.status()
                if state.get('phase') == 'error':
                    raise RuntimeError(state.get('error') or '摄像头读取失败')
                if state.get('phase') == 'stopped':
                    raise RuntimeError('摄像头已停止，录像已结束')
                if snapshot is not None:
                    timestamp = float(getattr(snapshot, 'monotonic_time', now))
                    if not math.isfinite(timestamp):
                        raise RuntimeError('摄像头帧时间戳无效')
                    timestamp = min(now, max(self._started_monotonic, timestamp))
                    if last_capture is not None and timestamp < last_capture:
                        raise RuntimeError('摄像头帧时间戳发生倒退')
                    if last_capture is not None and timestamp - last_capture > self.max_frame_gap_s:
                        write_until(last_capture + self.max_frame_gap_s, inclusive=False)
                        raise RuntimeError('摄像头长时间没有新画面，录像不完整')
                    # Fill earlier output slots with the image actually available
                    # then. A newly arrived frame must not overwrite the past.
                    write_until(timestamp, inclusive=False)
                    sequence = snapshot.sequence
                    image = snapshot.frame
                    if (image is None or getattr(image, 'ndim', 0) != 3 or image.shape[2] != 3
                            or str(getattr(image, 'dtype', '')) != 'uint8'):
                        raise RuntimeError('摄像头帧格式无效')
                    if image.shape[1] != self.width or image.shape[0] != self.height:
                        image = cv2.resize(image, (self.width, self.height), interpolation=cv2.INTER_AREA)
                    latest_image, latest_used = image, False
                    last_capture = timestamp
                    with self.lock:
                        self.source_frames += 1
                        if self._first_frame_monotonic is None:
                            self._first_frame_monotonic = timestamp
                last_seen = last_capture if last_capture is not None else self._started_monotonic
                if now - last_seen > self.max_frame_gap_s:
                    write_until(last_seen + self.max_frame_gap_s, inclusive=False)
                    raise RuntimeError('摄像头长时间没有新画面，录像不完整')
                write_until(now, inclusive=True)
            # Keep the final short gap on the same timeline, without adding a
            # frame whose presentation time is exactly at or after stop().
            end = self._stop_monotonic if self._stop_monotonic is not None else self.clock()
            write_until(end, inclusive=False)
        except Exception as error:
            with self.lock:
                self.error = self.error or str(error)
        finally:
            if writer is not None:
                try:
                    writer.release()
                except Exception as error:
                    self.error = self.error or str(error)
            try:
                if self.frames and (not self.path.is_file() or not self.path.stat().st_size):
                    self.error = self.error or '录像编码器没有生成有效文件'
            except OSError as error:
                self.error = self.error or '无法读取录像文件：' + str(error)
            with self.lock:
                self.ended_at, self._ended_monotonic = self.wall_clock(), self.clock()
                self._closed = True
            try:
                self._write_manifest()
            except OSError as error:
                with self.lock:
                    self.error = self.error or '无法保存录像清单：' + str(error)

    def _write_manifest(self):
        temporary = self.video_dir / 'manifest.tmp'
        with temporary.open('w', encoding='utf-8') as file:
            os.chmod(temporary, 0o600)
            json.dump(self.status(), file, ensure_ascii=False, indent=2)
            file.flush()
            os.fsync(file.fileno())
        temporary.replace(self.video_dir / 'manifest.json')

    def stop(self, timeout=15):
        with self.lock:
            if self.started_at is None or self._closed:
                return self.status()
            if not self.stop_event.is_set():
                self._stop_monotonic = self.clock()
                self.stop_event.set()
            thread = self.thread
        if thread and thread is not threading.current_thread():
            thread.join(timeout=max(0., timeout))
        if thread and thread is not threading.current_thread() and thread.is_alive():
            with self.lock:
                self.error = self.error or '录像线程未能在规定时间内停止'
        return self.status()

    def status(self):
        with self.lock:
            return {'path': 'video/session.webm', 'mime_type': 'video/webm', 'codec': 'VP8',
                    'frames': self.frames, 'source_frames': self.source_frames,
                    'duplicated_frames': self.duplicated_frames,
                    'fps': self.fps, 'duration_s': self.frames / self.fps,
                    'running': bool(self.thread and self.thread.is_alive() and not self._closed),
                    'complete': bool(self._closed and not self.error and self.frames > 0),
                    'error': self.error, 'started_at': self.started_at, 'ended_at': self.ended_at,
                    'first_frame_offset_s': (self._first_frame_monotonic - self.origin
                                             if self._first_frame_monotonic is not None else None),
                    'recording_duration_s': (self._ended_monotonic - self._started_monotonic
                                             if self._ended_monotonic is not None else None),
                    'timing': 'constant-rate VP8; short camera gaps repeat the last available frame'}
