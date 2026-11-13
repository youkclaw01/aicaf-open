"""워커(asis_view.Controller.generate)의 저장소 연결 시험.

계약(docs/EVIDENCE-STORE-DESIGN-V2.md 14.5): 저장소는 정본에서 만든 파생물이다.
  - 개정 생성이 성공하면 <개정>/store/ 에 evidence.db와 analysis.db가 생기고 job.json의 store 항목에 요약이 남는다.
  - 저장소 적재·감사가 어떤 이유로 실패해도 개정 상태는 바뀌지 않는다(generated_candidate 유지). 실패는 job.json의 store 항목에 기록된다.
  - snapshot.json·analysis.json 정본 바이트는 바뀌지 않는다.
"""
import json
import hashlib
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import asis_view
import asis_worker
import asis_store_link as link
from test_asis_evidence_store import sample
from test_asis_analysis_store import stage_data

AID = 999997
REVISION = 'r-20000101T000000-0000000000000002'


def prepare(control):
    folder = control.job_dir(AID, REVISION)
    folder.mkdir(parents=True)
    s = sample()
    run = dict(s['run'], assessment_id=AID, analysis_revision=REVISION)
    snap = {'facts': s['facts'], 'criteria': s['criteria'], 'run': run,
            'context': {'collection_basis': s['basis'], 'criterion_assessments': s['assessments'], 'dataset_register': []}, 'stage_data': {}}
    raw = asis_view.asis_model.encoded(snap)
    (folder / 'snapshot.json').write_bytes(raw)
    asis_view.atomic(folder / 'revision-context.json', {})
    asis_view.atomic(folder / 'job.json', {'run': run, 'revision_id': REVISION, 'status': 'queued', 'pid': 1001,
        'process_identity': 'launcher', 'snapshot_sha256': hashlib.sha256(raw).hexdigest(),
        'steps': [{'name': 'analysis', 'status': 'pending'}]})
    return folder, run


def controller(tmp_path, monkeypatch, analyzer):
    control = asis_view.Controller(ROOT=tmp_path, assessment=None, infra_case_dir=None, db=None, test_analyzer=analyzer)
    monkeypatch.setattr(asis_view.os, 'getpid', lambda: 1002)
    monkeypatch.setattr(asis_view.os, 'getppid', lambda: 1001)
    monkeypatch.setattr(asis_worker, 'process_identity', lambda pid: {1001: 'launcher', 1002: 'worker'}.get(pid))
    return control


def make_result(run):
    approved = stage_data()
    for value in approved.values():
        value['run'] = run
    snap = {'run': run, 'stage_data': approved}
    return {'input': snap, 'model_call_performed': False, 'requirements_application': {}, 'run': run}


def finish_ok(control):
    def build(result, directory, **kw):
        manifest = {'artifacts': [], 'quality_status': 'x', 'run': result['run']}
        asis_view.atomic(Path(directory) / 'artifact-manifest.json', manifest)
        return manifest
    control.builder = build


def test_a_generated_revision_gets_both_stores_and_a_summary_in_job_json(tmp_path, monkeypatch):
    c = controller(tmp_path, monkeypatch, None)
    folder, run = prepare(c)
    c.test_analyzer = lambda data, **kw: make_result(run)
    finish_ok(c)
    row = c.generate(AID, REVISION)
    assert row['status'] == 'generated_candidate'
    assert (folder / 'store' / 'evidence.db').is_file() and (folder / 'store' / 'analysis.db').is_file()
    store = asis_view.read(folder / 'job.json')['store']
    assert store['evidence']['ok'] is True and store['analysis']['ok'] is True and store['analysis']['validate_ok'] is True
    assert 'audit' in store and isinstance(store['audit']['ok'], bool)


