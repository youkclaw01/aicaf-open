"""현황분석 진행 표시: 총 18단계 중 현재 단계와 그 단계 안의 진행률.

영석님 지정(2026-10-17): 단일 전체 %는 단위 수가 단계마다 늘어 중간에 내려가므로 'N단계 - P% 진행 중 / 총 18단계'로 나눠 보여 준다.
작업 원장(task-ledger)의 작업 키를 18단계로 매핑하고 실제 완료 사실만 집계한다. 시간·심박·대기량은 완료 증거가 아니다.
모델 호출 없음, 순수 계산. 개정 진행 기록(job.json progress.stage)과 포털 화면이 같은 값을 쓴다.
"""
import re

STAGES = [
    (1, '스냅샷'), (2, '해석'), (3, '영역별 이슈 계획'), (4, '영역 작성·검토'), (5, '영역 시사점'), (6, '종합 계획'),
    (7, '방향 작성·검토'), (8, '종합 전체 검토'), (9, '진단·과제 계획'), (10, '진단·과제 작성·검토'), (11, '요약'),
    (12, '문서 전체 검토'), (13, '시각 명세'), (14, 'PPT·Excel 생성'), (15, '실렌더'), (16, '시각 검토'),
    (17, '항목별 렌더 검토'), (18, '후보 등록'),
]
TOTAL = len(STAGES)
NAMES = dict(STAGES)
# 키 전체가 같을 때만 쓰는 고정 작업(항목 번호가 붙지 않는 작업)
FIXED = {'interpret': 2, 'issue-plan': 3, 'synthesis-plan': 6, 'synthesis': 8, 'action-item-plan': 9, 'actions': 10, 'analysis-content': 10,
         'summary': 11, 'document': 12, 'visual-specs': 13, 'workbook-freeze': 14, 'native-presentation': 14, 'native_pair': 14,
         'actual_render': 15, 'visual_pages': 16, 'pair_consistency': 17}
_RULES = [
    (re.compile(r'^issue-plan-area-'), 3),
    (re.compile(r'^synthesis-plan-area-'), 6),
    (re.compile(r'^action-item-plan-'), 9),
    (re.compile(r'^direction-'), 7),
    (re.compile(r'^(diagnosis|action)-'), 10),
    (re.compile(r'^area-[a-z]+-summary(\.|$)'), 5),
    (re.compile(r'^area-'), 4),
]
DONE = {'completed', 'superseded'}
RUNNING = {'running'}
FAILED = {'failed', 'blocked'}


def stage_of(key):
    """작업 키의 단계 번호. 알 수 없는 키는 임의로 배정하지 않고 None 이다."""
    if key in FIXED:
        return FIXED[key]
    for pattern, stage in _RULES:
        if pattern.match(key):
            return stage
    return None


def _state(units):
    if not units:
        return 'waiting'
    if all(u['status'] == 'completed' for u in units):
        return 'done'
    if any(u['status'] in FAILED for u in units):
        return 'failed'
    if any(u['status'] in RUNNING for u in units):
        return 'running'
    return 'pending'


def compute(tasks, *, job_status):
    """tasks: {키: {'status','weight'}}. 반환: 현재 단계·단계 내 진행률·18단계 상태표·표시 문구."""
    per = {n: [] for n, _ in STAGES}
    for key, row in tasks.items():
        stage = stage_of(key)
        if stage is None or row.get('status') == 'superseded':
            continue
        per[stage].append({'status': row.get('status'), 'weight': float(row.get('weight') or 1.0)})
    started = [n for n in per if per[n]]
    if job_status == 'generated_candidate':
        table = [{'n': n, 'name': NAMES[n], 'state': 'done', 'done_units': len(per[n]), 'total_units': len(per[n])} for n, _ in STAGES]
        return {'current': TOTAL, 'total': TOTAL, 'name': NAMES[TOTAL], 'percent': 100, 'done_units': len(per[TOTAL]), 'total_units': len(per[TOTAL]),
                'completed_stages': TOTAL, 'reopened': False, 'stages': table, 'label': '완료 - 총 %d단계' % TOTAL}
    # 운영 원장은 뒤 단계 작업(종합 전체 검토·요약·문서 검토 등)을 개정 시작 때 미리 등록하고, 방향·진단 항목은 앞 단계가 끝난 뒤 등록한다.
    # 그래서 '뒤 단계가 등록됐다'는 사실만으로 등록 전 중간 단계를 끝난 것으로 보면 안 된다. 뒤 단계에 완료된 작업이 있을 때만 거쳐 간 것이다.
    completed_stages_with_work = [n for n in per if any(u['status'] == 'completed' for u in per[n])]
    last_completed = max(completed_stages_with_work) if completed_stages_with_work else 0
    table = []
    for n, name in STAGES:
        units = per[n]
        state = _state(units)
        if not units:
            state = 'done' if (n < last_completed or (n == 1 and started)) else 'waiting'
        table.append({'n': n, 'name': name, 'state': state, 'done_units': sum(1 for u in units if u['status'] == 'completed'), 'total_units': len(units)})
    # 현재 단계는 아직 끝나지 않은 가장 앞 단계다. 그 뒤 단계는 작업이 미리 등록돼 running 으로 표시되더라도 실제로는 앞 단계가 끝나야 시작하므로 대기로 보인다.
    first_unfinished = min((r['n'] for r in table if r['state'] != 'done'), default=TOTAL)
    for r in table:
        if r['n'] > first_unfinished and r['state'] != 'done':
            r['state'] = 'waiting'
    unfinished = [r['n'] for r in table if r['state'] != 'done']
    current = min(unfinished) if unfinished else TOTAL
    row = table[current - 1]
    units = per[current]
    total_w = sum(u['weight'] for u in units)
    done_w = sum(u['weight'] for u in units if u['status'] == 'completed')
    percent = int(round(100 * done_w / total_w)) if total_w else 0
    # 현재 단계에 무효화된 작업이 있으면 이미 지나간 단계를 다시 여는 보완 중이다(영역 지적으로 하위 단계가 무효가 된 경우).
    reopened = any(u['status'] == 'invalidated' for u in units)
    failed = job_status in ('failed', 'interrupted') or row['state'] == 'failed'
    verb = '중단' if failed else '진행 중'
    return {'current': current, 'total': TOTAL, 'name': NAMES[current], 'percent': percent, 'done_units': row['done_units'], 'total_units': row['total_units'],
            'completed_stages': sum(1 for r in table if r['state'] == 'done' and r['n'] < current), 'reopened': bool(reopened),
            'stages': table, 'label': '%d단계 - %d%% %s / 총 %d단계' % (current, percent, verb, TOTAL)}
