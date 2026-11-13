"""포털 진행 표시: 총 18단계 중 현재 단계와 그 단계 안의 진행률(영석님 지정 2026-10-17).

단일 전체 %는 단위 수가 단계마다 늘어나 중간에 내려가므로 믿을 수 없다. 작업 원장의 작업 키를 18개 단계로 매핑하고,
'N단계 - P% 진행 중 / 총 18단계'를 계산해 개정 진행 기록(progress.stage)에 싣는다. 실제 완료 사실만 집계하며 시간·심박은 쓰지 않는다.
"""
from pathlib import Path
import sys

APP = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP))
import asis_stages as S


def task(status, weight=1.0):
    return {'status': status, 'weight': weight}


def ledger(**groups):
    """groups: 키 접두 -> [상태...]. 키는 접두-번호로 펼친다."""
    rows = {}
    for prefix, states in groups.items():
        for i, st in enumerate(states):
            rows[prefix if len(states) == 1 and prefix in S.FIXED else f'{prefix}-{i}'] = task(st)
    return rows


def test_there_are_exactly_eighteen_named_stages_in_order():
    assert [n for n, _ in S.STAGES] == list(range(1, 19))
    assert [name for _, name in S.STAGES][:3] == ['스냅샷', '해석', '영역별 이슈 계획']
    assert S.STAGES[6] == (7, '방향 작성·검토') and S.STAGES[-1] == (18, '후보 등록')


def test_task_keys_map_to_the_stage_they_belong_to():
    cases = {
        'interpret': 2, 'issue-plan': 3, 'issue-plan-area-network': 3, 'issue-plan-area-network-review': 3,
        'area-ai-O_P0059': 4, 'area-ai-O_P0059.author-verified': 4, 'area-ai-O_P0059.reviewer-verified': 4, 'area-network': 4,
        'area-ai-summary': 5, 'area-ai-summary.reviewer-verified': 5,
        'synthesis-plan': 6, 'synthesis-plan-area-cost': 6, 'synthesis-plan-area-cost-review': 6,
        'direction-D_P0003': 7, 'direction-D_P0003.author-verified': 7,
        'synthesis': 8,
        'action-item-plan': 9, 'action-item-plan-area-ai': 9,
        'diagnosis-J_ITEM_0001': 10, 'action-A_ITEM_0001': 10, 'actions': 10, 'analysis-content': 10,
        'summary': 11, 'document': 12, 'visual-specs': 13,
        'workbook-freeze': 14, 'native-presentation': 14, 'native_pair': 14,
        'actual_render': 15, 'visual_pages': 16, 'pair_consistency': 17,
    }
    for key, stage in cases.items():
        assert S.stage_of(key) == stage, (key, S.stage_of(key), stage)


def test_unknown_task_keys_are_not_silently_assigned():
    assert S.stage_of('something-unknown') is None


def test_current_stage_is_the_first_stage_with_unfinished_work_and_percent_is_within_it():
    rows = {'interpret': task('completed'), 'issue-plan': task('completed'),
            'issue-plan-area-a': task('completed'), 'issue-plan-area-b': task('completed'),
            'area-a-O_P0001': task('completed'), 'area-a-O_P0002': task('running'), 'area-a-O_P0003': task('pending'), 'area-a-O_P0004': task('completed')}
    stage = S.compute(rows, job_status='running')
    assert stage['current'] == 4 and stage['total'] == 18 and stage['name'] == '영역 작성·검토'
    assert stage['done_units'] == 2 and stage['total_units'] == 4 and stage['percent'] == 50
    assert stage['label'] == '4단계 - 50% 진행 중 / 총 18단계'
    assert stage['completed_stages'] == 3


def test_percent_uses_task_weights_not_counts():
    rows = {'area-a-O_P0001': task('completed', 3.0), 'area-a-O_P0002': task('pending', 1.0)}
    assert S.compute(rows, job_status='running')['percent'] == 75


def test_current_stage_never_moves_backward_when_a_later_stage_registers_work():
    early = {'area-a-O_P0001': task('completed'), 'area-a-O_P0002': task('running')}
    assert S.compute(early, job_status='running')['current'] == 4
    # 4단계가 아직 끝나지 않았는데 뒤 단계 작업이 등록돼도 현재 단계는 미완료 최소 단계다.
    mixed = dict(early, **{'synthesis-plan': task('pending')})
    assert S.compute(mixed, job_status='running')['current'] == 4


def test_a_reopened_earlier_stage_is_the_current_stage_and_is_marked():
    rows = {'interpret': task('completed'), 'area-a-O_P0001': task('invalidated'), 'synthesis-plan': task('completed'),
            'direction-D_P0001': task('running')}
    stage = S.compute(rows, job_status='running')
    assert stage['current'] == 4 and stage['reopened'] is True


def test_stage_table_lists_status_per_stage_for_the_portal():
    rows = {'interpret': task('completed'), 'issue-plan': task('completed'), 'area-a-O_P0001': task('running')}
    table = S.compute(rows, job_status='running')['stages']
    assert [r['n'] for r in table] == list(range(1, 19))
    states = {r['n']: r['state'] for r in table}
    assert states[2] == 'done' and states[3] == 'done' and states[4] == 'running' and states[7] == 'waiting'


