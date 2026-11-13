"""통합 계획 단계: 영역별 종합 방향 후보를 한 번에 보고 같은 판단 주제를 묶는다. 모델은 후보 번호의 묶음만 정하고 코드가 합치고 검증한다.
운영 실측: 영역별 계획이 서로를 보지 못해 같은 주제를 중복 제안했고, 통합 검토가 라운드마다 이를 지적해 수렴하지 못했다."""
from pathlib import Path
from copy import deepcopy
import asyncio, json, sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import pytest
import asis_synthesis
from asis_synthesis import cross_area_view, validate_cross_area_groups, apply_cross_area_groups
from asis_pipeline import Pipeline
from test_asis_parallel_synthesis import synthesis_fixture, DirectionRuntime

REFS = ('issue_refs', 'area_implication_refs', 'fact_refs', 'criterion_refs', 'goal_refs')


def slot(scope, facts, impl='I_a', issues=('P_1',), crit=('C_1',)):
    return {'scope': scope, 'reason': scope + '의 근거', 'issue_refs': list(issues), 'area_implication_refs': [impl],
            'fact_refs': list(facts), 'criterion_refs': list(crit), 'goal_refs': []}


CANDIDATES = [slot('이미지 배포의 제한 응답 대응', ['F_1', 'F_2'], 'I_ai'), slot('이미지 배포 제한 응답과 용량 변경', ['F_2', 'F_3', 'F_4'], 'I_telemetry', ('P_2',)),
              slot('저장소 접근 경계', ['F_5'], 'I_data', ('P_3',)), slot('구독 예산 알림', ['F_6'], 'I_cost', ('P_4',))]
AREAS = ['ai', 'telemetry', 'data', 'cost']


def test_view_has_only_index_area_scope_and_reason():
    view = cross_area_view(CANDIDATES, AREAS)
    assert [list(row) for row in view['candidates']] == [['index', 'area', 'scope', 'reason']] * 4
    assert view['candidates'][1]['index'] == 1 and view['candidates'][1]['area'] == 'telemetry'
    assert 'fact_refs' not in json.dumps(view), 'references must never be sent: the model only decides which candidates share a topic'


def test_a_valid_grouping_is_accepted():
    assert validate_cross_area_groups(CANDIDATES, AREAS, {'groups': [{'members': [1, 0], 'reason': '같은 이미지 배포 제한 응답'}]}) == [[0, 1]]
    assert validate_cross_area_groups(CANDIDATES, AREAS, {'groups': []}) == []


@pytest.mark.parametrize('response, message', [
    ({'groups': [{'members': [0], 'reason': 'x'}]}, 'invalid_cross_area_group'),
    ({'groups': [{'members': [0, 9], 'reason': 'x'}]}, 'invalid_cross_area_group'),
    ({'groups': [{'members': [0, '1'], 'reason': 'x'}]}, 'invalid_cross_area_group'),
    ({'groups': [{'members': [0, 0], 'reason': 'x'}]}, 'invalid_cross_area_group'),
    ({'groups': [{'members': [0, 1], 'reason': ' '}]}, 'invalid_cross_area_group'),
    ({'groups': [{'members': [0, 1], 'reason': 'x', 'extra': 1}]}, 'invalid_cross_area_group'),
    ({'groups': [{'members': [0, 1], 'reason': 'x'}, {'members': [1, 2], 'reason': 'y'}]}, 'cross_area_group_member_reused'),
    ({'groups': 'x'}, 'invalid_cross_area_groups_shape'),
    ({'groups': [], 'new': 1}, 'invalid_cross_area_groups_shape'),
])
def test_malformed_groupings_are_rejected(response, message):
    with pytest.raises(ValueError, match=message):
        validate_cross_area_groups(CANDIDATES, AREAS, response)


def test_a_group_inside_a_single_area_is_rejected():
    with pytest.raises(ValueError, match='cross_area_group_single_area'):
        validate_cross_area_groups(CANDIDATES, ['ai', 'ai', 'data', 'cost'], {'groups': [{'members': [0, 1], 'reason': 'x'}]})


