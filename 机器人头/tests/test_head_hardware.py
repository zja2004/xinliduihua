import math
import unittest
from orin_voice.head_hardware import HeadHardware


class HeadHardwareTests(unittest.TestCase):
    def test_dry_run_tracks_without_starting_hardware(self):
        head = HeadHardware({'backend': 'dry_run'})
        head.start()
        head.track_target(2, -2)
        state = head.status()
        self.assertEqual(state['target'], {'x': 1., 'y': -1., 'visible': True})
        self.assertTrue(state['running'])
        self.assertFalse(state['motion_enabled'])
        self.assertIsNone(head.process)
        head.begin_utterance('u1', '你好')
        self.assertEqual(head.status()['utterance_id'], 'u1')
        head.cancel_utterance('u1')
        self.assertIsNone(head.status()['utterance_id'])
        head.close()
        self.assertFalse(head.status()['running'])

    def test_real_motion_requires_explicit_calibration_configuration(self):
        head = HeadHardware({'backend': 'ros', 'calibration_confirmed': False})
        state = head.start()
        self.assertFalse(state['running'])
        self.assertFalse(state['motion_enabled'])
        self.assertIn('标定', state['error'])
        self.assertIsNone(head.process)

    def test_invalid_visual_coordinates_rejected(self):
        head = HeadHardware()
        for value in (math.nan, math.inf):
            with self.assertRaises(ValueError):
                head.track_target(value, 0)
