import unittest

from orin_voice.endpoint import Endpoint


class EndpointTests(unittest.TestCase):
    def test_wait_does_not_accumulate_unbounded_audio(self):
        endpoint = Endpoint()
        for _ in range(10000):
            self.assertIsNone(endpoint.push(b'quiet', False))
        self.assertEqual(endpoint.frames, [])
        self.assertLessEqual(len(endpoint.pre), 10)

    def test_short_pause_preserves_turn_and_silence_finishes(self):
        endpoint = Endpoint(silence_ms=300)
        for _ in range(10):
            self.assertIsNone(endpoint.push(b'voice', True))
        for _ in range(5):
            self.assertIsNone(endpoint.push(b'pause', False))
        for _ in range(10):
            self.assertIsNone(endpoint.push(b'voice', True))
        for _ in range(9):
            self.assertIsNone(endpoint.push(b'end', False))
        self.assertIsNotNone(endpoint.push(b'end', False))

    def test_click_and_brief_noise_are_rejected(self):
        endpoint = Endpoint(silence_ms=300)
        self.assertIsNone(endpoint.push(b'click', True))
        for _ in range(20):
            self.assertIsNone(endpoint.push(b'quiet', False))
        for _ in range(3):
            self.assertIsNone(endpoint.push(b'noise', True))
        for _ in range(20):
            self.assertIsNone(endpoint.push(b'quiet', False))
        self.assertFalse(endpoint.started)

    def test_continuous_sound_is_bounded(self):
        endpoint = Endpoint(max_seconds=2)
        result = None
        for _ in range(67):
            result = endpoint.push(b'voice', True)
            if result is not None:
                break
        self.assertIsNotNone(result)
        self.assertLessEqual(len(result), 66)


if __name__ == '__main__':
    unittest.main()


class AdaptiveSpeechTests(unittest.TestCase):
    def test_soft_short_reply_with_voicing_gap_is_retained(self):
        from orin_voice.endpoint import AdaptiveEnergy
        gate = AdaptiveEnergy(.015)
        endpoint = Endpoint(silence_ms=600, min_speech_ms=120, onset_tolerance_ms=60)
        completed = None
        for rms in [.0002] * 12 + [.005, .005, .0002, .005, .005] + [.0002] * 22:
            value = endpoint.push(rms, gate.voiced(rms, endpoint.started))
            if value is not None:
                completed = value
                break
        self.assertIsNotNone(completed)
        self.assertIn(.005, completed)

    def test_isolated_clicks_do_not_trigger_short_answer_mode(self):
        from orin_voice.endpoint import AdaptiveEnergy
        gate = AdaptiveEnergy()
        endpoint = Endpoint(silence_ms=600, min_speech_ms=120, onset_tolerance_ms=60)
        for _ in range(10):
            for rms in [.02] + [.0001] * 12:
                self.assertIsNone(endpoint.push(rms, gate.voiced(rms, endpoint.started)))
        self.assertFalse(endpoint.started)

    def test_background_tracking_and_release_hysteresis(self):
        from orin_voice.endpoint import AdaptiveEnergy
        gate = AdaptiveEnergy()
        for _ in range(200):
            self.assertFalse(gate.voiced(.001))
        self.assertGreater(gate.threshold, .003)
        self.assertTrue(gate.voiced(.006))
        self.assertTrue(gate.voiced(.0025, active=True))
        self.assertFalse(gate.voiced(.0025, active=False))