def test_merging_keeps_the_union_of_every_reference_and_the_wording_with_the_most_refs():
    merged, report = apply_cross_area_groups(deepcopy(CANDIDATES), [[0, 1]])
    assert len(merged) == 3
    kept = merged[0]
    assert kept['scope'] == '이미지 배포 제한 응답과 용량 변경', 'the candidate with more references provides the wording'
    assert kept['fact_refs'] == ['F_2', 'F_3', 'F_4', 'F_1'] and set(kept['area_implication_refs']) == {'I_ai', 'I_telemetry'}
    assert set(kept['issue_refs']) == {'P_1', 'P_2'}
    for key in REFS:
        before = {ref for row in CANDIDATES for ref in row[key]}
        after = {ref for row in merged for ref in row[key]}
        assert before == after, 'no reference may disappear by merging: ' + key
    assert report == [{'kept_scope': '이미지 배포 제한 응답과 용량 변경', 'merged_scopes': ['이미지 배포의 제한 응답 대응']}]


def test_without_groups_nothing_changes():
    merged, report = apply_cross_area_groups(deepcopy(CANDIDATES), [])
    assert merged == CANDIDATES and report == []


def test_three_member_group_and_independent_groups():
    merged, _ = apply_cross_area_groups(deepcopy(CANDIDATES), [[0, 1, 3]])
    assert len(merged) == 2 and {ref for ref in merged[0]['fact_refs']} == {'F_1', 'F_2', 'F_3', 'F_4', 'F_6'}
    merged, _ = apply_cross_area_groups(deepcopy(CANDIDATES), [[0, 1], [2, 3]])
    assert len(merged) == 2


def planned(tmp_path, answer):
    data, plan = synthesis_fixture(tmp_path)
    seen = []

    class Runtime(DirectionRuntime):
        async def ask(self, role, message, **kwargs):
            if role == 'interpret' and message.startswith(asis_synthesis.CROSS_AREA_GROUPING_INSTRUCTION[:30]):
                seen.append(message)
                envelope = json.loads(message.rsplit('\n입력:\n', 1)[-1])
                assert set(envelope) == {'run', 'stage_data'} and envelope['stage_data']['mode'] == 'synthesis_cross_area_grouping'
                view = envelope['stage_data']['cross_area_candidates']
                assert 'facts' not in envelope and 'criteria' not in envelope, 'no source bodies may be sent to the grouping call'
                body = answer(view)
                return body if isinstance(body, str) else json.dumps(body, ensure_ascii=False), {'test_only': True, 'role': role}
            return await super().ask(role, message, **kwargs)
    pipe = Pipeline(Runtime(plan), tmp_path / 'run')
    return data, pipe, seen


def test_the_pipeline_calls_the_grouping_model_exactly_once_and_merges_what_it_groups(tmp_path):
    def answer(view):
        cands = view['candidates']
        return {'groups': [{'members': [cands[0]['index'], cands[1]['index']], 'reason': '같은 판단 주제'}]} if len({c['area'] for c in cands[:2]}) > 1 else {'groups': []}
    data, pipe, seen = planned(tmp_path, answer)
    slots = asyncio.run(asis_synthesis.plan_synthesis(pipe, data))
    assert len(seen) == 1, 'one grouping call per planning run'
    report = json.loads(next((tmp_path / 'run').rglob('cross-area-grouping-report.json')).read_text(encoding='utf-8'))
    assert report['enabled'] is True
    assert slots, 'planning still yields directions'


def test_an_invalid_grouping_is_repaired_once_then_fails_loudly_instead_of_being_skipped(tmp_path):
    data, pipe, seen = planned(tmp_path, lambda view: {'groups': [{'members': [0], 'reason': 'x'}]})
    with pytest.raises(RuntimeError, match='synthesis_cross_area_grouping_failed'):
        asyncio.run(asis_synthesis.plan_synthesis(pipe, data))
    assert len(seen) == pipe.rounds, 'the model is asked again with the contract error, then the revision fails visibly'
    assert any('직전 응답의 오류만 고친다' in m for m in seen[1:])


def test_a_malformed_response_text_fails_loudly(tmp_path):
    data, pipe, seen = planned(tmp_path, lambda view: 'not json at all')
    with pytest.raises(RuntimeError, match='synthesis_cross_area_grouping_failed'):
        asyncio.run(asis_synthesis.plan_synthesis(pipe, data))
