import copy
import json
import unittest
from pathlib import Path

from orin_voice.primary21 import (INSTRUMENT, LABELS, RESPONSE_OPTIONS, SUBSCALES,
                                  TASK_RATINGS, comparison, explicit_score, scores, task_scores)
from orin_voice.settings import ROOT


def answered_questions(value=0):
    return [dict(id=f'Q{number:02}', status='answered', score=value) for number in range(1, 22)]


class PrimaryConfigTests(unittest.TestCase):
    def test_config_has_21_distinct_direct_scored_topics_and_separate_identity(self):
        config = json.loads((ROOT / 'configs/primary21.zh.json').read_text(encoding='utf-8'))
        self.assertEqual(config['instrument'], 'PRIMARY-21')
        self.assertEqual(config['reference_period'], 'past_week')
        self.assertEqual(config['validation_status'], 'unvalidated_project_authored')
        self.assertEqual(config['response_options'], RESPONSE_OPTIONS)
        self.assertEqual([q['id'] for q in config['questions']], [f'Q{i:02}' for i in range(1, 22)])
        self.assertEqual(len({q['text'] for q in config['questions']}), 21)
        for key, numbers in SUBSCALES.items():
            expected = [f'Q{number:02}' for number in numbers]
            self.assertEqual(config['subscales'][key]['item_ids'], expected)
            self.assertEqual([q['id'] for q in config['questions'] if q['dimension'] == key], expected)
        forbidden = ['绝望', '不配做人', '生命毫无意义', '患有', '抑郁症', '焦虑症', '恐慌', '不正常']
        for question in config['questions']:
            self.assertTrue(question['text'].strip())
            self.assertTrue(question['topic'].strip())
            self.assertTrue(all(word not in question['text'] for word in forbidden))
        self.assertEqual(config['scoring']['multiplier'], 1)
        self.assertIsNone(config['scoring']['clinical_cutoffs'])

    def test_subscales_partition_21_positions_without_reversing_any_score(self):
        self.assertEqual(sorted(i for items in SUBSCALES.values() for i in items), list(range(1, 22)))
        self.assertEqual(SUBSCALES['mood'], (3, 5, 10, 13, 16, 17, 21))
        self.assertEqual(SUBSCALES['worry'], (2, 4, 7, 9, 15, 19, 20))
        self.assertEqual(SUBSCALES['tension'], (1, 6, 8, 11, 12, 14, 18))
        self.assertEqual(set(LABELS), set(SUBSCALES))
        self.assertEqual({item['key'] for item in TASK_RATINGS}, set(SUBSCALES))
        self.assertTrue(all('过去一周' in item['text'] for item in TASK_RATINGS))


class PrimaryExplicitScoreTests(unittest.TestCase):
    def test_exact_frequency_labels_and_explicit_matching_numbers(self):
        cases = [('没有', 0), ('偶尔', 1), ('有时', 2), ('经常', 3),
                 ('零分。', 0), ('我选一分', 1), ('我选择２分', 2), ('我的评分是3分', 3),
                 ('我选没有', 0), ('选择偶尔', 1), ('二分，有时', 2), ('经常，三分', 3)]
        for text, expected in cases:
            self.assertEqual(explicit_score(text), expected, text)

    def test_narratives_conflicts_negations_and_unconfirmed_asr_are_not_scores(self):
        for text in [None, 2, '', '不想回答', '不知道', '不是二分，是三分', '没有朋友',
                     '我有3个朋友', '最近两天难过', '经常担心考试', '一分，因为今天很累',
                     '偶尔或者有时', '有时经常', '不经常', '三分没有', '22', '-1', '4',
                     '二分二分', '我觉得二分吧', '二分二分那边', '完全没有感觉到开心']:
            self.assertIsNone(explicit_score(text), text)


class PrimaryScoringTests(unittest.TestCase):
    def test_zero_and_maximum_and_monotonic_direction_without_multiplier(self):
        for value, total in [(0, 0), (1, 21), (2, 42), (3, 63)]:
            with self.subTest(value=value):
                result = scores(answered_questions(value))
                self.assertTrue(result['complete'])
                self.assertEqual(result['instrument'], INSTRUMENT)
                self.assertEqual(result['total_score'], total)
                self.assertEqual(result['mean_score'], value)
                self.assertEqual(result['total_range'], [0, 63])
                self.assertEqual(result['coverage'], {'answered': 21, 'total': 21, 'ratio': 1.})
                for scale in result['subscales'].values():
                    self.assertEqual(scale['raw_score'], value * 7)
                    self.assertEqual(scale['mean_score'], value)
                    self.assertEqual(scale['raw_range'], [0, 21])
                    self.assertEqual(scale['mean_range'], [0, 3])
                    self.assertNotIn('doubled_score', scale)
                    self.assertNotIn('severity', scale)

    def test_missing_and_skipped_choices_do_not_become_zero_or_partial_totals(self):
        rows = answered_questions(2)
        rows[0].update(status='skipped', score=3)
        rows[2]['score'] = None
        result = scores(rows)
        self.assertFalse(result['complete'])
        self.assertIsNone(result['total_score'])
        self.assertIsNone(result['mean_score'])
        self.assertEqual(result['coverage']['answered'], 19)
        self.assertEqual(result['subscales']['mood']['missing_item_ids'], ['Q03'])
        self.assertIsNone(result['subscales']['mood']['raw_score'])
        self.assertIsNone(result['subscales']['tension']['mean_score'])
        self.assertEqual(result['subscales']['worry']['raw_score'], 14)
        self.assertEqual(result['subscales']['worry']['mean_score'], 2)

    def test_invalid_unfinished_duplicate_and_absent_rows_remain_missing(self):
        for invalid in (True, 2., '2', -1, 4, float('nan')):
            rows = answered_questions()
            rows[1]['score'] = invalid
            result = scores(rows)
            self.assertIsNone(result['total_score'], repr(invalid))
            self.assertEqual(result['subscales']['worry']['missing_item_ids'], ['Q02'])
        rows = answered_questions(1)
        rows[1]['status'] = 'answering'
        rows.append(dict(id='Q03', score=2, status='answered'))
        rows.pop(3)
        original = copy.deepcopy(rows)
        result = scores(rows)
        self.assertEqual(result['coverage']['answered'], 18)
        self.assertEqual(rows, original)
        self.assertEqual(result['subscales']['mood']['missing_item_ids'], ['Q03'])
        self.assertEqual(result['subscales']['worry']['missing_item_ids'], ['Q02', 'Q04'])
        empty = scores([])
        self.assertEqual(empty['coverage']['answered'], 0)
        self.assertTrue(all(scale['raw_score'] is None for scale in empty['subscales'].values()))


