"""DASS-21 deterministic scoring; never infer a choice from narrative text.

Sources: UNSW Chinese DASS21 response form and scoring template.
https://dass.psy.unsw.edu.au/Chinese/tmhc.htm
https://dass.psy.unsw.edu.au/DASSFAQ.htm (FAQs 8, 9, 27, 30)
"""
import re

SUBSCALES = {
    'depression': (3, 5, 10, 13, 16, 17, 21),
    'anxiety': (2, 4, 7, 9, 15, 19, 20),
    'stress': (1, 6, 8, 11, 12, 14, 18),
}


def explicit_score(text):
    value = re.sub(r'[\s，。！？、,.!?：:]', '', text)
    value = value.translate(str.maketrans('０１２３', '0123'))
    options = {
        0: {'0', '零', '零分', '0分', '不适用', '不適用'},
        1: {'1', '一', '一分', '1分', '颇适用或间中适用', '頗適用或間中適用'},
        2: {'2', '二', '两', '二分', '两分', '2分', '很适用或经常适用', '很適用或經常適用'},
        3: {'3', '三', '三分', '3分', '最适用或常常适用', '最適用或常常適用'},
    }
    for prefix in ('我选择', '我选', '选择', '选', '我的选择是', '我的评分是'):
        if value.startswith(prefix):
            value = value[len(prefix):]
            break
    exact = next((score for score, choices in options.items() if value in choices), None)
    if exact is not None:
        return exact
    # Accept repeated identical explicit choices and bounded spoken fillers.
    # Never extract arbitrary digits from a story or resolve conflicting choices.
    for prefix in ('我觉得应该是', '我觉得是', '我觉得', '应该是', '那就', '就是'):
        if value.startswith(prefix):
            value = value[len(prefix):]
            break
    match = re.fullmatch(
        r'([零〇一二两三0123])分?(.*)', value)
    if not match:
        return None
    numeral, tail = match.groups()
    number = {'零': 0, '〇': 0, '一': 1, '二': 2, '两': 2, '三': 3}.get(numeral)
    if number is None:
        number = int(numeral)
    equivalents = {0: '零〇0', 1: '一1', 2: '二两2', 3: '三3'}[number]
    tail = re.sub(r'^([' + equivalents + r']分)+', '', tail)
    if re.fullmatch(r'(?:那[边变遍]|就这样|是的|好的|吧|啊|呀|呢|嗯|对|哦|呗|儿|了)*', tail):
        return number
    return None


def scores(questions):
    by_id = {q['id']: q for q in questions}
    result = {'instrument': 'DASS-21', 'multiplier': 2,
              'missing_policy': 'no_imputation; incomplete subscale scores are null',
              'audience': 'researcher_or_qualified_professional', 'subscales': {}}
    for name, items in SUBSCALES.items():
        missing, values = [], []
        for number in items:
            item_id = f'Q{number:02}'
            question = by_id.get(item_id, {})
            value = question.get('score')
            if question.get('status') != 'answered' or type(value) is not int or not 0 <= value <= 3:
                missing.append(item_id)
            else:
                values.append(value)
        raw = sum(values) if not missing else None
        result['subscales'][name] = {
            'item_ids': [f'Q{number:02}' for number in items], 'complete': not missing,
            'missing_item_ids': missing, 'raw_score': raw,
            'doubled_score': raw * 2 if raw is not None else None,
            'raw_range': [0, 21], 'doubled_range': [0, 42],
        }
    result['complete'] = all(s['complete'] for s in result['subscales'].values())
    return result
