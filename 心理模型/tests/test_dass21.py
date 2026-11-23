import json
import tempfile
import unittest
from pathlib import Path

from orin_voice.dass21 import SUBSCALES, explicit_score, scores
from orin_voice.interview import Interview
from orin_voice.settings import ROOT


class DassTests(unittest.TestCase):
    def test_only_explicit_unambiguous_scores(self):
        for text, value in [('零分', 0), ('我选一分', 1), ('２分。', 2), ('三', 3),
                            ('我选择二分', 2), ('我的评分是3分', 3)]:
            self.assertEqual(explicit_score(text), value)
        for text in ['最近两天不好', '不是二分，是三分', '一分或两分', '偶尔', '4', '-1',
                     '我有3个朋友', '二分，因为最近很累']:
            self.assertIsNone(explicit_score(text), text)

    def test_subscales_partition_original_order(self):
        self.assertEqual(sorted(i for items in SUBSCALES.values() for i in items), list(range(1, 22)))
        self.assertEqual(SUBSCALES['depression'], (3, 5, 10, 13, 16, 17, 21))
        self.assertEqual(SUBSCALES['anxiety'], (2, 4, 7, 9, 15, 19, 20))
        self.assertEqual(SUBSCALES['stress'], (1, 6, 8, 11, 12, 14, 18))

    def test_zero_and_maximum_and_missing_never_imputed(self):
        questions = [dict(id=f'Q{i:02}', score=3, status='answered') for i in range(1, 22)]
        result = scores(questions)
        self.assertTrue(result['complete'])
        for value in result['subscales'].values():
            self.assertEqual(value['raw_score'], 21)
            self.assertEqual(value['doubled_score'], 42)
        questions[0]['status'] = 'skipped'
        result = scores(questions)
        self.assertIsNone(result['subscales']['stress']['raw_score'])
        self.assertEqual(result['subscales']['stress']['missing_item_ids'], ['Q01'])
        self.assertTrue(result['subscales']['depression']['complete'])
        for q in questions:
            q.update(score=0, status='answered')
        self.assertEqual(scores(questions)['subscales']['depression']['doubled_score'], 0)

    def test_narrative_cannot_be_confirmed_without_score(self):
        with tempfile.TemporaryDirectory() as directory:
            config = json.loads((ROOT / 'configs/dass21.zh.json').read_text())
            config['auto_advance'] = False
            path = Path(directory) / 'manual.json'
            path.write_text(json.dumps(config))
            interview = Interview(path, directory)
            self.assertTrue(interview.is_dass)
            interview.accept('最近有一点累')
            interview.accept('确认')
            self.assertEqual(interview.index, 0)
            interview.accept('二分')
            interview.accept('最近事情多')
            self.assertEqual(interview.current['score'], 2)
            interview.accept('我选一分')
            interview.accept('确认')
            self.assertEqual(interview.index, 1)
            self.assertEqual(interview.data['questions'][0]['score'], 1)
            self.assertEqual(len(interview.data['questions'][0]['segments']), 4)
            self.assertIn('past_week', interview.data['reference_period'])

    def test_full_session_and_skipped_score_export(self):
        with tempfile.TemporaryDirectory() as directory:
            interview = Interview(ROOT / 'configs/dass21.zh.json', directory)
            interview.accept('三分')
            interview.accept('上一题')
            interview.accept('跳过这题')
            for _ in range(20):
                interview.accept('零分')
            interview.finish_summary(interview.basic_summary())
            data = json.loads((interview.directory / 'answers.json').read_text())
            self.assertEqual(data['status'], 'completed')
            self.assertEqual(data['completed_count'], 20)
            self.assertIsNone(data['questions'][0]['score'])
            self.assertFalse(data['scoring']['complete'])
            self.assertEqual(data['scoring']['subscales']['anxiety']['doubled_score'], 0)
            self.assertNotIn('严重', data['overall_summary'])
            with self.assertRaises(ValueError):
                interview.summary_prompt()

    def test_automatic_progression_reads_every_next_question(self):
        with tempfile.TemporaryDirectory() as directory:
            interview = Interview(ROOT / 'configs/dass21.zh.json', directory)
            self.assertIn(interview.current['question'], interview.introduction)
            self.assertNotIn('DASS', interview.introduction)
            for number in range(1, 22):
                reply = interview.accept('二分')
                self.assertEqual(interview.index, number)
                if number < 21:
                    self.assertIn(interview.current['question'], reply)
            self.assertEqual(interview.data['completed_count'], 21)
            for subscale in interview.data['scoring']['subscales'].values():
                self.assertEqual(subscale['raw_score'], 14)
                self.assertEqual(subscale['doubled_score'], 28)

    def test_requests_to_ask_are_not_answers_and_repair_current_prompt(self):
        with tempfile.TemporaryDirectory() as directory:
            interview = Interview(ROOT / 'configs/dass21.zh.json', directory)
            for text in ['你应该主动的去问我问题呀', '你这么不主动', '开始吧', '重复']:
                reply = interview.accept(text)
                self.assertIn(interview.current['question'], reply)
                self.assertEqual(interview.current['segments'], [])
                self.assertEqual(interview.index, 0)
            reply = interview.accept('最近很累')
            self.assertIn(interview.current['question'], reply)
            self.assertIn(interview.options_prompt, reply)

    def test_previous_question_can_be_corrected_without_losing_audio_metadata(self):
        with tempfile.TemporaryDirectory() as directory:
            interview = Interview(ROOT / 'configs/dass21.zh.json', directory)
            interview.accept('二分')
            interview.accept('上一题')
            self.assertEqual(interview.index, 0)
            self.assertEqual(interview.data['completed_count'], 0)
            self.assertIsNone(interview.current['score'])
            self.assertEqual(len(interview.current['segments']), 1)
            interview.accept('零分')
            self.assertEqual(interview.index, 1)
            self.assertEqual(interview.data['questions'][0]['score'], 0)
            self.assertEqual(len(interview.data['questions'][0]['segments']), 2)

    def test_actual_failed_transcript_is_recorded_and_advances(self):
        for text in ['二分二分那变', '二分二分', '2分二分那边', '我觉得二分吧']:
            with tempfile.TemporaryDirectory() as directory:
                interview = Interview(ROOT / 'configs/dass21.zh.json', directory)
                reply = interview.accept(text)
                self.assertEqual(interview.index, 1, text)
                self.assertEqual(interview.data['questions'][0]['score'], 2)
                self.assertEqual(interview.data['questions'][0]['answer_text'], text)
                self.assertIn('已记录2分', reply)
                saved = json.loads((interview.directory / 'answers.json').read_text())
                self.assertEqual(saved['completed_count'], 1)
        for text in ['二分三分', '不是二分', '二分或者三分', '22', '最近两天累']:
            self.assertIsNone(explicit_score(text))

    def test_reply_reuses_whole_question_and_option_cache_units(self):
        with tempfile.TemporaryDirectory() as directory:
            interview = Interview(ROOT / 'configs/dass21.zh.json', directory)
            reply = interview.accept('二分')
            self.assertEqual(interview.speech_parts(reply), ['已记录2分。', interview.prompt])
            reply = interview.accept('最近很累')
            self.assertIn(interview.prompt, interview.speech_parts(reply))
            self.assertIn(interview.options_prompt, interview.speech_parts(reply))