def test_canonical_files_are_unchanged_by_the_store_step(tmp_path, monkeypatch):
    c = controller(tmp_path, monkeypatch, None)
    folder, run = prepare(c)
    before = (folder / 'snapshot.json').read_bytes()
    c.test_analyzer = lambda data, **kw: make_result(run)
    finish_ok(c)
    c.generate(AID, REVISION)
    assert (folder / 'snapshot.json').read_bytes() == before


def test_a_store_failure_never_fails_the_revision(tmp_path, monkeypatch):
    c = controller(tmp_path, monkeypatch, None)
    folder, run = prepare(c)
    c.test_analyzer = lambda data, **kw: make_result(run)
    finish_ok(c)

    def boom(*a, **k):
        raise RuntimeError('store_exploded')
    monkeypatch.setattr(link, 'load_evidence', boom)
    monkeypatch.setattr(link, 'load_analysis', boom)
    row = c.generate(AID, REVISION)
    assert row['status'] == 'generated_candidate', 'a derived store must never fail the real work'
    store = asis_view.read(folder / 'job.json')['store']
    assert store['evidence']['ok'] is False and 'store_exploded' in store['evidence']['error']
    assert store['analysis']['ok'] is False


def test_the_store_step_runs_before_the_model_so_the_evidence_db_exists_during_analysis(tmp_path, monkeypatch):
    c = controller(tmp_path, monkeypatch, None)
    folder, run = prepare(c)
    seen = {}

    def analyzer(data, **kw):
        seen['evidence_during_analysis'] = (folder / 'store' / 'evidence.db').is_file()
        return make_result(run)
    c.test_analyzer = analyzer
    finish_ok(c)
    c.generate(AID, REVISION)
    assert seen['evidence_during_analysis'] is True


def test_a_failed_revision_still_keeps_the_evidence_db_for_diagnosis(tmp_path, monkeypatch):
    c = controller(tmp_path, monkeypatch, None)
    folder, run = prepare(c)

    def failing(data, **kw):
        raise RuntimeError('analysis_exploded')
    c.test_analyzer = failing
    row = c.generate(AID, REVISION)
    assert row['status'] == 'failed' and 'analysis_exploded' in row['error']
    assert (folder / 'store' / 'evidence.db').is_file() and not (folder / 'store' / 'analysis.db').exists()


def test_the_summary_is_small_and_has_no_paths_or_secrets(tmp_path, monkeypatch):
    c = controller(tmp_path, monkeypatch, None)
    folder, run = prepare(c)
    c.test_analyzer = lambda data, **kw: make_result(run)
    finish_ok(c)
    c.generate(AID, REVISION)
    store = asis_view.read(folder / 'job.json')['store']
    assert len(json.dumps(store)) < 2000
    assert str(tmp_path) not in json.dumps(store)


def test_a_very_long_store_error_is_truncated_in_the_summary(tmp_path, monkeypatch):
    """job.json은 화면·원장에 읽히는 파일이다. 저장소 오류 문장이 길어도 요약은 작게 유지한다."""
    c = controller(tmp_path, monkeypatch, None)
    folder, run = prepare(c)
    c.test_analyzer = lambda data, **kw: make_result(run)
    finish_ok(c)
    monkeypatch.setattr(link, 'load_evidence', lambda f: {'ok': False, 'error': 'E' * 5000})
    c.generate(AID, REVISION)
    store = asis_view.read(folder / 'job.json')['store']
    assert 0 < len(store['evidence']['error']) <= 160 and len(json.dumps(store)) < 2000


def test_an_exception_text_in_the_summary_is_truncated_too(tmp_path, monkeypatch):
    c = controller(tmp_path, monkeypatch, None)
    folder, run = prepare(c)
    c.test_analyzer = lambda data, **kw: make_result(run)
    finish_ok(c)

    def boom(*a, **k):
        raise RuntimeError('X' * 5000)
    monkeypatch.setattr(link, 'load_analysis', boom)
    c.generate(AID, REVISION)
    store = asis_view.read(folder / 'job.json')['store']
    assert len(store['analysis']['error']) <= 160
