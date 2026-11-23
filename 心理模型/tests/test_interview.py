import json
import tempfile
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from orin_voice.interview import Interview, intent
from orin_voice.duplex import cancellable
from orin_voice.settings import ROOT


class InterviewTests(unittest.TestCase):
    def test_all_questions_exported_and_no_advance_on_pause_or_repeat(self):
        with tempfile.TemporaryDirectory() as directory:
            session = Interview(ROOT / 'configs/interview.example.json', directory)
            session.accept('最近睡得不好')
            session.accept('有时也会担心考试')
            session.accept('再说一遍')
            self.assertEqual(session.index, 0)
            self.assertEqual(len(session.current['segments']), 2)
            session.accept('回答完了')
            self.assertEqual(session.index, 1)
            for _ in range(20):
                session.accept('跳过这题')
            data = json.loads((session.directory / 'answers.json').read_text())
            self.assertEqual(data['status'], 'completed')
            self.assertEqual(data['completed_count'], 1)
            self.assertEqual(len(data['questions']), 21)
            self.assertIn('考试', data['questions'][0]['answer_text'])
            session.finish_summary('用户提到睡眠与考试方面的困扰。')
            self.assertTrue((session.directory / 'summary.txt').exists())

    def test_control_words_inside_answer_are_data(self):
        self.assertEqual(intent('我不想回答的时候会出去走走'), 'answer')
        self.assertEqual(intent('跳过这题。'), 'skip')

    def test_empty_finish_and_stop_do_not_lose_partial_answer(self):
        with tempfile.TemporaryDirectory() as directory:
            session = Interview(ROOT / 'configs/interview.example.json', directory)
            session.accept('回答完了')
            self.assertEqual(session.index, 0)
            session.accept('这是我的回答')
            session.accept('结束访谈')
            self.assertEqual(session.data['status'], 'stopped')
            self.assertEqual(session.current['answer_text'], '这是我的回答')

    def test_cancel_does_not_wait_for_synthesis(self):
        cancel = threading.Event()
        release = threading.Event()
        def slow():
            release.wait(2)
            return 'obsolete'
        with ThreadPoolExecutor(max_workers=1) as executor:
            threading.Timer(.05, cancel.set).start()
            started = time.monotonic()
            try:
                self.assertIsNone(cancellable(executor, cancel, slow))
                self.assertLess(time.monotonic() - started, .5)
            finally:
                release.set()

    def test_recorded_audio_matches_question(self):
        import numpy as np
        import soundfile as sf
        with tempfile.TemporaryDirectory() as directory:
            session = Interview(ROOT / 'configs/interview.example.json', directory)
            samples = np.zeros(1600, dtype=np.float32)
            session.accept('测试回答', samples, 16000)
            relative = session.current['segments'][0]['audio_path']
            recording, rate = sf.read(session.directory / relative)
            self.assertEqual(rate, 16000)
            self.assertEqual(len(recording), len(samples))

    def test_speech_disk_cache_avoids_repeated_inference(self):
        from types import SimpleNamespace
        from unittest.mock import Mock
        from orin_voice.audio import SpeechSynthesizer
        import numpy as np
        with tempfile.TemporaryDirectory() as directory:
            synth = SpeechSynthesizer.__new__(SpeechSynthesizer)
            synth.speaker = 3
            synth.cache_directory = Path(directory)
            synth.cache_identity = 'test-model-identity'
            synth.engine = Mock()
            synth.engine.generate.return_value = SimpleNamespace(
                samples=np.zeros(2400, dtype=np.float32), sample_rate=24000)
            first, rate = synth.synthesize('已记录2分。')
            second, other_rate = synth.synthesize('已记录2分。')
            self.assertEqual(synth.engine.generate.call_count, 1)
            self.assertEqual(rate, other_rate)
            self.assertEqual(len(first), len(second))
