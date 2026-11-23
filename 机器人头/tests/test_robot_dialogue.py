import unittest

from orin_voice.robot_dialogue import constrain_reply, requests_skip


class RobotDialogueGuardTests(unittest.TestCase):
    def test_skip_intent_is_detected(self):
        self.assertTrue(requests_skip('这个问题我不想回答，可以跳过吗？'))
        self.assertTrue(requests_skip('我想跳过'))
        self.assertFalse(requests_skip('我今天有点难过'))

    def test_reply_is_bounded_to_two_sentences(self):
        reply = constrain_reply('第一句。第二句！第三句。')
        self.assertEqual(reply, '第一句。第二句！')
        self.assertLessEqual(len(constrain_reply('这是一段很长很长很长很长很长很长很长很长很长很长的话。')), 61)
