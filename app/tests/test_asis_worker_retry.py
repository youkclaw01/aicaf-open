"""동결된 실패 개정의 제한 재실행이 원문·실패 기록·프로세스 소유권을 보존하는지 검증한다."""
from copy import deepcopy
from pathlib import Path
import hashlib
import sys
import time

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import asis_runtime
import asis_view
import asis_worker

AID = 999997
REVISION = 'r-20000101T000000-0000000000000002'
IMPLEMENTATION = 'a' * 64


def fixture(tmp_path, monkeypatch):
    control = asis_view.Controller(ROOT=tmp_path, assessment=None, infra_case_dir=None, db=None)
    folder = control.job_dir(AID, REVISION)
    folder.mkdir(parents=True)
    snapshot = asis_view.asis_model.encoded({'run': {'assessment_id': AID, 'analysis_revision': REVISION}})
    (folder / 'snapshot.json').write_bytes(snapshot)
    asis_view.atomic(folder / 'revision-context.json', {'requirements': '', 'prior_output': None})
    row = {'run': {'assessment_id': AID, 'analysis_revision': REVISION}, 'revision_id': REVISION,
           'status': 'failed', 'pid': 1001, 'process_identity': 'old-owner',
           'snapshot_sha256': hashlib.sha256(snapshot).hexdigest(),
           'requested_model_policy': asis_runtime.policy(),
           'error': 'AgentException:Timeout after 600s waiting for session.idle',
           'finished_at': '2000-01-01T00:10:00+00:00',
           'progress': {'overall_percent': 10, 'units': [
               {'unit': 'interpret', 'phase': 'interpret', 'status': 'completed'},
               {'unit': 'issue-plan', 'phase': 'interpret', 'status': 'running'}]}}
    asis_view.atomic(folder / 'job.json', row)
    asis_view.atomic(control.folder(AID) / 'state.json', {'latest_revision_id': REVISION})
    monkeypatch.setattr(asis_runtime, 'implementation_fingerprint', lambda: IMPLEMENTATION)
    monkeypatch.setattr(asis_worker, 'process_identity', lambda pid: None)
    kwargs = {'retry_id': 'approved-unit-retry', 'expected_snapshot_sha256': row['snapshot_sha256'],
              'expected_implementation_sha256': IMPLEMENTATION}
    return control, folder, snapshot, row, kwargs


def test_failed_owner_retry_queues_same_snapshot_and_archives_exact_failure(tmp_path, monkeypatch):
    control, folder, snapshot, row, kwargs = fixture(tmp_path, monkeypatch)
    original = (folder / 'job.json').read_bytes()
    result = control.retry_existing(AID, REVISION, **kwargs)
    assert result['status'] == 'queued' and result['pid'] is None
    assert result['retry_count'] == 1 and result['retry_id'] == kwargs['retry_id']
    assert result['revision_id'] == REVISION and result['run'] == row['run']
    assert result['progress'] == row['progress']
    assert (folder / 'snapshot.json').read_bytes() == snapshot
    assert (folder / 'retry-history' / kwargs['retry_id'] / 'job-before.json').read_bytes() == original
    assert 'error' not in result and 'finished_at' not in result
    once = (folder / 'job.json').read_bytes()
    assert control.retry_existing(AID, REVISION, **kwargs) == result
    assert (folder / 'job.json').read_bytes() == once


@pytest.mark.parametrize('identity', ['living-owner', 'unverifiable'])
def test_retry_refuses_living_or_unverifiable_owner_without_mutation(tmp_path, monkeypatch, identity):
    control, folder, snapshot, row, kwargs = fixture(tmp_path, monkeypatch)
    if identity!='unverifiable':
        row['process_identity']=identity
        asis_view.atomic(folder/'job.json',row)
    before = (folder / 'job.json').read_bytes()
    monkeypatch.setattr(asis_worker, 'process_identity', lambda pid: identity)
    with pytest.raises(ValueError, match='retry_owner_not_proven_dead'):
        control.retry_existing(AID, REVISION, **kwargs)
    assert (folder / 'job.json').read_bytes() == before
    assert (folder / 'snapshot.json').read_bytes() == snapshot


