"""분리 실행 워커가 부모(SSH 세션·서비스 작업 개체)와 함께 죽지 않는 계약 시험.

Windows 에서 SSH 세션·서비스 안의 프로세스는 Job 개체에 묶여 있어, DETACHED_PROCESS 만으로 띄운 워커는 부모 세션이 끝나면 함께 종료된다.
2026-10-17 프로젝트 25 개정 r-20261017T003438 이 이 때문에 시작 1초 만에 사라졌다(worker.log 0바이트, 하트비트 1회).
launch 는 Windows 에서 작업 개체 이탈(CREATE_BREAKAWAY_FROM_JOB)을 요청해야 하고, 이탈이 거부되는 환경에서도 launch 가 실패하지 않아야 한다.
플랫폼 분기는 spawn_worker 로 분리해 Windows 가 아닌 개발 환경에서도 시험한다(잠금 등 다른 Windows 전용 경로는 시험 대상이 아니다).
"""
from pathlib import Path
import subprocess
import sys

APP = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP))
import asis_view

BREAKAWAY = 0x01000000
DETACHED = 0x00000008
NEW_GROUP = 0x00000200


class FakeProc:
    pid = 4242


def patch_flags(monkeypatch):
    monkeypatch.setattr(subprocess, 'DETACHED_PROCESS', DETACHED, raising=False)
    monkeypatch.setattr(subprocess, 'CREATE_NEW_PROCESS_GROUP', NEW_GROUP, raising=False)


def test_windows_spawn_requests_breakaway_from_job(monkeypatch):
    patch_flags(monkeypatch)
    calls = []
    monkeypatch.setattr(asis_view.subprocess, 'Popen', lambda command, **kw: calls.append(kw) or FakeProc())
    proc, breakaway = asis_view.spawn_worker(['x'], log=None, cwd='.', env={}, windows=True)
    flags = calls[0]['creationflags']
    assert flags & DETACHED and flags & NEW_GROUP
    assert flags & BREAKAWAY, 'SSH 세션·서비스 작업 개체에서 이탈해야 부모 종료 때 워커가 죽지 않는다'
    assert breakaway is True and proc.pid == FakeProc.pid


def test_windows_spawn_retries_without_breakaway_when_job_forbids_it(monkeypatch):
    patch_flags(monkeypatch)
    calls = []

    def fake_popen(command, **kw):
        calls.append(kw['creationflags'])
        if kw['creationflags'] & BREAKAWAY:
            raise PermissionError(5, 'Access is denied')  # 작업 개체가 이탈을 허용하지 않는 환경
        return FakeProc()

    monkeypatch.setattr(asis_view.subprocess, 'Popen', fake_popen)
    proc, breakaway = asis_view.spawn_worker(['x'], log=None, cwd='.', env={}, windows=True)
    assert len(calls) == 2 and calls[0] & BREAKAWAY and not (calls[1] & BREAKAWAY)
    assert calls[1] & DETACHED and calls[1] & NEW_GROUP
    assert breakaway is False, '이탈 없이 실행했다는 사실을 호출자가 job.json 에 남길 수 있어야 한다'


def test_windows_spawn_does_not_swallow_other_errors(monkeypatch):
    """인터프리터 부재 같은 다른 오류를 이탈 거부로 오인해 이탈 없이 다시 시도하면, 같은 실패를 숨기거나 부모 종료에 취약한 워커를 만든다.
    첫 호출만 실패하게 하고 두 번째 호출이 성공하도록 해서, 삼키는 구현은 예외 없이 통과하므로 이 시험이 잡는다."""
    patch_flags(monkeypatch)
    calls = []

    def fake_popen(command, **kw):
        calls.append(kw['creationflags'])
        if len(calls) == 1:
            raise FileNotFoundError(2, 'interpreter missing')
        return FakeProc()

    monkeypatch.setattr(asis_view.subprocess, 'Popen', fake_popen)
    try:
        asis_view.spawn_worker(['x'], log=None, cwd='.', env={}, windows=True)
    except FileNotFoundError:
        assert len(calls) == 1, '다른 오류에서는 다시 시도하지 않는다'
        return
    raise AssertionError('인터프리터 부재 오류가 이탈 거부로 오인되어 삼켜졌다')


def test_non_windows_spawn_is_unchanged(monkeypatch):
    calls = []
    monkeypatch.setattr(asis_view.subprocess, 'Popen', lambda command, **kw: calls.append(kw) or FakeProc())
    proc, breakaway = asis_view.spawn_worker(['x'], log=None, cwd='.', env={}, windows=False)
    assert calls[0].get('start_new_session') is True and 'creationflags' not in calls[0]
    assert breakaway is None