class PrimaryTaskAndComparisonTests(unittest.TestCase):
    def test_tasks_require_three_explicit_self_ratings_without_transcript_inference(self):
        result = task_scores([{'id': 'R_MOOD', 'key': 'mood', 'status': 'answered', 'score': 1},
                              {'id': 'R_WORRY', 'key': 'worry', 'status': 'answered', 'score': 2},
                              {'id': 'R_TENSION', 'key': 'tension', 'status': 'answered', 'score': 3}])
        self.assertTrue(result['complete'])
        self.assertEqual(result['total_score'], 6)
        self.assertEqual(result['total_range'], [0, 9])
        self.assertEqual(result['mean_score'], 2)
        self.assertEqual(result['coverage'], {'answered': 3, 'total': 3, 'ratio': 1.})
        self.assertEqual(result['subscales']['mood']['raw_score'], 1)
        self.assertEqual(result['subscales']['worry']['mean_score'], 2)
        incomplete = task_scores([{'id': 'R_MOOD', 'status': 'answered', 'score': 0},
                                 {'id': 'R_WORRY', 'status': 'answered', 'answer_text': '我经常很害怕'},
                                 {'id': 'R_TENSION', 'status': 'skipped', 'score': 3},
                                 {'id': 'task_open_1', 'status': 'answered', 'score': 3}])
        self.assertFalse(incomplete['complete'])
        self.assertEqual(incomplete['subscales']['mood']['raw_score'], 0)
        self.assertIsNone(incomplete['subscales']['worry']['raw_score'])
        self.assertIsNone(incomplete['subscales']['tension']['raw_score'])
        self.assertIsNone(incomplete['total_score'])
        self.assertIsNone(incomplete['mean_score'])
        self.assertEqual(incomplete['coverage']['answered'], 1)

    def test_task_compact_mapping_is_explicit_but_invalid_or_duplicate_ratings_are_missing(self):
        self.assertEqual(task_scores({'mood': 0, 'worry': 0, 'tension': 0})['total_score'], 0)
        self.assertEqual(task_scores({'mood': 3, 'worry': 3, 'tension': 3})['total_score'], 9)
        for invalid in (True, 2., '有时', '2', None, -1, 4):
            result = task_scores({'mood': invalid, 'worry': 0, 'tension': 0})
            self.assertIsNone(result['subscales']['mood']['raw_score'])
            self.assertIsNone(result['total_score'])
        result = task_scores([{'id': 'R_MOOD', 'status': 'answered', 'score': 1},
                              {'id': 'R_MOOD', 'status': 'answered', 'score': 3}])
        self.assertIsNone(result['subscales']['mood']['raw_score'])
        mismatch = task_scores([{'id': 'R_MOOD', 'key': 'worry', 'status': 'answered', 'score': 1}])
        self.assertEqual(mismatch['coverage']['answered'], 0)

    def test_comparison_uses_same_direction_means_and_only_describes_differences(self):
        first = scores(answered_questions(1))
        second = task_scores({'mood': 3, 'worry': 1, 'tension': 0})
        result = comparison(first, second)
        self.assertEqual(first['total_score'], 21)
        self.assertEqual(second['total_score'], 4)
        for key, delta in [('mood', 2), ('worry', 0), ('tension', -1)]:
            self.assertTrue(result['dimensions'][key]['comparable'])
            self.assertEqual(result['dimensions'][key]['difference'], delta)
        self.assertAlmostEqual(result['overall']['difference'], 4 / 3 - 1)
        self.assertFalse(result['automatic_consistency_judgment'])
        self.assertNotIn('consistent', result)
        self.assertNotIn('diagnosis', result)
        self.assertNotIn('equivalent_score', result)
        self.assertEqual(result['difference_direction'], 'task_minus_questionnaire')

    def test_incomplete_comparison_keeps_known_dimensions_without_imputing_overall(self):
        rows = answered_questions(2)
        rows[2]['status'] = 'skipped'
        result = comparison(scores(rows), task_scores({'mood': 2, 'worry': 3}))
        self.assertFalse(result['dimensions']['mood']['comparable'])
        self.assertIsNone(result['dimensions']['mood']['difference'])
        self.assertEqual(result['dimensions']['mood']['task_rating'], 2)
        self.assertTrue(result['dimensions']['worry']['comparable'])
        self.assertEqual(result['dimensions']['worry']['difference'], 1)
        self.assertFalse(result['dimensions']['tension']['comparable'])
        self.assertIsNone(result['overall']['difference'])
        self.assertFalse(result['overall']['comparable'])
        self.assertIsNone(comparison(None, None)['overall']['difference'])
        with self.assertRaises(ValueError):
            comparison({'instrument': 'DASS-21'}, task_scores({}))


if __name__ == '__main__':
    unittest.main()
