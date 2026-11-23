import json
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

import numpy as np

from orin_voice.video_recorder import CameraVideoRecorder


class Clock:
    def __init__(self):
        self.now = 100.

    def __call__(self):
        return self.now


class ScriptedCamera:
    """A CameraHub-shaped source whose capture delays advance a fake clock."""
    def __init__(self, clock, entries=(), stop_at=.5):
        self.clock = clock
        self.entries = list(entries)
        self.origin = clock()
        self.stop_at = self.origin + stop_at
        self.recorder = None
        self.starts = 0
        self.waits = 0

    def start(self):
        self.starts += 1

    def status(self):
        return {'phase': 'streaming'}

    def wait_frame(self, after_sequence=-1, timeout=1):
        self.waits += 1
        deadline = self.clock() + timeout
        if self.entries and self.origin + self.entries[0][0] <= deadline:
            at, level, *stamp = self.entries.pop(0)
            self.clock.now = self.origin + at
            if isinstance(level, Exception):
                raise level
            return SimpleNamespace(sequence=self.waits,
                monotonic_time=self.origin + (stamp[0] if stamp else at),
                frame=np.full((24, 32, 3), level, dtype=np.uint8))
        self.clock.now = min(deadline, self.stop_at)
        if self.clock.now >= self.stop_at:
            self.recorder._stop_monotonic = self.clock()
            self.recorder.stop_event.set()
        return None


class FakeWriter:
    def __init__(self, path, *, opened=True, fail_write=False):
        self.path = Path(path)
        self.path.write_bytes(b'fake-webm-header')
        self.opened = opened
        self.fail_write = fail_write
        self.images = []
        self.releases = 0

    def isOpened(self):
        return self.opened

    def write(self, image):
        if self.fail_write:
            raise OSError('磁盘写入失败')
        self.images.append(image.copy())

    def release(self):
        self.releases += 1


