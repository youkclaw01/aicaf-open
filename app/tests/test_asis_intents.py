"""고객 발언·추가 요구의 해석을 사실·원문과 결합하는 결정적 계약 시험이다."""
from pathlib import Path
from copy import deepcopy
import importlib.util
import sys
import pytest

APP = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP))


def test_intent_binding_preserves_quote_and_never_promotes_to_operating_fact():
    assert importlib.util.find_spec('asis_pipeline') is not None, 'AS-IS intent pipeline missing'
    import asis_pipeline
    import asis_analysis
    data, _ = asis_analysis.contract().synthetic_fixture()
    data['context']['requirements'] = '공용 접근은 열지 말고 사설 연결만 검증'
    data['facts'][0].update(id='F_IV_Q1_transcript', value='고객 문서를 외부에 보내지 않고 내부 직원이 검색하도록 하고 싶습니다.')
    data['measurements'] = []
    payload = {'goals':[{'source_fact_id':'F_IV_Q1_transcript','quote':'내부 직원이 검색하도록 하고 싶습니다.',
        'interpretation':'내부 직원용 검색 도입 목표','area_refs':['network','data'],'ambiguity':''}],
        'requirements':[{'quote':'공용 접근은 열지 말고 사설 연결만 검증','interpretation':'공용 접근 확대 없이 사설 경로 검증',
                         'area_refs':['network'],'status':'applied_as_analysis_constraint'}]}
    bound = asis_pipeline.bind_intents(data, payload)
    assert bound['goals'][0]['quote'] in data['facts'][0]['value']
    assert bound['context']['current_operations_verified'] is False
    assert bound['facts'] == data['facts']
    assert bound['context']['requirements_application']['customer_fact_promoted'] is False
    bad = deepcopy(payload);bad['goals'][0]['quote'] = '원문에 없는 허구'
    with pytest.raises(ValueError, match='goal_quote_not_in_source'):
        asis_pipeline.bind_intents(data, bad)