@pytest.mark.parametrize('mismatch', ['snapshot', 'implementation', 'policy', 'latest', 'budget'])
def test_retry_rejects_binding_drift_and_exhausted_budget(tmp_path, monkeypatch, mismatch):
    control, folder, snapshot, row, kwargs = fixture(tmp_path, monkeypatch)
    if mismatch == 'snapshot':
        kwargs['expected_snapshot_sha256'] = 'b' * 64
    elif mismatch == 'implementation':
        kwargs['expected_implementation_sha256'] = 'b' * 64
    elif mismatch == 'policy':
        row['requested_model_policy'] = {**deepcopy(asis_runtime.policy()), 'fallback_allowed': True}
        asis_view.atomic(folder / 'job.json', row)
    elif mismatch == 'latest':
        asis_view.atomic(control.folder(AID) / 'state.json', {'latest_revision_id': 'another-revision'})
    else:
        row['retry_count'] = 1
        asis_view.atomic(folder / 'job.json', row)
    before = (folder / 'job.json').read_bytes()
    with pytest.raises(ValueError):
        control.retry_existing(AID, REVISION, **kwargs)
    assert (folder / 'job.json').read_bytes() == before
    assert (folder / 'snapshot.json').read_bytes() == snapshot



def test_a_stopped_running_revision_with_a_dead_owner_is_retried_through_the_approved_path(tmp_path, monkeypatch):
    """운영 사례: 개정의 워커를 종료하면 job.json 은 running(하트비트 정지)으로 남고 소유자 프로세스는 없다.
    Controller.job() 은 이를 interrupted 로 읽는다. 승인된 재대기 경로가 이 상태를 받아들여 같은 개정을 queued 로 되돌려야 한다."""
    control, folder, snapshot, row, kwargs = fixture(tmp_path, monkeypatch)
    stopped = {k: v for k, v in row.items() if k not in ('error', 'finished_at')}
    stopped.update(status='running', pid=4312, process_identity='dead-owner', started_epoch=time.time() - 7200, heartbeat_epoch=time.time() - 2200)
    asis_view.atomic(folder / 'job.json', stopped)
    assert control.job(AID, REVISION)['status'] == 'interrupted', '디스크가 running 이어도 하트비트 정지+소유자 없음이면 interrupted 로 읽혀야 한다'
    original = (folder / 'job.json').read_bytes()
    result = control.retry_existing(AID, REVISION, **kwargs)
    assert result['status'] == 'queued' and result['pid'] is None and result['retry_count'] == 1
    assert (folder / 'retry-history' / kwargs['retry_id'] / 'job-before.json').read_bytes() == original, '원래의 running 기록이 그대로 보존돼야 한다'
    assert (folder / 'snapshot.json').read_bytes() == snapshot


def test_a_running_revision_with_a_fresh_heartbeat_is_not_retried(tmp_path, monkeypatch):
    control, folder, snapshot, row, kwargs = fixture(tmp_path, monkeypatch)
    live = {k: v for k, v in row.items() if k not in ('error', 'finished_at')}
    live.update(status='running', pid=4312, process_identity='owner', started_epoch=time.time() - 60, heartbeat_epoch=time.time() - 5)
    asis_view.atomic(folder / 'job.json', live)
    before = (folder / 'job.json').read_bytes()
    with pytest.raises(ValueError, match='retry_requires_terminal_failure'):
        control.retry_existing(AID, REVISION, **kwargs)
    assert (folder / 'job.json').read_bytes() == before



def _exhausted(tmp_path, monkeypatch):
    """운영 사례: 첫 재대기(retry_count=1)를 쓴 개정이 다른 결함으로 다시 실패했다."""
    control, folder, snapshot, row, kwargs = fixture(tmp_path, monkeypatch)
    row.update(retry_count=1, retry_id='first-retry', retry_implementation_sha256='c' * 64)
    asis_view.atomic(folder / 'job.json', row)
    kwargs2 = {'retry_id': 'second-retry-after-code-fix', 'expected_snapshot_sha256': kwargs['expected_snapshot_sha256'],
               'expected_implementation_sha256': IMPLEMENTATION}
    return control, folder, snapshot, row, kwargs2


