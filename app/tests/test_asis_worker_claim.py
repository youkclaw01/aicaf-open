"""Windows venv 실행기와 실제 worker의 PID가 달라도 승인된 작업만 인계받는지 검증한다."""
from pathlib import Path
import hashlib
import sys

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import asis_view
import asis_worker


AID = 999998
REVISION = 'r-20000101T000000-0000000000000001'


def prepare(control):
    folder = control.job_dir(AID, REVISION)
    folder.mkdir(parents=True)
    snapshot = asis_view.asis_model.encoded({'run': {'assessment_id': AID}, 'synthetic': True})
    (folder / 'snapshot.json').write_bytes(snapshot)
    asis_view.atomic(folder / 'revision-context.json', {})
    asis_view.atomic(folder / 'job.json', {
        'run': {'assessment_id': AID}, 'revision_id': REVISION, 'status': 'queued',
        'pid': 1001, 'process_identity': 'launcher-created-identity',
        'snapshot_sha256': hashlib.sha256(snapshot).hexdigest(), 'steps': [],
    })
    return folder, snapshot


def test_verified_launcher_child_claims_frozen_queued_revision(tmp_path, monkeypatch):
    observed = []
    control = asis_view.Controller(ROOT=tmp_path, assessment=None, infra_case_dir=None,
                                  db=None, test_analyzer=lambda data, **kwargs: data)
    folder, snapshot = prepare(control)
    monkeypatch.setattr(asis_view.os, 'getpid', lambda: 1002)
    monkeypatch.setattr(asis_view.os, 'getppid', lambda: 1001)
    monkeypatch.setattr(asis_worker, 'process_identity',
                        lambda pid: {1001: 'launcher-created-identity',
                                     1002: 'worker-created-identity'}.get(pid))

    def stop_before_artifacts(result, directory, **kwargs):
        row = asis_view.read(folder / 'job.json')
        assert isinstance(row, dict)
        observed.append(row['status'])
        raise RuntimeError('synthetic_worker_claim_stop_before_artifacts')

    monkeypatch.setattr(control, 'builder', stop_before_artifacts)
    result = control.generate(AID, REVISION)
    assert observed == ['running'], 'verified venv child must leave queued and enter analysis'
    assert result['status'] == 'failed'
    assert result['pid'] == 1002
    assert result['process_identity'] == 'worker-created-identity'
    assert result['error'] == 'RuntimeError:synthetic_worker_claim_stop_before_artifacts'
    assert (folder / 'snapshot.json').read_bytes() == snapshot


@pytest.mark.parametrize('parent,identity', [
    (2001, 'launcher-created-identity'),
    (1001, 'reused-pid-other-identity'),
    (1001, 'unverifiable'),
    (1001, None),
])
def test_unbound_process_cannot_claim_queued_revision(tmp_path, monkeypatch, parent, identity):
    control = asis_view.Controller(ROOT=tmp_path, assessment=None, infra_case_dir=None,
                                  db=None, test_analyzer=lambda data, **kwargs: data)
    folder, snapshot = prepare(control)
    original = (folder / 'job.json').read_bytes()
    monkeypatch.setattr(asis_view.os, 'getpid', lambda: 1002)
    monkeypatch.setattr(asis_view.os, 'getppid', lambda: parent)
    monkeypatch.setattr(asis_worker, 'process_identity', lambda pid: identity)
    result = control.generate(AID, REVISION)
    assert result['status'] == 'queued'
    assert (folder / 'job.json').read_bytes() == original
    assert (folder / 'snapshot.json').read_bytes() == snapshot
