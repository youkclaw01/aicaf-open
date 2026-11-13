"""2차 묶기의 입력 크기 검증: 합친 방향이 작성 입력 한도(asis_model_view.LIMITS)를 넘으면 코드가 묶음을 줄이거나 풀어 준다.

운영 실측(2026-10-16): 2차 묶기로 참조가 합집합으로 늘어난 방향 6개가 145만~196만 자로 모델 호출 전에 model_input_over_limit 실패했다.
"""
from copy import deepcopy
from pathlib import Path
import asyncio, json, sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import asis_model_view as mv
import asis_synthesis as S
from test_asis_model_view import heavy_data


def build_case(tmp_path, big_fact_chars):
    """방향 3개 후보: 후보 0·1은 서로 다른 큰 사실을 인용한다. 합치면 두 큰 사실이 모두 한 방향 입력에 실린다."""
    data, plan = heavy_data(tmp_path, extra_facts=0)
    run = data['run']
    for name in ('F_BIG_A', 'F_BIG_B'):
        data['facts'].append({'id': name, 'source_id': 'x.json', 'field_path': '/' + name, 'run': deepcopy(run),
                              'value': {'blob': 'x' * big_fact_chars}, 'collection_status': 'collected', 'scope_complete': True})
    base = dict(plan['directions'][0])
    rows = []
    for i, big in enumerate(['F_BIG_A', 'F_BIG_B', None]):
        row = deepcopy(base)
        row['scope'] = '범위 %d' % i
        row['fact_refs'] = list(dict.fromkeys(row['fact_refs'] + ([big] if big else [])))
        rows.append(row)
    return data, rows


def test_a_merge_whose_combined_references_exceed_the_limit_is_detected_by_code(tmp_path):
    data, rows = build_case(tmp_path, big_fact_chars=500_000)
    assert S.merged_direction_over_limit(data, rows, [0]) is None, 'one big fact alone fits'
    over = S.merged_direction_over_limit(data, rows, [0, 1])
    assert over is not None and over[0] > over[1] == mv.LIMITS['direction_author']


def test_a_group_over_the_limit_is_dropped_when_even_two_members_do_not_fit_and_trimmed_otherwise(tmp_path):
    data, rows = build_case(tmp_path, big_fact_chars=500_000)
    kept, rejected = S._drop_groups_over_input_limit(data, rows, [[0, 1]])
    assert kept == [] and rejected[0]['reason'] == 'over_input_limit_even_at_two_members'
    kept, rejected = S._drop_groups_over_input_limit(data, rows, [[0, 2]])
    assert kept == [[0, 2]] and rejected == [], 'a group that fits is left untouched'
    # 3개 묶음에서 가장 무거운 후보를 빼면 한도 안에 드는 경우: 0·1·2 -> (0,1 합치면 초과) -> 가장 참조가 많은 것을 빼도 0·1이 남아 초과면 전체 해제
    kept, rejected = S._drop_groups_over_input_limit(data, rows, [[0, 1, 2]])
    assert all(S.merged_direction_over_limit(data, rows, g) is None for g in kept), 'whatever is kept must fit'


def test_small_references_merge_as_before(tmp_path):
    data, rows = build_case(tmp_path, big_fact_chars=1000)
    kept, rejected = S._drop_groups_over_input_limit(data, rows, [[0, 1, 2]])
    assert kept == [[0, 1, 2]] and rejected == []


class _Pipe:
    """모델이 묶음을 정하는 호출 하나만 흉내 내는 최소 파이프(응답 고정). 저장은 무시한다."""
    rounds = 3

    class _Validator:
        @staticmethod
        def strict_load(raw):
            return json.loads(raw)

    validator = _Validator()

    def __init__(self, response):
        self.response = response
        self.runtime = self
        self.folder = Path('.')

    async def ask(self, role, message, **kwargs):
        return json.dumps(self.response, ensure_ascii=False), {'test_only': True}

    def save(self, *args, **kwargs):
        return None