def test_a_second_retry_is_allowed_only_after_the_implementation_changed(tmp_path, monkeypatch):
    """첫 재대기 때와 구현 지문이 달라졌다 = 실패 원인을 고친 코드가 배포됐다. 이때만 같은 개정을 한 번 더 재대기한다(체크포인트·원문 보존)."""
    control, folder, snapshot, row, kwargs = _exhausted(tmp_path, monkeypatch)
    original = (folder / 'job.json').read_bytes()
    result = control.retry_after_code_fix(AID, REVISION, **kwargs)
    assert result['status'] == 'queued' and result['pid'] is None
    assert result['retry_count'] == 2 and result['retry_id'] == kwargs['retry_id']
    assert result['retry_implementation_sha256'] == IMPLEMENTATION
    assert (folder / 'snapshot.json').read_bytes() == snapshot
    assert (folder / 'retry-history' / kwargs['retry_id'] / 'job-before.json').read_bytes() == original
    assert 'error' not in result and 'finished_at' not in result
    once = (folder / 'job.json').read_bytes()
    assert control.retry_after_code_fix(AID, REVISION, **kwargs) == result      # 같은 승인 ID 재전달은 확대되지 않는다
    assert (folder / 'job.json').read_bytes() == once


def test_a_second_retry_is_refused_when_the_code_did_not_change(tmp_path, monkeypatch):
    control, folder, snapshot, row, kwargs = _exhausted(tmp_path, monkeypatch)
    row['retry_implementation_sha256'] = IMPLEMENTATION          # 첫 재대기 때와 같은 코드
    asis_view.atomic(folder / 'job.json', row)
    before = (folder / 'job.json').read_bytes()
    with pytest.raises(ValueError, match='retry_code_not_changed'):
        control.retry_after_code_fix(AID, REVISION, **kwargs)
    assert (folder / 'job.json').read_bytes() == before


def test_a_fifteenth_retry_is_always_refused(tmp_path, monkeypatch):
    control, folder, snapshot, row, kwargs = _exhausted(tmp_path, monkeypatch)
    row['retry_count'] = 14
    asis_view.atomic(folder / 'job.json', row)
    before = (folder / 'job.json').read_bytes()
    with pytest.raises(ValueError, match='retry_budget_exhausted'):
        control.retry_after_code_fix(AID, REVISION, **kwargs)
    assert (folder / 'job.json').read_bytes() == before


def test_a_first_retry_cannot_use_the_second_retry_path(tmp_path, monkeypatch):
    control, folder, snapshot, row, kwargs = fixture(tmp_path, monkeypatch)
    with pytest.raises(ValueError, match='retry_first_use_retry_existing'):
        control.retry_after_code_fix(AID, REVISION, retry_id='x-retry', expected_snapshot_sha256=kwargs['expected_snapshot_sha256'],
                                     expected_implementation_sha256=IMPLEMENTATION)


@pytest.mark.parametrize('mismatch', ['snapshot', 'implementation', 'policy', 'latest', 'living_owner'])
def test_second_retry_rejects_binding_drift_and_a_living_owner(tmp_path, monkeypatch, mismatch):
    control, folder, snapshot, row, kwargs = _exhausted(tmp_path, monkeypatch)
    if mismatch == 'snapshot':
        kwargs['expected_snapshot_sha256'] = 'b' * 64
    elif mismatch == 'implementation':
        kwargs['expected_implementation_sha256'] = 'b' * 64
    elif mismatch == 'policy':
        row['requested_model_policy'] = {**deepcopy(asis_runtime.policy()), 'fallback_allowed': True}
        asis_view.atomic(folder / 'job.json', row)
    elif mismatch == 'latest':
        asis_view.atomic(control.folder(AID) / 'state.json', {'latest_revision_id': 'another-revision'})
    else:
        row['process_identity']='living-owner'
        asis_view.atomic(folder/'job.json',row)
        monkeypatch.setattr(asis_worker, 'process_identity', lambda pid: 'living-owner')
    before = (folder / 'job.json').read_bytes()
    with pytest.raises(ValueError):
        control.retry_after_code_fix(AID, REVISION, **kwargs)
    assert (folder / 'job.json').read_bytes() == before


