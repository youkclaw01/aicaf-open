"""비용·경제성을 AS-IS의 독립 필수 영역으로 정규화하고 안전한 기준 대조를 검증한다."""
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import asis_analysis
from test_asis_connection import synthetic_input


def test_cost_is_first_class_area_with_fact_bound_limits(tmp_path):
    import asis_evidence_views
    data=asis_evidence_views.enrich(synthetic_input(tmp_path))
    result=asis_analysis.analyze(data)
    assert 'cost' in asis_analysis.AREA_NAMES
    area=next(a for a in result['stages'][0]['areas'] if a['area_id']=='cost')
    assert area['observations']
    assert all(o['fact_refs'] for o in area['observations'])
    assert 'WAF-AI-CO1' in area['implication']['criterion_refs']
    assert '현금' in area['implication']['impact']
    assert result['input']['judgments'], '판단 원장이 비어 있음'
    assert any(x['criterion_id'].startswith('AILZ-') for x in result['input']['context']['criterion_assessments'])
    assert not any(x['status']=='compliant' and not x['applicability_confirmed'] for x in result['input']['judgments'])
