"""장표 명세 호출의 입력·검증 시험."""
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import json
import pytest
import asis_visual_forms as V


def inputs():
    topics = [{'id': 'ai-T01', 'scope': 'AI 계정·배포 정합'}, {'id': 'ai-T02', 'scope': '비용 통제'}]
    diagnoses = {'ai-T01': {'text': '진단' * 400, 'impact': '영향' * 200, 'fact_refs': ['F_E_aiDeployments', 'F_IV_XA1-1_transcript']},
                 'ai-T02': {'text': '진단2', 'impact': '영향2', 'fact_refs': ['F_D_cost_monthly']}}
    facts = {'F_E_aiDeployments': {'value': [{'a': 1}] * 5}, 'F_IV_XA1-1_transcript': {'value': '인터뷰 원문 ' * 500}, 'F_D_cost_monthly': {'value': [{'Cost': 1}] * 30}}
    return topics, diagnoses, facts


def test_the_visual_input_never_carries_a_fact_body_or_a_full_diagnosis():
    topics, diagnoses, facts = inputs()
    view = V.visual_view('ai', topics, diagnoses, facts)
    text = json.dumps(view, ensure_ascii=False)
    assert '인터뷰 원문' not in text and '"Cost"' not in text, 'no fact body reaches the model'
    assert len(view['topics'][0]['diagnosis']) == 500 and len(view['topics'][0]['impact']) == 240
    kinds = {row['fact_id']: (row['kind'], row['records']) for row in view['topics'][0]['cited_facts']}
    assert kinds['F_E_aiDeployments'] == ('config', 5) and kinds['F_IV_XA1-1_transcript'] == ('interview', None)
    assert {layer['key'] for layer in view['layers']} == set(V.LAYER_KEYS)


def good(form='architecture', data=None):
    return {'form': form, 'headline': '제목', 'conclusion': '결론', 'data': data or {'focus_layers': ['ai'], 'gap_layers': []}}


def test_every_topic_must_get_exactly_one_validated_visual():
    cited = {'ai-T01': {'F1'}, 'ai-T02': {'F2'}}
    out = V.validate_visuals(['ai-T01', 'ai-T02'], cited, {'visuals': {'ai-T01': good(), 'ai-T02': good('table', {'columns': ['a', 'b', 'c'], 'rows': [['1', '2', '3']]})}})
    assert set(out) == {'ai-T01', 'ai-T02'} and out['ai-T02']['form'] == 'table'
    with pytest.raises(ValueError, match="visuals_topic_mismatch:missing=\\['ai-T02'\\]"):
        V.validate_visuals(['ai-T01', 'ai-T02'], cited, {'visuals': {'ai-T01': good()}})
    with pytest.raises(ValueError, match="extra=\\['zzz'\\]"):
        V.validate_visuals(['ai-T01'], cited, {'visuals': {'ai-T01': good(), 'zzz': good()}})
    with pytest.raises(ValueError, match='invalid_visuals_shape'):
        V.validate_visuals(['ai-T01'], cited, {'visuals': {'ai-T01': good()}, 'prose': 'x'})


def test_a_visual_that_cites_a_fact_outside_its_own_topic_is_rejected():
    cited = {'ai-T01': {'F_D_mine'}}
    bad = good('chart', {'metrics': [{'fact_id': 'F_D_other', 'label': 'x', 'aggregate': 'sum'}]})
    with pytest.raises(ValueError, match='visual_fact_not_cited:F_D_other'):
        V.validate_visuals(['ai-T01'], cited, {'visuals': {'ai-T01': bad}})
