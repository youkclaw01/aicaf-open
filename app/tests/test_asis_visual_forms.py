"""장표 시각 형식 계약 시험: 글이 아니라 형식(아키텍처·표·매트릭스·흐름도·차트·스코어 카드)으로 보여 주고, 길이·범위를 코드가 강제한다."""
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import pytest
import asis_visual_forms as V

CITED = {'F_D_cost_monthly', 'F_E_aiDeployments', 'F_T_subnets'}


def visual(form, data, headline='핵심 제목', conclusion='한 줄 결론'):
    return {'form': form, 'headline': headline, 'conclusion': conclusion, 'data': data}


def test_each_form_accepts_its_own_valid_data():
    V.validate_visual(visual('architecture', {'focus_layers': ['ai', 'network'], 'gap_layers': ['network']}), CITED)
    V.validate_visual(visual('table', {'columns': ['대상', '현재', '갭'], 'rows': [['계정', '3개', '미확인']]}), CITED)
    V.validate_visual(visual('matrix', {'columns': ['부여', '검토', '회수'], 'rows': [{'label': 'AI 관리', 'states': ['충족', '부분', '미확인']}]}), CITED)
    V.validate_visual(visual('flow', {'steps': [{'label': '요청', 'owner': '운영', 'broken': False}, {'label': '승인', 'owner': '관리', 'broken': True}, {'label': '반영', 'owner': '운영', 'broken': False}]}), CITED)
    V.validate_visual(visual('chart', {'metrics': [{'fact_id': 'F_D_cost_monthly', 'label': '월 비용', 'aggregate': 'sum'}]}), CITED)
    V.validate_visual(visual('scorecard', {'items': [{'fact_id': 'F_E_aiDeployments', 'label': '배포', 'aggregate': 'count'}] * 3}), CITED)


def test_the_text_on_a_slide_is_limited_by_code_so_prose_cannot_fill_it():
    with pytest.raises(ValueError, match='visual_text_too_long:headline:61>60'):
        V.validate_visual(visual('architecture', {'focus_layers': ['ai'], 'gap_layers': []}, headline='가' * 61), CITED)
    with pytest.raises(ValueError, match='visual_text_too_long:conclusion'):
        V.validate_visual(visual('architecture', {'focus_layers': ['ai'], 'gap_layers': []}, conclusion='가' * 91), CITED)
    with pytest.raises(ValueError, match='visual_text_too_long:cell'):
        V.validate_visual(visual('table', {'columns': ['a', 'b', 'c'], 'rows': [['가' * 26, 'x', 'y']]}), CITED)


@pytest.mark.parametrize('form,data,error', [
    ('architecture', {'focus_layers': ['ai', 'moon'], 'gap_layers': []}, 'visual_architecture_layer_unknown'),     # 정해진 계층만
    ('architecture', {'focus_layers': [], 'gap_layers': []}, 'visual_architecture_focus_empty'),
    ('architecture', {'focus_layers': ['ai'], 'gap_layers': ['data']}, 'visual_architecture_gap_outside_focus'),
    ('table', {'columns': ['a', 'b'], 'rows': [['1', '2']]}, 'visual_table_columns'),
    ('table', {'columns': ['a', 'b', 'c'], 'rows': [['1', '2', '3']] * 9}, 'visual_table_rows_over:8'),
    ('table', {'columns': ['a', 'b', 'c'], 'rows': [['1', '2']]}, 'visual_table_row_width'),
    ('matrix', {'columns': ['a', 'b'], 'rows': [{'label': 'x', 'states': ['충족', '아마도']}]}, 'visual_matrix_state_invalid'),
    ('flow', {'steps': [{'label': 'a', 'owner': 'o', 'broken': False}] * 2}, 'visual_flow_steps'),
    ('flow', {'steps': [{'label': 'a', 'owner': 'o', 'broken': 'yes'}] * 3}, 'visual_flow_broken_not_boolean'),
    ('chart', {'metrics': []}, 'visual_chart_metrics'),
    ('chart', {'metrics': [{'fact_id': 'F_D_cost_monthly', 'label': 'x', 'aggregate': 'avg'}]}, 'visual_chart_aggregate_unknown'),
    ('scorecard', {'items': [{'fact_id': 'F_E_aiDeployments', 'label': 'x', 'aggregate': 'count'}] * 2}, 'visual_scorecard_items'),
])
def test_every_malformed_form_is_rejected_with_its_own_reason(form, data, error):
    with pytest.raises(ValueError, match=error):
        V.validate_visual(visual(form, data), CITED)


def test_a_chart_or_scorecard_may_only_use_facts_the_diagnosis_cites_and_never_carries_a_number():
    with pytest.raises(ValueError, match='visual_fact_not_cited:F_D_other'):
        V.validate_visual(visual('chart', {'metrics': [{'fact_id': 'F_D_other', 'label': 'x', 'aggregate': 'sum'}]}), CITED)
    with pytest.raises(ValueError, match='visual_shape_invalid:chart_metric'):
        V.validate_visual(visual('chart', {'metrics': [{'fact_id': 'F_D_cost_monthly', 'label': 'x', 'aggregate': 'sum', 'value': 1234}]}), CITED)


def test_an_unknown_form_or_an_extra_field_is_rejected():
    with pytest.raises(ValueError, match='visual_form_unknown:pie'):
        V.validate_visual(visual('pie', {}), CITED)
    with pytest.raises(ValueError, match='visual_shape_invalid:visual'):
        V.validate_visual({**visual('architecture', {'focus_layers': ['ai'], 'gap_layers': []}), 'prose': '긴 글'}, CITED)