def test_stages_without_registered_tasks_before_the_current_one_count_as_done_only_if_a_later_stage_has_started():
    # 운영 원장은 영역 작업과 함께 이슈 계획·영역 시사점 작업을 항상 등록한다. 완료된 뒤 단계가 있어야 등록 안 된 앞 단계가 거쳐 간 것이다.
    rows = {'interpret': task('completed'), 'issue-plan': task('completed'), 'area-a-O_P0001': task('completed'), 'area-a-summary': task('completed'),
            'synthesis-plan': task('running')}
    stage = S.compute(rows, job_status='running')
    states = {r['n']: r['state'] for r in stage['stages']}
    assert states[1] == 'done', '스냅샷은 개정이 시작되면 끝난 것이다'
    assert states[6] == 'running' and stage['current'] == 6


def test_generated_candidate_is_all_eighteen_done():
    rows = {'interpret': task('completed'), 'native_pair': task('completed'), 'pair_consistency': task('completed')}
    stage = S.compute(rows, job_status='generated_candidate')
    assert stage['current'] == 18 and stage['percent'] == 100 and stage['completed_stages'] == 18
    assert all(r['state'] == 'done' for r in stage['stages']) and stage['label'] == '완료 - 총 18단계'


def test_failed_job_keeps_the_stage_where_it_stopped():
    rows = {'interpret': task('completed'), 'issue-plan': task('completed'), 'issue-plan-area-a': task('completed'), 'area-a-O_P0001': task('failed')}
    stage = S.compute(rows, job_status='failed')
    assert stage['current'] == 4 and stage['label'].startswith('4단계 - ') and stage['label'].endswith('중단 / 총 18단계')


def test_preregistered_pending_later_stages_do_not_mark_unregistered_middle_stages_done():
    """운영 원장은 뒤 단계 작업(종합 전체 검토·요약·문서 검토 등)을 개정 시작 때 미리 등록한다. 그 작업이 대기·실행 중이라고 해서
    아직 등록되지 않은 중간 단계(방향 작성은 종합 계획이 끝난 뒤에 등록)를 끝난 것으로 보면 안 된다(실측: 7단계가 done 으로 표시됨)."""
    rows = {'interpret': task('completed'), 'area-a-O_P0001': task('completed'), 'area-a-summary': task('completed'),
            'synthesis-plan': task('running'), 'synthesis-plan-area-a': task('running'),
            'synthesis': task('running'), 'summary': task('pending'), 'document': task('pending')}
    stage = S.compute(rows, job_status='running')
    states = {r['n']: r['state'] for r in stage['stages']}
    assert stage['current'] == 6
    assert states[7] == 'waiting', '등록되지 않은 방향 작성 단계는 대기여야 한다'
    assert states[6] == 'running'
    assert states[8] == 'waiting', '종합 전체 검토는 미리 등록돼 running 으로 표시돼도 6단계가 끝나기 전에는 대기다(병렬 실행으로 오해하지 않게)'


def test_an_unregistered_middle_stage_is_done_only_after_a_later_stage_has_completed_work():
    rows = {'interpret': task('completed'), 'synthesis-plan': task('completed'), 'synthesis': task('completed'), 'actions': task('running')}
    states = {r['n']: r['state'] for r in S.compute(rows, job_status='running')['stages']}
    assert states[7] == 'done', '뒤 단계(종합 전체 검토)가 완료됐다면 그 앞의 방향 작성은 거친 것이다'


def test_stages_after_the_current_one_are_shown_as_waiting_even_if_their_task_is_marked_running():
    """종합 전체 검토(synthesis)는 개정 시작 때 미리 등록되고 'running' 으로 표시되지만 실제 작업은 종합 계획·방향 작성이 끝난 뒤에 한다.
    화면에서 6단계와 8단계가 동시에 '진행 중'으로 보이면 사용자가 병렬 실행으로 오해한다(운영 실측)."""
    rows = {'interpret': task('completed'), 'area-a-O_P0001': task('completed'), 'area-a-summary': task('completed'),
            'synthesis-plan': task('running'), 'synthesis-plan-area-a': task('running'), 'synthesis': task('running'), 'summary': task('pending')}
    stage = S.compute(rows, job_status='running')
    states = {r['n']: r['state'] for r in stage['stages']}
    assert stage['current'] == 6 and states[6] == 'running'
    assert states[8] == 'waiting' and states[11] == 'waiting', '현재 단계 뒤의 단계는 대기여야 한다'


def test_waiting_stages_do_not_show_unit_counts():
    rows = {'area-a-O_P0001': task('running'), 'summary': task('pending')}
    stage = S.compute(rows, job_status='running')
    later = [r for r in stage['stages'] if r['n'] > stage['current']]
    assert later and all(r['state'] == 'waiting' for r in later)


def test_a_stage_can_run_in_parallel_with_the_current_one_only_if_it_is_the_current_one():
    # 운영 원장은 영역 작업과 함께 이슈 계획 작업(3단계)을 항상 등록하고 완료한다. 앞 단계가 거쳐 간 상태에서 영역 작업이 실패한 경우다.
    rows = {'interpret': task('completed'), 'issue-plan': task('completed'), 'issue-plan-area-a': task('completed'),
            'area-a-O_P0001': task('failed'), 'synthesis-plan': task('running')}
    stage = S.compute(rows, job_status='running')
    assert stage['current'] == 4
    assert {r['n']: r['state'] for r in stage['stages']}[6] == 'waiting'