def test_a_third_code_fix_retry_is_allowed_once_more_only_when_the_code_changed_again(tmp_path, monkeypatch):
    """운영 2026-10-18: 두 번째 재대기 뒤 다른 원인(입력 길이)으로 다시 실패했다. 코드가 또 바뀌었다면 영석님 지시(같은 개정 이어가기)에 따라 한 번 더 허용한다."""
    control, folder, snapshot, row, kwargs = _exhausted(tmp_path, monkeypatch)
    row.update(retry_count=2, retry_id='second-retry', retry_implementation_sha256='d' * 64)
    asis_view.atomic(folder / 'job.json', row)
    result = control.retry_after_code_fix(AID, REVISION, **kwargs)
    assert result['retry_count'] == 3 and result['status'] == 'queued' and result['retry_implementation_sha256'] == IMPLEMENTATION
    assert (folder / 'retry-history' / kwargs['retry_id'] / 'job-before.json').is_file()
    assert control.retry_after_code_fix(AID, REVISION, **kwargs) == result


def test_a_third_code_fix_retry_is_refused_when_the_code_did_not_change(tmp_path, monkeypatch):
    control, folder, snapshot, row, kwargs = _exhausted(tmp_path, monkeypatch)
    row.update(retry_count=2, retry_id='second-retry', retry_implementation_sha256=IMPLEMENTATION)
    asis_view.atomic(folder / 'job.json', row)
    before = (folder / 'job.json').read_bytes()
    with pytest.raises(ValueError, match='retry_code_not_changed'):
        control.retry_after_code_fix(AID, REVISION, **kwargs)
    assert (folder / 'job.json').read_bytes() == before


def test_a_fourth_code_fix_retry_is_allowed_once_more_only_when_the_code_changed_again(tmp_path, monkeypatch):
    """운영 2026-10-18: 세 번째 재대기 뒤에도 새 원인(전송 전 한도 검사·검토 입력 길이)으로 실패했다. 영석님 지시(같은 개정 이어가기)에 따라 코드가 또 바뀐 경우에만 한 번 더 허용한다. 이후는 거부한다."""
    control, folder, snapshot, row, kwargs = _exhausted(tmp_path, monkeypatch)
    row.update(retry_count=3, retry_id='third-retry', retry_implementation_sha256='e' * 64)
    asis_view.atomic(folder / 'job.json', row)
    result = control.retry_after_code_fix(AID, REVISION, **kwargs)
    assert result['retry_count'] == 4 and result['status'] == 'queued' and result['retry_implementation_sha256'] == IMPLEMENTATION
    assert (folder / 'retry-history' / kwargs['retry_id'] / 'job-before.json').is_file()
    assert control.retry_after_code_fix(AID, REVISION, **kwargs) == result


def test_a_fourth_code_fix_retry_is_refused_when_the_code_did_not_change(tmp_path, monkeypatch):
    control, folder, snapshot, row, kwargs = _exhausted(tmp_path, monkeypatch)
    row.update(retry_count=3, retry_id='third-retry', retry_implementation_sha256=IMPLEMENTATION)
    asis_view.atomic(folder / 'job.json', row)
    before = (folder / 'job.json').read_bytes()
    with pytest.raises(ValueError, match='retry_code_not_changed'):
        control.retry_after_code_fix(AID, REVISION, **kwargs)
    assert (folder / 'job.json').read_bytes() == before


def test_a_fifth_code_fix_retry_is_allowed_once_more_only_when_the_code_changed_again(tmp_path, monkeypatch):
    """운영 2026-10-18: 네 번째 재대기 뒤 새 원인(입력이 한도를 넘는 큰 과제)으로 실패했다. 영석님 지시(과제는 권장 20개·최대 30개, 같은 개정으로 이어서)에 따라
    코드가 또 바뀐 경우에만 한 번 더 허용한다. 이후는 거부한다."""
    control, folder, snapshot, row, kwargs = _exhausted(tmp_path, monkeypatch)
    row.update(retry_count=4, retry_id='fourth-retry', retry_implementation_sha256='f' * 64)
    asis_view.atomic(folder / 'job.json', row)
    result = control.retry_after_code_fix(AID, REVISION, **kwargs)
    assert result['retry_count'] == 5 and result['status'] == 'queued' and result['retry_implementation_sha256'] == IMPLEMENTATION
    assert (folder / 'retry-history' / kwargs['retry_id'] / 'job-before.json').is_file()
    assert control.retry_after_code_fix(AID, REVISION, **kwargs) == result


