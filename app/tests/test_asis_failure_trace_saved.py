"""워커가 실패로 끝나면 예외 스택이 개정 폴더에 남는다(비밀은 제거). 원인을 추적할 수 없던 중단(cross_run_previous_stage)의 재발 방지 시험이다.
실제 생성 흐름(Controller.generate)에서 분석기가 예외를 던지는 상황을 재현한다."""
from pathlib import Path
import hashlib, sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import asis_view, asis_worker
from test_asis_worker_claim import AID, REVISION, prepare


def failing_analyzer(data, **kwargs):
    # 예외 메시지에 토큰이 섞이는 최악의 경우도 함께 본다.
    raise ValueError('cross_run_previous_stage github_pat_ABCDEF123456789 Bearer secret.token.value')


def run(tmp_path, monkeypatch):
    control = asis_view.Controller(ROOT=tmp_path, assessment=None, infra_case_dir=None, db=None, test_analyzer=failing_analyzer)
    folder, _ = prepare(control)
    monkeypatch.setattr(asis_view.os, 'getpid', lambda: 1002)
    monkeypatch.setattr(asis_view.os, 'getppid', lambda: 1001)
    monkeypatch.setattr(asis_worker, 'process_identity', lambda pid: {1001: 'launcher-created-identity', 1002: 'worker-created-identity'}.get(pid))
    return control, folder, control.generate(AID, REVISION)


def test_a_failed_generation_saves_a_redacted_stack_trace_and_links_it_from_the_job(tmp_path, monkeypatch):
    control, folder, result = run(tmp_path, monkeypatch)
    assert result['status'] == 'failed' and result['error'].startswith('ValueError:cross_run_previous_stage')
    link = result['failure_trace']
    assert link.startswith('failure-traces/') and link.endswith('-ValueError.txt')
    trace = (folder / link).read_text(encoding='utf-8')
    assert 'Traceback (most recent call last)' in trace and 'failing_analyzer' in trace, 'the trace must show where the exception was raised'
    assert 'ABCDEF123456789' not in trace and 'secret.token.value' not in trace, 'secrets must be redacted from the saved trace'
    assert '[REDACTED]' in trace
    assert asis_view.read(folder / 'job.json')['failure_trace'] == link, 'the job row on disk must carry the link'


def test_failing_to_write_the_trace_never_masks_the_original_failure(tmp_path, monkeypatch):
    original = Path.write_text

    def refuse(self, *args, **kwargs):
        if 'failure-traces' in str(self):
            raise OSError(28, 'No space left on device')
        return original(self, *args, **kwargs)
    monkeypatch.setattr(Path, 'write_text', refuse)
    control, folder, result = run(tmp_path, monkeypatch)
    assert result['status'] == 'failed' and result['error'].startswith('ValueError:cross_run_previous_stage')
    assert 'failure_trace' not in result, 'no link may be recorded for a trace that was not saved'
