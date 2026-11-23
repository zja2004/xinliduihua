"""Project-authored PRIMARY-21 scoring, with no diagnostic interpretation.

This child-friendly questionnaire retains the existing 21 topic positions, but
changes wording and response anchors. It is not DASS-21, a validated translation,
or a clinical screening instrument. No DASS multiplier or norms are used.
"""
from __future__ import annotations

import math
import re
from collections import defaultdict
from collections.abc import Mapping

INSTRUMENT = 'PRIMARY-21'
VERSION = '1.0'
SUBSCALES = {
    'mood': (3, 5, 10, 13, 16, 17, 21),
    'worry': (2, 4, 7, 9, 15, 19, 20),
    'tension': (1, 6, 8, 11, 12, 14, 18),
}
LABELS = {'mood': '心情与兴趣', 'worry': '担心与身体感受', 'tension': '紧绷与调节'}
RESPONSE_OPTIONS = {'0': '没有', '1': '偶尔', '2': '有时', '3': '经常'}
TASK_RATINGS = (
    {'id': 'R_MOOD', 'key': 'mood', 'title': '补充自评：心情与兴趣',
     'text': '再想一想过去一周，你有多少时候心情不太好，或对喜欢的事情提不起兴趣？'},
    {'id': 'R_WORRY', 'key': 'worry', 'title': '补充自评：担心与身体感受',
     'text': '过去一周，你有多少时候觉得担心或害怕？'},
    {'id': 'R_TENSION', 'key': 'tension', 'title': '补充自评：紧绷与调节',
     'text': '过去一周，你有多少时候心里紧绷，或不太能放松？'},
)
NOTICE = ('项目自编试用问卷，尚未验证信效度；分数仅整理本人报告的过去一周感受，'
          '不代表疾病、诊断或风险等级，也不使用原量表常模。')


def explicit_score(text):
    """Recognize only a complete, unambiguous short choice, never a story.

    Numeric choices and the four exact frequency labels share the same anchors.
    A negation, correction, explanation or conflicting pair requires clarification.
    """
    if not isinstance(text, str):
        return None
    value = re.sub(r'[\s，。！？、,.!?：:]', '', text)
    value = value.translate(str.maketrans('０１２３', '0123'))
    for prefix in ('我的选择是', '我的评分是', '我选择', '我选', '选择', '选'):
        if value.startswith(prefix):
            value = value[len(prefix):]
            break
    choices = {
        0: {'0', '零', '〇', '0分', '零分', '〇分', '没有'},
        1: {'1', '一', '1分', '一分', '偶尔'},
        2: {'2', '二', '两', '2分', '二分', '两分', '有时'},
        3: {'3', '三', '3分', '三分', '经常'},
    }
    # Permit a stated number plus its matching label, but not two different
    # labels/numbers. Repeated choices or spoken filler are left for confirmation.
    for score, labels in choices.items():
        if value in labels:
            return score
        numeral_forms = {0: ('0', '零', '〇'), 1: ('1', '一'),
                         2: ('2', '二', '两'), 3: ('3', '三')}[score]
        label = RESPONSE_OPTIONS[str(score)]
        if value in {number + '分' + label for number in numeral_forms} | {
                label + number + '分' for number in numeral_forms}:
            return score
    return None


def _valid_score(value):
    return type(value) is int and 0 <= value <= 3


def _coverage(answered, total):
    return {'answered': answered, 'total': total, 'ratio': answered / total}


def _metadata(instrument):
    return {'instrument': instrument, 'version': VERSION, 'reference_period': 'past_week',
            'validation_status': 'unvalidated_project_authored',
            'direction': 'higher_means_more_self_reported_difficulty',
            'missing_policy': 'no_imputation; incomplete totals and subscales are null',
            'interpretation': 'descriptive_only; no_norms_or_diagnostic_thresholds',
            'audience': 'teacher_for_manual_review', 'notice': NOTICE}


def _question_rows(questions):
    rows = defaultdict(list)
    if questions is None:
        return rows
    for question in questions:
        if isinstance(question, Mapping) and isinstance(question.get('id'), str):
            rows[question['id']].append(question)
    return rows


def scores(questions):
    """Return raw 0–21 and mean 0–3 per group, total 0–63 when complete.

    Each Q01–Q21 must occur once, have status='answered', and contain an integer
    score 0–3. Missing, skipped, duplicate, unfinished and malformed choices are
    missing data. Partial sums are never presented as complete scores.
    """
    rows = _question_rows(questions)
    result = _metadata(INSTRUMENT)
    result.update(subscales={}, total_range=[0, 63], mean_range=[0, 3])
    valid_count, raw_values = 0, []
    for key, numbers in SUBSCALES.items():
        item_ids = [f'Q{number:02}' for number in numbers]
        missing, values = [], []
        for item_id in item_ids:
            entries = rows.get(item_id, [])
            question = entries[0] if len(entries) == 1 else {}
            score = question.get('score')
            if question.get('status') == 'answered' and _valid_score(score):
                values.append(score)
            else:
                missing.append(item_id)
        raw = sum(values) if not missing else None
        valid_count += len(values)
        raw_values.extend(values)
        result['subscales'][key] = {
            'label': LABELS[key], 'item_ids': item_ids, 'missing_item_ids': missing,
            'complete': not missing, 'raw_score': raw,
            'mean_score': raw / len(item_ids) if raw is not None else None,
            'raw_range': [0, 21], 'mean_range': [0, 3],
            'coverage': _coverage(len(values), len(item_ids)),
        }
    result['complete'] = valid_count == 21
    result['coverage'] = _coverage(valid_count, 21)
    result['total_score'] = sum(raw_values) if result['complete'] else None
    result['mean_score'] = result['total_score'] / 21 if result['complete'] else None
    return result


