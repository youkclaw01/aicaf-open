"""구간·통합 검토의 evidence_refs 정리: 입력에 없는 ID만 제거하고, 지적 내용·판정·다른 필드는 바꾸지 않으며, 구간·통합 검토에만 적용한다.
운영 실측: 통합 검토가 방향·시사점 ID를 evidence_refs에 43회 연속 넣어 invalid_reference:evidence_refs로 개정이 실패했다."""
from pathlib import Path
from copy import deepcopy
import asyncio, json, sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from asis_pipeline import strip_unknown_evidence_refs, Pipeline
from test_asis_parallel_synthesis import synthesis_fixture, DirectionRuntime
from test_asis_synthesis_review_shards import _merged
import asis_synthesis

DATA = {'facts': [{'id': 'F_1'}, {'id': 'F_2'}], 'criteria': [{'id': 'C_1'}], 'goals': [{'id': 'G_1'}], 'judgments': [{'id': 'J_1'}]}


def review(refs_per_defect, verdict='revise'):
    return {'stage': 'review', 'run': {}, 'verdict': verdict, 'content_checked': True, 'render_checked': False,
            'defects': [{'where': '/directions/%d/title' % i, 'quote': 'q%d' % i, 'why': 'w', 'fix': 'f', 'evidence_refs': refs} for i, refs in enumerate(refs_per_defect)],
            'unresolved': [{'topic': 't', 'reason': 'r', 'required_evidence': 'e', 'fact_refs': ['D_9']}]}


def test_ids_that_are_not_inputs_are_removed_and_the_removal_is_recorded(tmp_path):
    original = review([['D_P0001', 'I_network', 'F_1', 'G_1'], ['D_P0002'], []])
    result = strip_unknown_evidence_refs(DATA, original, tmp_path)
    assert [d['evidence_refs'] for d in result['defects']] == [['F_1', 'G_1'], [], []]
    saved = json.loads((tmp_path / 'evidence-refs-stripped.json').read_text(encoding='utf-8'))
    assert saved['removed'] == [{'defect': 0, 'where': '/directions/0/title', 'dropped': ['D_P0001', 'I_network']},
                                {'defect': 1, 'where': '/directions/1/title', 'dropped': ['D_P0002']}]


def test_nothing_else_in_the_review_changes_and_the_original_object_is_untouched(tmp_path):
    original = review([['D_P0001', 'F_1']])
    before = deepcopy(original)
    result = strip_unknown_evidence_refs(DATA, original, tmp_path)
    assert original == before, 'the model response object must not be mutated'
    for key in ('verdict', 'content_checked', 'render_checked', 'unresolved', 'stage', 'run'):
        assert result[key] == original[key]
    for a, b in zip(result['defects'], original['defects']):
        assert {k: v for k, v in a.items() if k != 'evidence_refs'} == {k: v for k, v in b.items() if k != 'evidence_refs'}


def test_a_clean_review_is_returned_as_is_and_no_record_is_written(tmp_path):
    original = review([['F_1', 'C_1', 'J_1']])
    result = strip_unknown_evidence_refs(DATA, original, tmp_path / 'x')
    assert result == original and not (tmp_path / 'x').exists()


def test_malformed_shapes_are_left_to_the_contract_to_reject(tmp_path):
    assert strip_unknown_evidence_refs(DATA, 'not a dict', tmp_path) == 'not a dict'
    assert strip_unknown_evidence_refs(DATA, {'defects': 'x'}, tmp_path) == {'defects': 'x'}
    odd = review([['F_1']]); odd['defects'][0]['evidence_refs'] = 'F_1'
    assert strip_unknown_evidence_refs(DATA, odd, tmp_path)['defects'][0]['evidence_refs'] == 'F_1', 'a non-list value is not our case to fix'


def test_sharded_integration_review_survives_directions_in_evidence_refs(tmp_path):
    """통합 검토가 방향 ID를 evidence_refs에 넣어도 개정이 계약 오류로 실패하지 않고, 지적 자체는 보존된다."""
    data, plan = synthesis_fixture(tmp_path)
    merged = {**_merged(60), 'run': data['run']}

    class Offender(DirectionRuntime):
        async def ask(self, role, message, **kwargs):
            payload = json.loads(message.rsplit('\n입력:\n', 1)[-1])
            if role == 'content_review' and payload['stage_data'].get('mode') == 'synthesis-integration':
                rows = payload['stage_data']['draft']['directions']
                value = {'stage': 'review', 'run': payload['run'], 'verdict': 'revise', 'content_checked': True, 'render_checked': False,
                         'defects': [{'where': '/directions/0/title', 'quote': rows[0]['title'], 'why': '다른 방향과 중복',
                                      'fix': '구분', 'evidence_refs': ['D_0001', 'I_x']}], 'unresolved': []}
                return json.dumps(value, ensure_ascii=False), {'test_only': True, 'role': role}
            return await super().ask(role, message, **kwargs)
    pipe = Pipeline(Offender(plan), tmp_path / 'run')
    result = asyncio.run(asis_synthesis._review_merged_synthesis(pipe, data, merged))
    assert result['verdict'] == 'revise' and result['defects'][0]['where'] == '/directions/0/title'
    assert result['defects'][0]['evidence_refs'] == []
    stripped = list((tmp_path / 'run').rglob('evidence-refs-stripped.json'))
    assert stripped, 'the removal must be recorded next to the review'


def test_a_non_sharded_review_is_not_cleaned_and_still_fails_on_unknown_refs(tmp_path):
    """구간·통합이 아닌 검토(작성 단계 검토 등)는 기존 엄격한 계약을 그대로 유지한다."""
    import inspect
    source = inspect.getsource(Pipeline._independent_review)
    assert 'if shard_note:' in source and 'strip_unknown_evidence_refs' in source
    block = source[source.index('if shard_note:\n                    # 구간'):source.index('self.validator.validate_output(review_data, review)')]
    assert 'strip_unknown_evidence_refs(review_data, review, attempt_folder)' in block