def test_a_fifth_code_fix_retry_is_refused_when_the_code_did_not_change(tmp_path, monkeypatch):
    control, folder, snapshot, row, kwargs = _exhausted(tmp_path, monkeypatch)
    row.update(retry_count=4, retry_id='fourth-retry', retry_implementation_sha256=IMPLEMENTATION)
    asis_view.atomic(folder / 'job.json', row)
    before = (folder / 'job.json').read_bytes()
    with pytest.raises(ValueError, match='retry_code_not_changed'):
        control.retry_after_code_fix(AID, REVISION, **kwargs)
    assert (folder / 'job.json').read_bytes() == before


def test_a_sixth_code_fix_retry_is_allowed_once_more_only_when_the_code_changed_again(tmp_path, monkeypatch):
    """운영 2026-10-18: 다섯 번째 재대기 뒤 분할한 뒷 절반(A_ITEM_0020)이 요약 2단계로도 0.5만 토큰 넘쳐 실패했다. 영석님 지시(한 번 더 분할하고 상한 6)에 따라
    코드가 또 바뀐 경우에만 허용한다. 이후는 거부한다."""
    control, folder, snapshot, row, kwargs = _exhausted(tmp_path, monkeypatch)
    row.update(retry_count=5, retry_id='fifth-retry', retry_implementation_sha256='9' * 64)
    asis_view.atomic(folder / 'job.json', row)
    result = control.retry_after_code_fix(AID, REVISION, **kwargs)
    assert result['retry_count'] == 6 and result['status'] == 'queued' and result['retry_implementation_sha256'] == IMPLEMENTATION
    assert (folder / 'retry-history' / kwargs['retry_id'] / 'job-before.json').is_file()
    assert control.retry_after_code_fix(AID, REVISION, **kwargs) == result


def test_a_sixth_code_fix_retry_is_refused_when_the_code_did_not_change(tmp_path, monkeypatch):
    control, folder, snapshot, row, kwargs = _exhausted(tmp_path, monkeypatch)
    row.update(retry_count=5, retry_id='fifth-retry', retry_implementation_sha256=IMPLEMENTATION)
    asis_view.atomic(folder / 'job.json', row)
    before = (folder / 'job.json').read_bytes()
    with pytest.raises(ValueError, match='retry_code_not_changed'):
        control.retry_after_code_fix(AID, REVISION, **kwargs)
    assert (folder / 'job.json').read_bytes() == before


def test_a_seventh_code_fix_retry_is_allowed_once_more_only_when_the_code_changed_again(tmp_path, monkeypatch):
    """운영 2026-10-18: 여섯 번째 재대기 뒤 10단계가 끝나고 11단계 요약 입력이 34.3만 토큰으로 한도를 넘어 실패했다. 영석님 지시(C안: 11단계 수정 후 재개)에 따라
    코드가 또 바뀐 경우에만 허용한다. 이후는 거부한다."""
    control, folder, snapshot, row, kwargs = _exhausted(tmp_path, monkeypatch)
    row.update(retry_count=6, retry_id='sixth-retry', retry_implementation_sha256='8' * 64)
    asis_view.atomic(folder / 'job.json', row)
    result = control.retry_after_code_fix(AID, REVISION, **kwargs)
    assert result['retry_count'] == 7 and result['status'] == 'queued' and result['retry_implementation_sha256'] == IMPLEMENTATION
    assert (folder / 'retry-history' / kwargs['retry_id'] / 'job-before.json').is_file()
    assert control.retry_after_code_fix(AID, REVISION, **kwargs) == result


