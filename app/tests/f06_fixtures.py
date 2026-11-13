"""고객 사실이 아닌 F06 경계 시험 입력. 실제 200문항 정본은 변경하지 않는다."""
from copy import deepcopy

# 각 영역 25문항의 정수 점수 합은 영역 점수와 같다. 필수 문항은 별도 하한을 지킨다.
CASES = {
    'normal': ({'A': 100, 'B': 100, 'C': 100, 'D': 100}, 'Realizing'),
    'all_three': ({'A': 75, 'B': 75, 'C': 75, 'D': 75}, 'Scaling'),
    'all_two': ({'A': 50, 'B': 50, 'C': 50, 'D': 50}, 'Implementing'),
    'unanswered': (None, None),
    'zero_numeric': (0, 'Exploring'),
    'zero_string': ('0', 'Exploring'),
    'all_unknown': ('미확인', None),
    'all_na': ('해당없음', None),
    'one_missing': ('one_missing', None),
    'area_na': ('area_na', None),
    'd75': ({'A': 100, 'B': 100, 'C': 100, 'D': 75}, 'Scaling'),
    'd79': ({'A': 100, 'B': 100, 'C': 100, 'D': 79}, 'Scaling'),
    'd80': ({'A': 100, 'B': 100, 'C': 100, 'D': 80}, 'Realizing'),
    'min_a69': ({'A': 69, 'B': 100, 'C': 100, 'D': 100}, 'Scaling'),
    'min_a70': ({'A': 70, 'B': 100, 'C': 100, 'D': 100}, 'Realizing'),
    'min_b69': ({'A': 100, 'B': 69, 'C': 100, 'D': 100}, 'Implementing'),
    'min_c69': ({'A': 100, 'B': 100, 'C': 69, 'D': 100}, 'Implementing'),
    'b49': ({'A': 100, 'B': 49, 'C': 100, 'D': 100}, 'Planning'),
    'b50': ({'A': 100, 'B': 50, 'C': 100, 'D': 100}, 'Implementing'),
    'c39': ({'A': 100, 'B': 100, 'C': 39, 'D': 100}, 'Planning'),
    'c40': ({'A': 100, 'B': 100, 'C': 40, 'D': 100}, 'Implementing'),
    'planning_a49': ({'A': 49, 'B': 25, 'C': 25, 'D': 25}, 'Exploring'),
    'planning_a50': ({'A': 50, 'B': 25, 'C': 25, 'D': 25}, 'Planning'),
    'overall29_8': ({'A': 50, 'B': 23, 'C': 23, 'D': 23}, 'Exploring'),
    'overall30': ({'A': 50, 'B': 24, 'C': 23, 'D': 23}, 'Planning'),
    'overall49_8': ({'A': 50, 'B': 50, 'C': 40, 'D': 59}, 'Planning'),
    'overall50': ({'A': 50, 'B': 50, 'C': 40, 'D': 60}, 'Implementing'),
    'overall69_8': ({'A': 69, 'B': 70, 'C': 70, 'D': 70}, 'Implementing'),
    'overall70': ({'A': 70, 'B': 70, 'C': 70, 'D': 70}, 'Scaling'),
    'overall84_8': ({'A': 79, 'B': 80, 'C': 80, 'D': 100}, 'Scaling'),
    'overall85': ({'A': 80, 'B': 80, 'C': 80, 'D': 100}, 'Realizing'),
    'must_zero': ('must_zero', 'Planning'),
    'must_one': ('must_one', 'Planning'),
    'must_two': ('must_two', 'Realizing'),
    'must_one_a_low': ('must_one_a_low', 'Exploring'),
    'unknown20': ('unknown20', 'Realizing'),
    'unknown21': ('unknown21', None),
    'unknown_na20': ('unknown_na20', 'Realizing'),
    'unknown_na21': ('unknown_na21', None),
}

PPT_CASES = ('normal', 'unanswered', 'zero_numeric', 'd75', 'd80', 'must_one',
             'unknown20', 'unknown21', 'unknown_na20', 'unknown_na21', 'area_na', 'all_na')


def area_answers(qs, targets):
    answers = {}
    for area in 'ABCD':
        rows = [q for q in qs if q['area'] == area]
        assert len(rows) == 25
        values = [2 if q['must'] else 0 for q in rows]
        remaining = targets[area] - sum(values)
        assert remaining >= 0
        while remaining:
            for i in range(len(values)):
                if remaining and values[i] < 4:
                    values[i] += 1
                    remaining -= 1
        answers.update({q['id']: {'score': str(value)} for q, value in zip(rows, values)})
    return answers


def make_case(qs, name):
    definition, expected = CASES[name]
    if isinstance(definition, dict):
        return area_answers(qs, definition), expected
    if definition is None:
        return {}, expected
    answers = {q['id']: {'score': '4'} for q in qs}
    if name in ('zero_numeric', 'zero_string', 'all_unknown', 'all_na'):
        answers = {q['id']: {'score': definition} for q in qs}
    elif name == 'one_missing':
        answers.pop(qs[-1]['id'])
    elif name == 'area_na':
        for q in qs:
            if q['area'] == 'D':
                answers[q['id']]['score'] = '해당없음'
    elif name.startswith('must_'):
        if name == 'must_one_a_low':
            for q in qs:
                if q['area'] == 'A':
                    answers[q['id']]['score'] = '1'
        else:
            qid = next(q['id'] for q in qs if q['must'])
            answers[qid]['score'] = {'must_zero': '0', 'must_one': '1', 'must_two': '2'}[name]
    elif name.startswith('unknown'):
        order = [q for group in zip(*[[q for q in qs if q['area'] == area] for area in 'ABCD']) for q in group]
        unknown, na = {'unknown20': (20, 0), 'unknown21': (21, 0),
                       'unknown_na20': (16, 20), 'unknown_na21': (17, 20)}[name]
        for q in order[:unknown]:
            answers[q['id']]['score'] = '미확인'
        for q in order[unknown:unknown + na]:
            answers[q['id']]['score'] = '해당없음'
    else:
        raise ValueError('fixture_not_implemented: ' + name)
    return answers, expected


def rounding_probe(qs, unknown):
    """100문항 운영 입력으로 발생하지 않는 반올림 경계를 읽기 전용 합성 입력으로 확인한다."""
    expanded = []
    for repeat in range(25):
        for q in qs:
            row = deepcopy(q)
            row['id'] = f'f06-r{repeat}-{q["id"]}'
            expanded.append(row)
    answers = {q['id']: {'score': '미확인' if i < unknown else '4'} for i, q in enumerate(expanded)}
    return expanded, answers
