"""방향 단독 검토가 다른 방향 본문 대조 때문에 blocked가 된 경우만 병합 검토로 넘기고, 사실·근거 확인 불가는 계속 차단한다."""
from pathlib import Path
import inspect, sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import asis_pipeline
from asis_pipeline import _cross_item_only

REAL = {'fact_refs': ['F_T_foundryAccountConfiguration'],
        'reason': '제공된 draft와 할당 슬롯에는 D_P0073 및 I_ai 연결만 있다. D_P0019·D_P0121·D_P0182의 실제 내용과 관련 영역 참조가 제공되지 않아 공통 방향으로의 통합 또는 독립 방향 간 역할 구분을 대조할 수 없다.',
        'required_evidence': '동일 run·분석 개정의 D_P0019·D_P0121·D_P0182 본문, 관련 영역별 승인 시사점의 ID·본문·사실 참조, 전체 방향 참조와 edges 및 통합 결정 자료.',
        'topic': '공통 변경 통제의 영역 간 연결과 중복 해소 확인'}


def test_cross_direction_comparison_request_is_deferred():
    assert _cross_item_only(REAL, 'direction-D_P0073') is True


def test_fact_value_or_unrelated_requests_stay_blocking():
    fact = {'fact_refs': ['F_T_x'], 'reason': '인용된 F_T_x의 값이 제공되지 않아 확인할 수 없다.',
            'required_evidence': 'F_T_x 원문 수집 값', 'topic': '근거 값 확인'}
    assert _cross_item_only(fact, 'direction-D_P0073') is False
    mixed = dict(REAL, required_evidence='D_P0019 본문과 F_T_x 원문 수집 값')
    assert _cross_item_only(mixed, 'direction-D_P0073') is False, 'a request that also needs fact values must still block'
    own = dict(REAL, reason='D_P0073의 근거가 제공되지 않아 다른 방향과 대조할 수 없다.', required_evidence='D_P0073 본문')
    assert _cross_item_only(own, 'direction-D_P0073') is False, 'only references to its own ID must not defer'


def test_deferral_is_limited_to_direction_stage_without_defects():
    source = inspect.getsource(asis_pipeline.Pipeline._reviewed_stage)
    branch = source[source.index("if review['verdict'] == 'blocked':"):]
    assert 'stage == 2' in branch and "not review['defects']" in branch and 'not deterministic_defects' in branch