def test_a_seventh_code_fix_retry_is_refused_when_the_code_did_not_change(tmp_path, monkeypatch):
    control, folder, snapshot, row, kwargs = _exhausted(tmp_path, monkeypatch)
    row.update(retry_count=6, retry_id='sixth-retry', retry_implementation_sha256=IMPLEMENTATION)
    asis_view.atomic(folder / 'job.json', row)
    before = (folder / 'job.json').read_bytes()
    with pytest.raises(ValueError, match='retry_code_not_changed'):
        control.retry_after_code_fix(AID, REVISION, **kwargs)
    assert (folder / 'job.json').read_bytes() == before


def test_an_eighth_code_fix_retry_is_allowed_once_more_only_when_the_code_changed_again(tmp_path, monkeypatch):
    """운영 2026-10-18: 일곱 번째 재대기에서 11단계 요약 작성은 통과했지만 요약 검토가 34.9만 토큰으로 거절됐다. 영석님 지시(A안: 검토에도 같은 규칙, 상한 8)에 따라
    코드가 또 바뀐 경우에만 허용한다. 이후는 거부한다."""
    control, folder, snapshot, row, kwargs = _exhausted(tmp_path, monkeypatch)
    row.update(retry_count=7, retry_id='seventh-retry', retry_implementation_sha256='9' * 64)
    asis_view.atomic(folder / 'job.json', row)
    result = control.retry_after_code_fix(AID, REVISION, **kwargs)
    assert result['retry_count'] == 8 and result['status'] == 'queued' and result['retry_implementation_sha256'] == IMPLEMENTATION
    assert (folder / 'retry-history' / kwargs['retry_id'] / 'job-before.json').is_file()
    assert control.retry_after_code_fix(AID, REVISION, **kwargs) == result


def test_an_eighth_code_fix_retry_is_refused_when_the_code_did_not_change(tmp_path, monkeypatch):
    control, folder, snapshot, row, kwargs = _exhausted(tmp_path, monkeypatch)
    row.update(retry_count=7, retry_id='seventh-retry', retry_implementation_sha256=IMPLEMENTATION)
    asis_view.atomic(folder / 'job.json', row)
    before = (folder / 'job.json').read_bytes()
    with pytest.raises(ValueError, match='retry_code_not_changed'):
        control.retry_after_code_fix(AID, REVISION, **kwargs)
    assert (folder / 'job.json').read_bytes() == before


def test_a_ninth_code_fix_retry_is_allowed_once_more_only_when_the_code_changed_again(tmp_path, monkeypatch):
    """운영 2026-10-18: 여덟 번째 재대기에서 11단계 요약 검토가 입력 한도 없이 blocked + defects 로 즉시 실패했다(조정자 분기가 defects 가 있으면 건너뜀).
    영석님 지시(A안: blocked + 구체적 defects 를 재작성 경로로, 상한 9)에 따라 코드가 또 바뀐 경우에만 허용한다. 이후는 거부한다."""
    control, folder, snapshot, row, kwargs = _exhausted(tmp_path, monkeypatch)
    row.update(retry_count=8, retry_id='eighth-retry', retry_implementation_sha256='b' * 64)
    asis_view.atomic(folder / 'job.json', row)
    result = control.retry_after_code_fix(AID, REVISION, **kwargs)
    assert result['retry_count'] == 9 and result['status'] == 'queued' and result['retry_implementation_sha256'] == IMPLEMENTATION
    assert (folder / 'retry-history' / kwargs['retry_id'] / 'job-before.json').is_file()
    assert control.retry_after_code_fix(AID, REVISION, **kwargs) == result


def test_a_ninth_code_fix_retry_is_refused_when_the_code_did_not_change(tmp_path, monkeypatch):
    control, folder, snapshot, row, kwargs = _exhausted(tmp_path, monkeypatch)
    row.update(retry_count=8, retry_id='eighth-retry', retry_implementation_sha256=IMPLEMENTATION)
    asis_view.atomic(folder / 'job.json', row)
    before = (folder / 'job.json').read_bytes()
    with pytest.raises(ValueError, match='retry_code_not_changed'):
        control.retry_after_code_fix(AID, REVISION, **kwargs)
    assert (folder / 'job.json').read_bytes() == before


