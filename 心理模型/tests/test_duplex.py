import io
import threading
import unittest
from unittest.mock import patch

from orin_voice.duplex import Turns, IdlePrompt
from orin_voice.llm import LocalChat


class DuplexTests(unittest.TestCase):
    def test_new_speech_cancels_active_and_queued_replies(self):
        turns = Turns()
        active = turns.cancel
        turns.submit(b'old', 16000)
        turns.interrupt()
        turns.submit(b'new', 16000)
        self.assertTrue(active.is_set())
        self.assertTrue(turns.pending.get_nowait()[2].is_set())
        newest = turns.pending.get_nowait()
        self.assertEqual(newest[:2], (b'new', 16000))
        self.assertFalse(newest[2].is_set())

    def test_backlog_retains_newest_and_is_bounded(self):
        turns = Turns()
        for i in range(100):
            turns.interrupt()
            turns.submit(i, 16000)
        self.assertEqual(turns.pending.qsize(), 2)
        self.assertEqual(turns.pending.get_nowait()[0], 98)
        self.assertEqual(turns.pending.get_nowait()[0], 99)

    def test_cancel_closes_stream_without_playable_result(self):
        cancel = threading.Event()
        cancel.set()
        response = io.BytesIO(b'data: {"choices": [{"delta": {"content": "old"}}]}\n')
        with patch('orin_voice.llm.open_local', return_value=response):
            self.assertIsNone(LocalChat().complete('test', cancel=cancel))
        self.assertTrue(response.closed)


class IdlePromptTests(unittest.TestCase):
    def test_reminders_wait_for_silence_and_are_bounded(self):
        idle = IdlePrompt(interval=20, maximum=2)
        idle.reset(100)
        self.assertFalse(idle.due(119, quiet=True))
        self.assertFalse(idle.due(121, quiet=False))
        self.assertTrue(idle.due(122, quiet=True))
        self.assertFalse(idle.due(123, quiet=True))
        self.assertTrue(idle.due(142, quiet=True))
        self.assertFalse(idle.due(200, quiet=True))
        idle.reset(200)
        self.assertTrue(idle.due(220, quiet=True))
