"""단계별 병렬도 감시 계약 시험(합성 호출 기록, 모델 호출 없음).

2026-10-17 운영: 6단계 종합 계획은 독립 10개 작업인데 평균 동시 1.7, 40.8분이 걸렸다. 방향 작성은 독립 87개 중 2개만 돌았다.
이 사실은 영수증에 대기 시간이 없어 사후 분석 전까지 알려지지 않았다.

asis_parallelism.analyze(calls, stages) 가 다음을 계산한다(순수 계산):
- 단계별 호출 수·구간(분)·평균 동시·최대 동시·호출 중앙값·최장
- 단계별 '이상 병렬도'(독립 단위 수와 호출 길이로 계산한 이론 소요) 대비 실제 소요와 손실 시간
- 경고 판정: 독립 단위가 충분한데(>=4) 평균 동시가 독립 단위의 일정 비율(기본 25%) 미만이고 그 단계의 거절 사유가 자원 제어(memory/commit)인 경우
"""
from pathlib import Path
import datetime as dt
import sys

APP = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP))
import asis_parallelism as P

T0 = dt.datetime(2026, 10, 17, 12, 0, 0, tzinfo=dt.timezone.utc)


def call(stage, start_s, length_s, wait_s=0.0, hold=None):
    s = T0 + dt.timedelta(seconds=start_s)
    e = s + dt.timedelta(seconds=length_s)
    return {'stage': stage, 'queued_at': (s - dt.timedelta(seconds=wait_s)).isoformat(), 'admitted_at': s.isoformat(),
            'request_started_at': s.isoformat(), 'completed_at': e.isoformat(), 'queue_wait_seconds': wait_s, 'last_hold_reason': hold}


def test_stage_stats_count_overlap_and_span():
    calls = [call(4, 0, 60), call(4, 10, 60), call(4, 20, 60), call(4, 200, 60)]
    out = P.analyze(calls, {4: {'independent_units': 4}})
    row = out['stages'][4]
    assert row['calls'] == 4 and row['peak'] == 3
    assert 0 < row['avg_concurrency'] < 3
    assert row['span_seconds'] == 260


def test_ideal_time_is_the_longest_call_when_all_units_are_independent_and_capacity_is_enough():
    calls = [call(6, i * 100, 90) for i in range(10)]    # 10개를 직렬로 돌린 상태(구간 990초, 호출 90초)
    out = P.analyze(calls, {6: {'independent_units': 10}})
    row = out['stages'][6]
    assert row['ideal_seconds'] == 90, '독립 10개를 모두 동시에 돌리면 가장 긴 호출 길이가 이상 소요다'
    assert row['loss_seconds'] == row['span_seconds'] - 90 and row['loss_seconds'] > 800


def test_a_serialized_stage_with_resource_holds_raises_a_warning():
    calls = [call(6, i * 100, 90, wait_s=60, hold='commit_reserve') for i in range(10)]
    out = P.analyze(calls, {6: {'independent_units': 10}})
    warn = [w for w in out['warnings'] if w['stage'] == 6]
    assert warn and warn[0]['cause'] == 'resource_admission' and warn[0]['reason'] == 'commit_reserve'
    assert warn[0]['independent_units'] == 10 and warn[0]['avg_concurrency'] < 2.5


def test_a_stage_that_is_naturally_serial_does_not_warn():
    calls = [call(2, 0, 120)]
    out = P.analyze(calls, {2: {'independent_units': 1}})
    assert out['warnings'] == []


def test_a_well_parallel_stage_does_not_warn_even_with_a_few_holds():
    calls = [call(4, 0, 100, wait_s=5, hold='memory_reserve') for _ in range(40)]
    out = P.analyze(calls, {4: {'independent_units': 40}})
    assert out['warnings'] == []


def test_low_parallelism_without_resource_holds_is_reported_with_an_unknown_cause():
    """자원 거절이 없는데 동시 수가 낮으면 원인은 자원이 아니다(의존·직렬 잠금·공급자). 원인을 지어내지 않는다."""
    calls = [call(3, i * 100, 90) for i in range(8)]
    out = P.analyze(calls, {3: {'independent_units': 8}})
    warn = out['warnings']
    assert warn and warn[0]['cause'] == 'not_resource_admission'


def test_missing_wait_fields_are_reported_as_a_measurement_gap_not_as_zero_wait():
    calls = [{'stage': 4, 'queued_at': (T0).isoformat(), 'admitted_at': T0.isoformat(), 'request_started_at': T0.isoformat(),
              'completed_at': (T0 + dt.timedelta(seconds=60)).isoformat()} for _ in range(3)]
    out = P.analyze(calls, {4: {'independent_units': 3}})
    assert out['stages'][4]['wait_recorded'] is False


def test_a_stage_with_only_a_few_independent_units_is_not_flagged_even_when_it_ran_serially():
    """독립 단위가 3개뿐이면 3개를 순서대로 돌려도 손실이 작아 경고 대상이 아니다(최소 단위 수 경계)."""
    calls = [call(9, i * 400, 300, wait_s=60, hold='commit_reserve') for i in range(3)]
    out = P.analyze(calls, {9: {'independent_units': 3}})
    assert out['warnings'] == []


def test_the_same_serial_run_is_flagged_when_there_were_enough_independent_units():
    calls = [call(9, i * 400, 300, wait_s=60, hold='commit_reserve') for i in range(4)]
    out = P.analyze(calls, {9: {'independent_units': 4}})
    assert [w['stage'] for w in out['warnings']] == [9]


def test_a_stage_that_ran_at_the_peak_the_host_allowed_is_not_flagged_even_with_many_independent_units():
    """운영 4단계: 독립 259개, 평균 동시 18.8, 최대 21. 비율(7%)만 보면 오탐이다. 최대 동시 수가 단계 내내 유지됐다면 한도가 병목이다."""
    calls = [call(4, (i // 20) * 60, 58) for i in range(260)]          # 20개씩 13묶음
    out = P.analyze(calls, {4: {'independent_units': 260}})
    row = out['stages'][4]
    assert row['peak'] == 20 and row['avg_concurrency'] >= 16
    assert out['warnings'] == [], '평균 동시가 최대 동시에 가까우면(허용 한도까지 쓴 단계) 경고하지 않는다'


def test_a_stage_that_stayed_far_below_its_own_peak_is_flagged():
    """최대 동시는 높았지만 대부분의 시간은 낮았다 -> 자원 판정이 중간에 막힌 단계."""
    calls = [call(4, 0, 30) for _ in range(20)] + [call(4, 40 + i * 100, 90, wait_s=50, hold='commit_reserve') for i in range(10)]
    out = P.analyze(calls, {4: {'independent_units': 30}})
    assert [w['stage'] for w in out['warnings']] == [4]
    assert out['warnings'][0]['cause'] == 'resource_admission'


def test_stage_with_zero_registered_units_is_not_flagged():
    calls = [call(7, i * 100, 90) for i in range(5)]
    out = P.analyze(calls, {})
    assert out['warnings'] == []