def test_a_tenth_code_fix_retry_is_allowed_once_more_only_when_the_code_changed_again(tmp_path, monkeypatch):
    """운영 2026-10-19: 아홉 번째 재대기에서 12단계 문서 검토의 종합·과제·통합 3개 단위가 모델 토큰 한도를 넘었다(글자 수 한도만 있고 토큰 한도 없음).
    영석님 지시(최선의 선택으로 자동 선택, 내일 아침까지 PPT 완성)에 따라 코드가 또 바뀐 경우에만 허용한다. 이후는 거부한다."""
    control, folder, snapshot, row, kwargs = _exhausted(tmp_path, monkeypatch)
    row.update(retry_count=9, retry_id='ninth-retry', retry_implementation_sha256='b' * 64)
    asis_view.atomic(folder / 'job.json', row)
    result = control.retry_after_code_fix(AID, REVISION, **kwargs)
    assert result['retry_count'] == 10 and result['status'] == 'queued' and result['retry_implementation_sha256'] == IMPLEMENTATION
    assert (folder / 'retry-history' / kwargs['retry_id'] / 'job-before.json').is_file()
    assert control.retry_after_code_fix(AID, REVISION, **kwargs) == result


def test_a_tenth_code_fix_retry_is_refused_when_the_code_did_not_change(tmp_path, monkeypatch):
    control, folder, snapshot, row, kwargs = _exhausted(tmp_path, monkeypatch)
    row.update(retry_count=9, retry_id='ninth-retry', retry_implementation_sha256=IMPLEMENTATION)
    asis_view.atomic(folder / 'job.json', row)
    before = (folder / 'job.json').read_bytes()
    with pytest.raises(ValueError, match='retry_code_not_changed'):
        control.retry_after_code_fix(AID, REVISION, **kwargs)
    assert (folder / 'job.json').read_bytes() == before


def test_an_eleventh_code_fix_retry_is_allowed_once_more_only_when_the_code_changed_again(tmp_path, monkeypatch):
    """운영 2026-10-19: 열 번째 재대기에서 12단계 문서 검토 14개 호출이 모두 197만~257만 자였다. stage_data 의 승인 블록 3개(195만 자)가 draft 와 같은 본문을 중복해 실렸다(내 측정은 이 블록이 빠진 환경이라 틀렸다).
    영석님 지시(진행해, 내일 아침까지 PPT 완성)에 따라 코드가 또 바뀐 경우에만 허용한다. 이후는 거부한다."""
    control, folder, snapshot, row, kwargs = _exhausted(tmp_path, monkeypatch)
    row.update(retry_count=10, retry_id='tenth-retry', retry_implementation_sha256='c' * 64)
    asis_view.atomic(folder / 'job.json', row)
    result = control.retry_after_code_fix(AID, REVISION, **kwargs)
    assert result['retry_count'] == 11 and result['status'] == 'queued' and result['retry_implementation_sha256'] == IMPLEMENTATION
    assert (folder / 'retry-history' / kwargs['retry_id'] / 'job-before.json').is_file()
    assert control.retry_after_code_fix(AID, REVISION, **kwargs) == result


def test_an_eleventh_code_fix_retry_is_refused_when_the_code_did_not_change(tmp_path, monkeypatch):
    control, folder, snapshot, row, kwargs = _exhausted(tmp_path, monkeypatch)
    row.update(retry_count=10, retry_id='tenth-retry', retry_implementation_sha256=IMPLEMENTATION)
    asis_view.atomic(folder / 'job.json', row)
    before = (folder / 'job.json').read_bytes()
    with pytest.raises(ValueError, match='retry_code_not_changed'):
        control.retry_after_code_fix(AID, REVISION, **kwargs)
    assert (folder / 'job.json').read_bytes() == before


def test_a_twelfth_code_fix_retry_is_allowed_once_more_only_when_the_code_changed_again(tmp_path, monkeypatch):
    """운영 2026-10-19: 열한 번째 재대기에서 12단계 문서 검토 17개가 모델에 도달했으나 영역 검토 9개가 blocked 였다(큰 사실 값이 not_inlined 로 전송돼 검토자가 대조할 수 없었다).
    영석님 지시(진행해, 내일 아침까지 PPT 완성)에 따라 코드가 또 바뀐 경우에만 허용한다. 이후는 거부한다."""
    control, folder, snapshot, row, kwargs = _exhausted(tmp_path, monkeypatch)
    row.update(retry_count=11, retry_id='eleventh-retry', retry_implementation_sha256='d' * 64)
    asis_view.atomic(folder / 'job.json', row)
    result = control.retry_after_code_fix(AID, REVISION, **kwargs)
    assert result['retry_count'] == 12 and result['status'] == 'queued' and result['retry_implementation_sha256'] == IMPLEMENTATION
    assert (folder / 'retry-history' / kwargs['retry_id'] / 'job-before.json').is_file()
    assert control.retry_after_code_fix(AID, REVISION, **kwargs) == result