class VideoRecorderTests(unittest.TestCase):
    def run_script(self, directory, entries, *, stop_at=.5, **options):
        clock = Clock()
        camera = ScriptedCamera(clock, entries, stop_at)
        writers = []
        def factory(path, fourcc, fps, size):
            writer = FakeWriter(path)
            writers.append(writer)
            return writer
        recorder = CameraVideoRecorder(camera, directory, fps=10, width=32, height=24,
            clock=clock, wall_clock=lambda: 1000 + clock() - 100,
            writer_factory=options.pop('writer_factory', factory), **options)
        camera.recorder = recorder
        recorder.start()
        recorder.thread.join(timeout=2)
        self.assertFalse(recorder.thread.is_alive(), 'fake camera must terminate without a device')
        return recorder, camera, writers

    def test_camera_stall_repeats_last_frame_without_speeding_up_playback(self):
        with tempfile.TemporaryDirectory() as directory:
            recorder, camera, writers = self.run_script(directory, [(0., 20), (.35, 90)])
            state = recorder.status()
            self.assertTrue(state['complete'])
            self.assertEqual(state['path'], 'video/session.webm')
            self.assertEqual(state['mime_type'], 'video/webm')
            self.assertEqual(state['frames'], 5)
            self.assertEqual(state['source_frames'], 2)
            self.assertEqual(state['duplicated_frames'], 3)
            self.assertAlmostEqual(state['duration_s'], .5)
            self.assertEqual([int(frame[0, 0, 0]) for frame in writers[0].images], [20, 20, 20, 20, 90])
            self.assertEqual(camera.starts, 1)
            self.assertEqual(writers[0].releases, 1)
            manifest = json.loads((Path(directory) / 'video/manifest.json').read_text())
            self.assertTrue(manifest['complete'])
            self.assertFalse(manifest['running'])

    def test_delayed_first_frame_has_explicit_offset_instead_of_fabricated_video(self):
        with tempfile.TemporaryDirectory() as directory:
            recorder, _, _ = self.run_script(directory, [(.2, 30), (.4, 70)])
            state = recorder.status()
            self.assertEqual(state['frames'], 3)
            self.assertAlmostEqual(state['first_frame_offset_s'], .2)
            self.assertAlmostEqual(state['duration_s'], .3)
            self.assertAlmostEqual(state['recording_duration_s'], .5)

    def test_no_frames_and_camera_failure_never_report_complete(self):
        with tempfile.TemporaryDirectory() as directory:
            recorder, _, writers = self.run_script(directory, [], stop_at=1, max_frame_gap_s=.2)
            self.assertFalse(recorder.status()['complete'])
            self.assertEqual(recorder.frames, 0)
            self.assertIn('没有新画面', recorder.error)
            self.assertEqual(writers[0].releases, 1)
        with tempfile.TemporaryDirectory() as directory:
            recorder, _, writers = self.run_script(directory, [(0., 10), (.15, RuntimeError('摄像头断开'))])
            self.assertGreater(recorder.frames, 0)
            self.assertFalse(recorder.status()['complete'])
            self.assertIn('摄像头断开', recorder.error)
            self.assertEqual(writers[0].releases, 1)

    def test_failed_encoder_and_write_are_visible_and_release_resources(self):
        for options in ({'opened': False}, {'fail_write': True}):
            with self.subTest(options=options), tempfile.TemporaryDirectory() as directory:
                writers = []
                def factory(path, *unused):
                    writer = FakeWriter(path, **options)
                    writers.append(writer)
                    return writer
                recorder, _, _ = self.run_script(directory, [(0., 10)], writer_factory=factory)
                self.assertFalse(recorder.status()['complete'])
                self.assertTrue(recorder.error)
                self.assertEqual(writers[0].releases, 1)

    def test_backward_camera_timestamp_is_an_explicit_partial_recording(self):
        with tempfile.TemporaryDirectory() as directory:
            recorder, _, _ = self.run_script(directory, [(.1, 10), (.2, 50, .05)])
            self.assertIn('时间戳发生倒退', recorder.error)
            self.assertFalse(recorder.status()['complete'])

    def test_stop_and_start_are_idempotent_and_never_stop_the_shared_camera(self):
        received = threading.Event()
        written = threading.Event()
        class Camera:
            starts = 0
            sequence = 0
            def start(self):
                self.starts += 1
            def status(self):
                return {'phase': 'streaming'}
            def wait_frame(self, after_sequence=-1, timeout=1):
                if after_sequence < 0:
                    received.set()
                    return SimpleNamespace(sequence=1, monotonic_time=time.monotonic(),
                        frame=np.zeros((24, 32, 3), dtype=np.uint8))
                time.sleep(min(timeout, .02))
                return None
        with tempfile.TemporaryDirectory() as directory:
            camera = Camera()
            writers = []
            def factory(path, *unused):
                writer = FakeWriter(path)
                original_write = writer.write
                def write(image):
                    original_write(image)
                    written.set()
                writer.write = write
                writers.append(writer)
                return writer
            recorder = CameraVideoRecorder(camera, directory, width=32, height=24, writer_factory=factory)
            recorder.start()
            self.assertTrue(received.wait(1))
            self.assertTrue(written.wait(1))
            recorder.start()
            state = recorder.stop(timeout=1)
            self.assertTrue(state['complete'])
            self.assertFalse(state['running'])
            self.assertEqual(state, recorder.stop())
            self.assertEqual(state, recorder.start())
            self.assertEqual(camera.starts, 1)
            self.assertEqual(len(writers), 1)
            self.assertEqual(writers[0].releases, 1)

    def test_real_vp8_webm_encoder_round_trip_with_fake_camera(self):
        import cv2
        with tempfile.TemporaryDirectory() as directory:
            clock = Clock()
            camera = ScriptedCamera(clock, [(0., 20), (.2, 90), (.4, 180)], stop_at=.5)
            recorder = CameraVideoRecorder(camera, directory, fps=10, width=32, height=24,
                                          clock=clock, wall_clock=lambda: 1000 + clock())
            camera.recorder = recorder
            recorder.start()
            recorder.thread.join(timeout=3)
            self.assertTrue(recorder.status()['complete'], recorder.error)
            self.assertEqual(recorder.path.read_bytes()[:4], b'\x1aE\xdf\xa3')  # EBML/WebM
            capture = cv2.VideoCapture(str(recorder.path))
            try:
                self.assertTrue(capture.isOpened())
                self.assertEqual(int(capture.get(cv2.CAP_PROP_FRAME_COUNT)), 5)
                self.assertAlmostEqual(capture.get(cv2.CAP_PROP_FPS), 10)
                decoded = []
                while True:
                    ok, frame = capture.read()
                    if not ok:
                        break
                    decoded.append(frame)
                self.assertEqual(len(decoded), 5)
                self.assertEqual(decoded[0].shape[:2], (24, 32))
            finally:
                capture.release()


if __name__ == '__main__':
    unittest.main()
