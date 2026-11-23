import threading
import time
import unittest
from unittest.mock import MagicMock, patch

import numpy as np

from orin_voice.camera import CameraHub
from orin_voice.presence import VisionPresence


class FakeCapture:
    def __init__(self, opened=True, image=None):
        self.opened = opened
        self.image = np.zeros((24, 32, 3), dtype=np.uint8) if image is None else image
        self.reads = 0
        self.releases = 0
        self.properties = []

    def isOpened(self):
        return self.opened

    def set(self, prop, value):
        self.properties.append((prop, value))
        return True

    def read(self):
        self.reads += 1
        return True, self.image.copy()

    def release(self):
        self.releases += 1


def wait_until(predicate, timeout=2):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(.01)
    raise AssertionError('condition not satisfied before timeout')


class CameraHubTests(unittest.TestCase):
    def test_multiple_consumers_and_idempotent_start_share_one_capture(self):
        capture = FakeCapture()
        factory = MagicMock(return_value=capture)
        hub = CameraHub({'fps': 1}, capture_factory=factory)
        self.addCleanup(hub.stop)
        hub.start()
        hub.start()
        first = hub.wait_frame(timeout=2)
        self.assertIsNotNone(first)
        shared = []
        def read():
            shared.append(hub.wait_frame(first.sequence-1, timeout=1))
        threads = [threading.Thread(target=read) for _ in range(4)]
        for thread in threads: thread.start()
        for thread in threads: thread.join(1)
        self.assertEqual(len(shared), 4)
        self.assertTrue(all(frame is first for frame in shared))
        self.assertTrue(first.jpeg.startswith(b'\xff\xd8'))
        self.assertFalse(first.frame.flags.writeable)
        factory.assert_called_once()
        hub.stop()
        self.assertEqual(capture.releases, 1)
        self.assertIsNone(hub.get_frame())
        self.assertIsNone(hub.wait_frame(timeout=.1))

    def test_stop_wakes_waiter_and_restart_uses_new_sequence(self):
        captures = [FakeCapture(), FakeCapture()]
        factory = MagicMock(side_effect=captures)
        hub = CameraHub({'fps': 1}, capture_factory=factory)
        self.addCleanup(hub.stop)
        hub.start()
        first = hub.wait_frame(timeout=2)
        waited = []
        thread = threading.Thread(target=lambda: waited.append(hub.wait_frame(first.sequence+100, timeout=10)))
        thread.start()
        hub.stop()
        thread.join(1)
        self.assertEqual(waited, [None])
        self.assertFalse(hub.is_alive())
        hub.start()
        next_frame = hub.wait_frame(after_sequence=first.sequence, timeout=2)
        self.assertGreater(next_frame.sequence, first.sequence)
        self.assertEqual(factory.call_count, 2)
        self.assertEqual(captures[0].releases, 1)

    def test_open_or_encode_failure_releases_capture_and_reports_error(self):
        for capture, encoder, message in [
                (FakeCapture(opened=False), None, '无法打开'),
                (FakeCapture(), lambda image, quality: b'', 'JPEG 编码失败')]:
            with self.subTest(message=message):
                hub = CameraHub({}, capture_factory=lambda *args: capture, jpeg_encoder=encoder)
                self.addCleanup(hub.stop)
                hub.start()
                wait_until(lambda: hub.status()['phase'] == 'error' and not hub.is_alive())
                self.assertIn(message, hub.status()['error'])
                self.assertIsNone(hub.get_frame())
                self.assertIsNone(hub.wait_frame(timeout=10))
                self.assertEqual(capture.releases, 1)

    def test_rotation_applies_to_both_detection_frame_and_preview(self):
        image = np.zeros((24, 32, 3), dtype=np.uint8)
        image[0, 0] = [0, 100, 200]
        hub = CameraHub({'rotation': 180}, capture_factory=lambda *args: FakeCapture(image=image))
        self.addCleanup(hub.stop)
        hub.start()
        frame = hub.wait_frame(timeout=2)
        self.assertTrue(np.array_equal(frame.frame[-1,-1], [0, 100, 200]))
        self.assertEqual(hub.status()['rotation'], 180)
        with self.assertRaises(ValueError):
            CameraHub({'rotation': 45})

    def test_preview_and_detector_share_capture_and_detector_does_not_stop_hub(self):
        capture = FakeCapture()
        factory = MagicMock(return_value=capture)
        hub = CameraHub({'fps': 20}, capture_factory=factory)
        self.addCleanup(hub.stop)
        hub.start()
        hub.wait_frame(timeout=2)
        cascade = MagicMock()
        cascade.empty.return_value = False
        cascade.detectMultiScale.return_value = []
        worker = VisionPresence({'fps': 20}, MagicMock(), MagicMock(), camera_hub=hub)
        with patch('cv2.CascadeClassifier', return_value=cascade):
            worker.start()
            wait_until(lambda: worker.status().get('frames', 0) >= 2)
            before = hub.get_frame().sequence
            worker.stop()
            worker.join(2)
            next_frame = hub.wait_frame(before, timeout=2)
        self.assertIsNotNone(next_frame)
        self.assertFalse(worker.is_alive())
        self.assertTrue(hub.is_alive())
        self.assertEqual(capture.releases, 0)
        factory.assert_called_once()


class YoloCameraTests(unittest.TestCase):
    def test_preview_draws_one_highest_confidence_target_and_keeps_raw_frame(self):
        detector = MagicMock()
        detector.detect.return_value = [dict(bbox=[1, 1, 22, 20], confidence=.62),
                                       dict(bbox=[12, 7, 8, 8], confidence=.93)]
        hub = CameraHub({'fps': 1, 'detector':'yolo'},
                        capture_factory=lambda *args: FakeCapture(),
                        detector_factory=lambda cfg: detector)
        self.addCleanup(hub.stop)
        with patch('cv2.rectangle', wraps=__import__('cv2').rectangle) as draw:
            hub.start()
            frame = hub.wait_frame(timeout=2)
            self.assertIsNotNone(frame)
            self.assertEqual(frame.target['bbox'], [12, 7, 8, 8])
            self.assertAlmostEqual(frame.target['confidence'], .93)
            self.assertEqual(frame.detection_count, 2)
            # One outline plus one label background, never two target outlines.
            self.assertEqual(draw.call_count, 2)
            self.assertEqual(int(frame.frame.max()), 0)
            self.assertEqual(hub.status()['selected_head'], frame.target)

    def test_no_detections_clears_previous_target(self):
        detector = MagicMock()
        detector.detect.side_effect = [[dict(bbox=[2, 3, 10, 10], confidence=.9)], []]
        hub = CameraHub({'fps': 8, 'detector':'yolo'},
                        capture_factory=lambda *args: FakeCapture(),
                        detector_factory=lambda cfg: detector)
        self.addCleanup(hub.stop)
        hub.start()
        first = hub.wait_frame(timeout=2)
        second = hub.wait_frame(first.sequence, timeout=2)
        self.assertIsNone(second.target)
        self.assertEqual(second.detection_count, 0)
        self.assertIsNone(hub.status()['selected_head'])