def test_a_twelfth_code_fix_retry_is_refused_when_the_code_did_not_change(tmp_path, monkeypatch):
    control, folder, snapshot, row, kwargs = _exhausted(tmp_path, monkeypatch)
    row.update(retry_count=11, retry_id='eleventh-retry', retry_implementation_sha256=IMPLEMENTATION)
    asis_view.atomic(folder / 'job.json', row)
    before = (folder / 'job.json').read_bytes()
    with pytest.raises(ValueError, match='retry_code_not_changed'):
        control.retry_after_code_fix(AID, REVISION, **kwargs)
    assert (folder / 'job.json').read_bytes() == before


def test_a_thirteenth_code_fix_retry_is_allowed_once_more_only_when_the_code_changed_again(tmp_path, monkeypatch):
    """운영 2026-10-19: 열두 번째 재대기에서 영역 검토 4개가 처음 통과했으나 종합·과제 구간 5개가 입력 한도(92.8만 > 90만)로 실패했다(값 전송을 구간까지 넓힌 내 수정의 결함).
    영석님 지시(상한 올려서 말한 방안 적용해서 진행)에 따라 코드가 또 바뀐 경우에만 허용한다. 이후는 거부한다."""
    control, folder, snapshot, row, kwargs = _exhausted(tmp_path, monkeypatch)
    row.update(retry_count=12, retry_id='twelfth-retry', retry_implementation_sha256='e' * 64)
    asis_view.atomic(folder / 'job.json', row)
    result = control.retry_after_code_fix(AID, REVISION, **kwargs)
    assert result['retry_count'] == 13 and result['status'] == 'queued' and result['retry_implementation_sha256'] == IMPLEMENTATION
    assert (folder / 'retry-history' / kwargs['retry_id'] / 'job-before.json').is_file()
    assert control.retry_after_code_fix(AID, REVISION, **kwargs) == result


def test_a_thirteenth_code_fix_retry_is_refused_when_the_code_did_not_change(tmp_path, monkeypatch):
    control, folder, snapshot, row, kwargs = _exhausted(tmp_path, monkeypatch)
    row.update(retry_count=12, retry_id='twelfth-retry', retry_implementation_sha256=IMPLEMENTATION)
    asis_view.atomic(folder / 'job.json', row)
    before = (folder / 'job.json').read_bytes()
    with pytest.raises(ValueError, match='retry_code_not_changed'):
        control.retry_after_code_fix(AID, REVISION, **kwargs)
    assert (folder / 'job.json').read_bytes() == before


@pytest.mark.parametrize('retry_count', [13, 14])
@pytest.mark.parametrize('code_changed', [False, True])
def test_operating_retry_limit_is_preserved_even_when_deploying_code_changes(tmp_path, monkeypatch, retry_count, code_changed):
    """운영의 13회 제한을 보존한다. 공통 기능 배포는 제한 상향이나 고객 개정 재개 승인이 아니다."""
    control, folder, snapshot, row, kwargs = _exhausted(tmp_path, monkeypatch)
    row.update(retry_count=retry_count, retry_id='last-approved-retry',
               retry_implementation_sha256='f' * 64 if code_changed else IMPLEMENTATION)
    asis_view.atomic(folder / 'job.json', row)
    before = (folder / 'job.json').read_bytes()
    with pytest.raises(ValueError, match='retry_budget_exhausted'):
        control.retry_after_code_fix(AID, REVISION, **kwargs)
    assert (folder / 'job.json').read_bytes() == before
    assert (folder / 'snapshot.json').read_bytes() == snapshot
    assert not (folder / 'retry-history' / kwargs['retry_id']).exists()