def test_the_real_second_pass_call_path_does_not_merge_a_group_over_the_input_limit(tmp_path):
    """모델이 큰 두 후보를 한 묶음으로 정해도 호출 경로가 한도를 확인해 합치지 않는다(운영 2026-10-16 재현)."""
    data, rows = build_case(tmp_path, big_fact_chars=500_000)
    pipe = _Pipe({'groups': [{'members': [0, 1], 'reason': '같은 판단'}]})
    merged, report = asyncio.run(S._second_pass_grouping(pipe, data, 'system', rows, ['a', 'b', 'c'], tmp_path))
    assert len(merged) == 3, 'the over-limit group must stay unmerged'
    assert report['groups_rejected_over_input_limit'][0]['reason'] == 'over_input_limit_even_at_two_members'
    for slot in merged:
        assert S.merged_direction_over_limit(data, merged, [merged.index(slot)]) is None


def test_the_real_call_path_still_merges_a_group_that_fits(tmp_path):
    data, rows = build_case(tmp_path, big_fact_chars=1000)
    pipe = _Pipe({'groups': [{'members': [0, 1], 'reason': '같은 판단'}]})
    merged, report = asyncio.run(S._second_pass_grouping(pipe, data, 'system', rows, ['a', 'b', 'c'], tmp_path))
    assert len(merged) == 2 and report['groups_rejected_over_input_limit'] == []


def test_the_limit_is_measured_on_what_is_actually_sent_not_on_the_pre_prompt_view(tmp_path):
    """가격 원천(F_E_prices*)은 모델에 가기 직전에 해시·행 수로 바뀐다. 그 본문이 127만 자라도 실제 전송은 작으므로 한도에서 거절하면 안 된다(운영 2026-10-16 오탐 6건)."""
    from test_asis_model_view import heavy_data
    data, plan = heavy_data(tmp_path, extra_facts=0)
    run = data['run']
    data['facts'].append({'id': 'F_E_pricesModels', 'source_id': 'prices.json', 'field_path': '/prices', 'run': deepcopy(run),
                          'value': [{'sku': 'm%d' % i, 'unit': 'x' * 400} for i in range(3000)], 'collection_status': 'collected', 'scope_complete': False})
    slot = dict(plan['directions'][0]); slot['id'] = data['allocated_ids']['directions'][0]
    slot['fact_refs'] = list(dict.fromkeys(slot['fact_refs'] + ['F_E_pricesModels']))
    scoped = S.direction_input(data, slot)
    from asis_evidence_views import prompt_view
    assert mv._chars(mv.direction_view(scoped, 'direction_author')) > mv.LIMITS['direction_author'], 'pre-prompt view is over the limit (this is what used to be rejected)'
    kind, view = mv.build(2, scoped)       # 실제 전송 크기로 재므로 예외 없이 통과한다
    assert kind == 'direction_author'
    assert mv._chars(prompt_view(deepcopy(view))) < 300_000


def test_a_really_oversized_sent_input_is_still_rejected_before_the_model_call(tmp_path):
    from test_asis_model_view import heavy_data
    data, plan = heavy_data(tmp_path, extra_facts=0)
    run = data['run']
    data['facts'].append({'id': 'F_X_HUGE', 'source_id': 'x.json', 'field_path': '/x', 'run': deepcopy(run),
                          'value': {'blob': 'x' * 40_000}, 'collection_status': 'collected', 'scope_complete': True})
    slot = dict(plan['directions'][0]); slot['id'] = data['allocated_ids']['directions'][0]
    slot['fact_refs'] = list(dict.fromkeys(slot['fact_refs'] + ['F_X_HUGE']))
    old = mv.LIMITS['direction_author']
    mv.LIMITS['direction_author'] = 10_000
    try:
        import pytest
        with pytest.raises(ValueError, match='model_input_over_limit:direction_author'):
            mv.build(2, S.direction_input(data, slot))
    finally:
        mv.LIMITS['direction_author'] = old