def _rating_rows(ratings):
    """Normalize explicit structured ratings without reading answer text/media."""
    rows = defaultdict(list)
    by_id = {item['id']: item['key'] for item in TASK_RATINGS}
    if isinstance(ratings, Mapping):
        # Compact mappings are already explicit structured choices. Mapping
        # values may instead carry the same status/score rows used by the UI.
        for key, value in ratings.items():
            dimension = key if key in SUBSCALES else by_id.get(key)
            if dimension:
                row = value if isinstance(value, Mapping) else {'score': value, 'status': 'answered'}
                rows[dimension].append(row)
        return rows
    for row in ratings or ():
        if not isinstance(row, Mapping):
            continue
        key, item_id = row.get('key'), row.get('id')
        mapped = by_id.get(item_id)
        # Reject a mismatched id/key rather than assigning a score to a group.
        if ((key is not None and key not in SUBSCALES)
                or (key and mapped and key != mapped)):
            continue
        dimension = key or mapped
        if dimension in SUBSCALES:
            rows[dimension].append(row)
    return rows


def task_scores(ratings):
    """Score three explicit past-week self-ratings collected after the tasks.

    Accepted inputs: {'mood': 1, 'worry': 2, 'tension': 0}, or rows containing
    id/key, status and score. Every list row must be answered. The reading and
    three open responses, recordings, facial movements and model output are
    never converted to ratings. Raw and mean values per group are both 0–3.
    """
    rows = _rating_rows(ratings)
    result = _metadata('PRIMARY-TASK-3')
    result.update(subscales={}, total_range=[0, 9], mean_range=[0, 3],
                  source='explicit_self_ratings_after_tasks; not_task_performance')
    values, missing = [], []
    for item in TASK_RATINGS:
        key, item_id = item['key'], item['id']
        entries = rows.get(key, [])
        row = entries[0] if len(entries) == 1 else {}
        value = row.get('score')
        complete = row.get('status') == 'answered' and _valid_score(value)
        value = value if complete else None
        if complete:
            values.append(value)
        else:
            missing.append(key)
        result['subscales'][key] = {
            'label': LABELS[key], 'item_ids': [item_id], 'complete': complete,
            'missing_item_ids': [] if complete else [item_id],
            'raw_score': value, 'mean_score': value,
            'raw_range': [0, 3], 'mean_range': [0, 3],
            'coverage': _coverage(int(complete), 1),
        }
    result['complete'] = len(values) == 3
    result['coverage'] = _coverage(len(values), 3)
    result['missing_rating_keys'] = missing
    result['total_score'] = sum(values) if result['complete'] else None
    result['mean_score'] = result['total_score'] / 3 if result['complete'] else None
    return result


def _mean(result, dimension=None):
    if not isinstance(result, Mapping):
        return None
    scales = result.get('subscales')
    part = scales.get(dimension, {}) if dimension and isinstance(scales, Mapping) else ({} if dimension else result)
    if not isinstance(part, Mapping) or part.get('complete') is not True:
        return None
    value = part.get('mean_score')
    if type(value) not in (int, float) or not math.isfinite(value) or not 0 <= value <= 3:
        return None
    return value


def comparison(questionnaire_result, task_result):
    """Describe self-report differences on 0–3 anchors without equivalence claims.

    Positive difference = the later direct self-rating minus the earlier mean
    for that topic. Both refer to the past week. This is not a consistency test,
    a change/effect measure, or a diagnostic threshold; the two parts have
    different numbers of questions and are not psychometrically equivalent.
    """
    for result, expected in ((questionnaire_result, INSTRUMENT), (task_result, 'PRIMARY-TASK-3')):
        if result and (not isinstance(result, Mapping) or result.get('instrument') != expected):
            raise ValueError('只能比较 PRIMARY-21 与明确补充自评，不可混用其他量表')
    dimensions = {}
    for key in SUBSCALES:
        first, second = _mean(questionnaire_result, key), _mean(task_result, key)
        ready = first is not None and second is not None
        dimensions[key] = {'label': LABELS[key], 'questionnaire_mean': first,
                           'task_rating': second, 'difference': second - first if ready else None,
                           'comparable': ready}
    first, second = _mean(questionnaire_result), _mean(task_result)
    ready = first is not None and second is not None
    return {'dimensions': dimensions,
            'overall': {'questionnaire_mean': first, 'task_mean': second,
                        'difference': second - first if ready else None, 'comparable': ready},
            'unit_range': [0, 3], 'difference_direction': 'task_minus_questionnaire',
            'automatic_consistency_judgment': False, 'interpretation': 'descriptive_only',
            'notice': ('只并列展示两部分自述及其差值；两部分题数和内容不同，尚无等效性证据。'
                       '不自动判断一致、不一致、变好、变坏或患病。')}
