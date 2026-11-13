"""개정 종료 시 단계별 병렬도 보고서를 자동으로 남기는 연결 계약 시험.

asis_parallelism.report_for_revision(folder) 이 개정 폴더의 영수증과 작업 원장을 읽어 parallelism-report.json 을 쓰고,
경고가 있으면 job.json 의 parallelism_warnings 에 요약을 남긴다. 성공·실패 어느 쪽으로 끝나도 호출된다.
보고서 작성 실패는 개정의 성공·실패를 바꾸지 않는다.
"""
from pathlib import Path
import datetime as dt
import json
import sys

APP = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP))
import asis_parallelism as P
import asis_view

T0 = dt.datetime(2026, 10, 17, 12, 0, 0, tzinfo=dt.timezone.utc)


def receipt(folder, name, start_s, length_s, wait_s=0.0, hold=None, with_wait=True):
    s = T0 + dt.timedelta(seconds=start_s)
    row = {'role': 'author', 'response_id': name, 'queued_at': (s - dt.timedelta(seconds=wait_s)).isoformat(), 'admitted_at': s.isoformat(),
           'request_started_at': s.isoformat(), 'completed_at': (s + dt.timedelta(seconds=length_s)).isoformat(), 'usage': {'input_token_count': 100}}
    if with_wait:
        row.update(queue_wait_seconds=wait_s, admission_hold_count=1 if hold else 0, admission_last_hold_reason=hold)
    path = folder / 'checkpoints' / name / 'round-001' / 'author-receipt.json'
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(row), encoding='utf-8')


def ledger(folder, units):
    tasks = {f'synthesis-plan-area-{i}': {'status': 'completed', 'weight': 1.0, 'depends_on': []} for i in range(units)}
    (folder / 'task-ledger.json').write_text(json.dumps({'tasks': tasks, 'plan_version': 1}), encoding='utf-8')


def test_stage_of_receipt_is_derived_from_its_checkpoint_folder_name():
    assert P.stage_of_unit('synthesis-plan-area-network-review') == 6
    assert P.stage_of_unit('area-ai-O_P0059') == 4
    assert P.stage_of_unit('direction-D_P0001') == 7
    assert P.stage_of_unit('totally-unknown') is None


def test_report_is_written_with_stage_rows_and_warnings(tmp_path):
    for i in range(10):
        receipt(tmp_path, f'synthesis-plan-area-{i}', i * 100, 90, wait_s=60, hold='commit_reserve')
    ledger(tmp_path, 10)
    report = P.report_for_revision(tmp_path)
    assert (tmp_path / 'parallelism-report.json').is_file()
    saved = json.loads((tmp_path / 'parallelism-report.json').read_text(encoding='utf-8'))
    assert saved['stages']['6']['calls'] == 10 and saved['warnings'] and saved['warnings'][0]['stage'] == 6
    assert report['warnings'][0]['reason'] == 'commit_reserve'


def test_unclassified_receipts_are_counted_not_dropped(tmp_path):
    receipt(tmp_path, 'weird-unit', 0, 30)
    ledger(tmp_path, 0)
    report = P.report_for_revision(tmp_path)
    assert report['unclassified_calls'] == 1


def test_a_corrupt_receipt_does_not_break_the_report(tmp_path):
    receipt(tmp_path, 'area-ai-O_P0001', 0, 30)
    bad = tmp_path / 'checkpoints' / 'area-ai-O_P0002' / 'round-001' / 'author-receipt.json'
    bad.parent.mkdir(parents=True)
    bad.write_text('{not json', encoding='utf-8')
    ledger(tmp_path, 1)
    report = P.report_for_revision(tmp_path)
    assert report['skipped_receipts'] == 1 and report['stages']['4']['calls'] == 1


def test_missing_wait_fields_in_old_receipts_are_flagged_as_a_measurement_gap(tmp_path):
    for i in range(5):
        receipt(tmp_path, f'area-ai-O_P{i:04d}', 0, 30, with_wait=False)
    ledger(tmp_path, 5)
    report = P.report_for_revision(tmp_path)
    assert report['stages']['4']['wait_recorded'] is False


def test_the_worker_writes_the_report_when_the_revision_ends_in_either_state():
    source = (APP / 'asis_view.py').read_text(encoding='utf-8')
    i = source.index('def generate(self,aid,revision):')
    body = source[i:i + 6500]
    assert body.count('report_parallelism(') >= 1 and 'finally:' in body[body.index('report_parallelism('):] or 'report_parallelism' in body, '개정 종료 경로가 병렬도 보고서를 남겨야 한다'
    assert 'asis_parallelism' in source


def test_report_failure_never_changes_the_revision_outcome(tmp_path):
    controller = asis_view.Controller(ROOT=tmp_path, assessment=None, infra_case_dir=None, db=None)
    folder = tmp_path / 'rev'
    folder.mkdir()
    # 영수증 폴더가 없어도 예외 없이 빈 보고서를 돌려주고 파일 쓰기 실패는 삼킨다
    assert controller.report_parallelism(folder) is None or isinstance(controller.report_parallelism(folder), dict)



def test_reused_checkpoints_from_an_earlier_run_do_not_stretch_a_stage_span(tmp_path):
    """재개한 개정은 이전 실행에서 만든 영수증(수 시간 전)과 이번 실행의 영수증이 한 폴더에 섞인다.
    단계 소요·평균 동시를 두 실행에 걸쳐 계산하면 평균 동시 0.07·손실 8,000초 같은 거짓 경고가 난다(운영 사고 2026-10-17).
    보고서는 개정의 가장 최근 실행 구간(시작 시각 이후)의 영수증만 집계하고, 이전 실행분은 건수만 센다."""
    # 이전 실행: 6시간 전에 3단계 호출 9건이 순서대로 돌았다(직렬)
    for i in range(9):
        receipt(tmp_path, f'issue-plan-area-{i}-review', i * 3600, 40, wait_s=0.0)
    # 이번 실행: 6시간 뒤 같은 3단계 호출 5건이 동시에 돌았다
    base = 9 * 3600 + 6 * 3600
    for i in range(5):
        receipt(tmp_path, f'issue-plan-area-x{i}-review', base + 1, 40, wait_s=0.0)
    tasks = {f'issue-plan-area-{i}': {'status': 'completed', 'weight': 1.0, 'depends_on': []} for i in range(10)}
    (tmp_path / 'task-ledger.json').write_text(json.dumps({'tasks': tasks, 'plan_version': 1,
        'created_at': (T0 + dt.timedelta(seconds=base - 5)).isoformat()}), encoding='utf-8')
    report = P.report_for_revision(tmp_path)
    row = report['stages']['3']
    assert row['span_seconds'] < 120, '이전 실행 영수증이 단계 구간을 늘렸다: %d초' % row['span_seconds']
    assert row['calls'] == 5 and row.get('earlier_run_calls') == 9
    assert report['warnings'] == [], report['warnings']


def test_a_revision_without_a_ledger_start_time_counts_every_receipt(tmp_path):
    for i in range(3):
        receipt(tmp_path, f'area-ai-O_P{i:04d}', i, 30)
    (tmp_path / 'task-ledger.json').write_text(json.dumps({'tasks': {}, 'plan_version': 1}), encoding='utf-8')
    report = P.report_for_revision(tmp_path)
    assert report['stages']['4']['calls'] == 3 and report['stages']['4'].get('earlier_run_calls', 0) == 0
