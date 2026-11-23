import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from orin_voice.presence import (PresenceGate, TargetSmoother, select_face,
    match_wake_phrase, VisionPresence, VoiceWakeListener, load_robot_config)


class PresenceTests(unittest.TestCase):
    def test_visual_wake_requires_stability_and_departure(self):
        gate = PresenceGate(stable_frames=3, missing_reset_seconds=2, cooldown_seconds=5)
        self.assertFalse(gate.update(True, 0))
        self.assertFalse(gate.update(True, .1))
        self.assertTrue(gate.update(True, .2))
        for now in (1, 2, 8, 100):
            self.assertFalse(gate.update(True, now))
        self.assertFalse(gate.update(False, 100.1))
        self.assertFalse(gate.update(True, 100.2))
        self.assertFalse(gate.armed)
        self.assertFalse(gate.update(False, 102.3))
        self.assertTrue(gate.armed)
        self.assertFalse(gate.update(True, 103))
        self.assertFalse(gate.update(True, 103.1))
        self.assertTrue(gate.update(True, 103.2))

    def test_cooldown_is_not_bypassed_by_short_absence(self):
        gate = PresenceGate(stable_frames=1, missing_reset_seconds=1, cooldown_seconds=10)
        self.assertTrue(gate.update(True, 1))
        gate.update(False, 3)
        self.assertFalse(gate.update(True, 4))
        self.assertTrue(gate.update(True, 11))

    def test_target_is_clamped_smoothed_and_finite(self):
        smoother = TargetSmoother(alpha=.5, max_step=.1, deadband=.01)
        self.assertEqual(smoother.update(4, -3), (.1, -.1))
        for _ in range(20):
            x, y = smoother.update(4, -3)
            self.assertLessEqual(x, 1)
            self.assertGreaterEqual(y, -1)
        with self.assertRaises(ValueError):
            smoother.update(float('nan'), .1)

    def test_largest_face_initially_then_nearest(self):
        self.assertEqual(select_face([(10,10,40,40),(100,100,80,80)],640,480),(100,100,80,80))
        self.assertEqual(select_face([(10,10,40,40),(100,100,80,80)],640,480, previous=(.05,.05)),(10,10,40,40))
        self.assertIsNone(select_face([],640,480))

    def test_wake_phrase_ignores_punctuation_but_not_unrelated_speech(self):
        phrases = ['你好小心', '小心小心', '你好机器人']
        self.assertEqual(match_wake_phrase('你好，小心！', phrases), '你好小心')
        self.assertEqual(match_wake_phrase('你好机器人，开始吧', phrases), '你好机器人')
        self.assertIsNone(match_wake_phrase('我很小心地走路', phrases))

    def test_configuration_does_not_arm_actuators(self):
        config = load_robot_config()
        self.assertFalse(config['enabled'])
        self.assertEqual(config['vision']['camera'], 0)
        self.assertIn('你好机器人',config['voice_wake']['phrases'])

    def test_voice_closes_capture_before_wake_callback(self):
        import numpy as np
        closed = []
        callback_calls = []
        listener = None
        sd = MagicMock()
        sd.query_devices.return_value = {'name':'microphone'}
        def input_stream(**kwargs):
            stream = MagicMock()
            def enter():
                callback = kwargs['callback']
                for level in [.005]*7+[0.]*20:
                    callback(np.full((480,1),level,dtype=np.float32),480,None,None)
                return stream
            stream.__enter__.side_effect=enter
            stream.__exit__.side_effect=lambda *a:closed.append(True)
            return stream
        sd.InputStream.side_effect=input_stream
        recognizer=MagicMock()
        recognizer.transcribe.return_value='你好，小心'
        def wake(event):
            self.assertTrue(closed)
            callback_calls.append(event)
        listener=VoiceWakeListener({'phrases':['你好小心']},wake,recognizer=recognizer)
        with patch('orin_voice.audio.sound_device',return_value=sd):
            listener._run()
        self.assertEqual(callback_calls[0]['trigger'],'voice')
        self.assertEqual(listener.status()['phase'],'awakened')

    def test_failed_camera_reports_error_without_motion(self):
        try:
            import cv2
        except ImportError:
            self.skipTest('OpenCV is optional')
        target=MagicMock()
        worker=VisionPresence({'camera':0},MagicMock(),target)
        capture=MagicMock()
        capture.isOpened.return_value=False
        with patch('cv2.VideoCapture',return_value=capture):
            worker._guard()
        self.assertEqual(worker.status()['phase'],'error')
        self.assertIn('无法打开',worker.status()['error'])
        target.assert_not_called()
        capture.release.assert_called_once()
