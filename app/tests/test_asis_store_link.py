"""개정 폴더의 정본(snapshot.json, analysis.json)을 근거·분석 저장소로 적재하는 연결부 시험.

계약: docs/EVIDENCE-STORE-DESIGN-V2.md 14.5(적재·감사 지점에 연결), 정본 JSON은 바뀌지 않는다.
- 적재는 파생물이다. 실패해도 개정 생성·정본 파일에는 영향이 없어야 하고, 실패는 기록으로 남는다.
- 같은 정본이면 같은 DB(바이트 동일), 정본이 바뀌면 다른 DB다. 기존 DB는 덮어쓰지 않는다.
"""
import hashlib
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import asis_store_link as link
import asis_evidence_store as estore
import asis_analysis_store as astore
from test_asis_evidence_store import sample
from test_asis_analysis_store import stage_data, RUN


def snapshot(tmp_path):
    s = sample()
    return {'facts': s['facts'], 'criteria': s['criteria'], 'run': s['run'],
            'context': {'collection_basis': s['basis'], 'criterion_assessments': s['assessments'], 'dataset_register': []},
            'stage_data': {}}


def folder_with_snapshot(tmp_path):
    folder = tmp_path / 'rev'
    folder.mkdir()
    (folder / 'snapshot.json').write_text(json.dumps(snapshot(tmp_path), ensure_ascii=False), encoding='utf-8')
    return folder


def test_snapshot_is_loaded_into_an_evidence_db_inside_the_revision_folder(tmp_path):
    folder = folder_with_snapshot(tmp_path)
    out = link.load_evidence(folder)
    assert out['ok'] is True and (folder / 'store' / 'evidence.db').is_file()
    h = estore.open_project(folder / 'store' / 'evidence.db')
    assert h.fact('F_T_foundryAccountConfiguration')['record_count'] == 2


def test_the_canonical_snapshot_is_not_modified(tmp_path):
    folder = folder_with_snapshot(tmp_path)
    before = hashlib.sha256((folder / 'snapshot.json').read_bytes()).hexdigest()
    link.load_evidence(folder)
    assert hashlib.sha256((folder / 'snapshot.json').read_bytes()).hexdigest() == before


def test_loading_twice_reuses_the_same_db_without_rewriting_it(tmp_path):
    folder = folder_with_snapshot(tmp_path)
    first = link.load_evidence(folder)
    mtime = (folder / 'store' / 'evidence.db').stat().st_mtime_ns
    second = link.load_evidence(folder)
    assert second['ok'] is True and second['reused'] is True and first['sha256'] == second['sha256']
    assert (folder / 'store' / 'evidence.db').stat().st_mtime_ns == mtime


def test_a_changed_snapshot_is_refused_instead_of_silently_reusing_a_stale_db(tmp_path):
    folder = folder_with_snapshot(tmp_path)
    link.load_evidence(folder)
    snap = json.loads((folder / 'snapshot.json').read_text(encoding='utf-8'))
    snap['facts'][0]['value'] = [{'id': 'changed'}]
    (folder / 'snapshot.json').write_text(json.dumps(snap, ensure_ascii=False), encoding='utf-8')
    out = link.load_evidence(folder)
    assert out['ok'] is False and out['error'] == 'snapshot_changed_since_load'


def test_a_load_failure_is_reported_not_raised_and_leaves_no_partial_db(tmp_path):
    folder = folder_with_snapshot(tmp_path)
    snap = json.loads((folder / 'snapshot.json').read_text(encoding='utf-8'))
    snap['context']['collection_basis'] = {'datasets': 'broken'}
    (folder / 'snapshot.json').write_text(json.dumps(snap, ensure_ascii=False), encoding='utf-8')
    out = link.load_evidence(folder)
    assert out['ok'] is False and out['error']
    assert not (folder / 'store' / 'evidence.db').exists()
    assert not list((folder / 'store').glob('*.loading')) if (folder / 'store').exists() else True


def test_a_missing_snapshot_is_reported(tmp_path):
    folder = tmp_path / 'empty'
    folder.mkdir()
    out = link.load_evidence(folder)
    assert out['ok'] is False and out['error'] == 'snapshot_missing'


def test_analysis_result_is_loaded_into_an_analysis_db_and_round_trips(tmp_path):
    folder = folder_with_snapshot(tmp_path)
    result = {'input': {**snapshot(tmp_path), 'stage_data': stage_data()}, 'stages': []}
    (folder / 'analysis.json').write_text(json.dumps(result, ensure_ascii=False), encoding='utf-8')
    out = link.load_analysis(folder, run=RUN)
    assert out['ok'] is True
    h = astore.open_analysis(folder / 'store' / 'analysis.db')
    assert h.export_stage('approved_directions') == stage_data()['approved_directions']
    assert out['validate']['ok'] is True


def test_analysis_load_reports_dangling_references_without_failing_the_load(tmp_path):
    folder = folder_with_snapshot(tmp_path)
    data = stage_data()
    data['approved_actions']['actions'][0]['direction_refs'] = ['D_MISSING']
    (folder / 'analysis.json').write_text(json.dumps({'input': {**snapshot(tmp_path), 'stage_data': data}}, ensure_ascii=False), encoding='utf-8')
    out = link.load_analysis(folder, run=RUN)
    assert out['ok'] is True and out['validate']['ok'] is False and out['validate']['dangling'][0]['ref'] == 'D_MISSING'


def test_citation_audit_finds_facts_cited_by_analysis_but_absent_from_evidence(tmp_path):
    folder = folder_with_snapshot(tmp_path)
    data = stage_data()
    data['approved_areas']['areas'][0]['observations'][0]['fact_refs'].append('F_T_notInEvidence')
    (folder / 'analysis.json').write_text(json.dumps({'input': {**snapshot(tmp_path), 'stage_data': data}}, ensure_ascii=False), encoding='utf-8')
    link.load_evidence(folder)
    link.load_analysis(folder, run=RUN)
    audit = link.audit_citations(folder)
    assert 'F_T_notInEvidence' in audit['missing_in_evidence']
    assert audit['ok'] is False


def test_citation_audit_passes_when_every_cited_fact_exists(tmp_path):
    folder = folder_with_snapshot(tmp_path)
    data = stage_data()
    # 표본 사실로 인용을 모두 맞춘다
    for a in data['approved_areas']['areas']:
        for o in a['observations']:
            o['fact_refs'] = ['F_T_foundryAccountConfiguration']
    data['approved_directions']['directions'][0]['fact_refs'] = ['F_T_foundryAccountConfiguration']
    data['approved_directions']['edges'] = []
    data['approved_actions']['diagnoses'][0]['fact_refs'] = ['F_T_foundryAccountConfiguration']
    data['approved_actions']['actions'][0]['fact_refs'] = ['F_T_foundryAccountConfiguration']
    (folder / 'analysis.json').write_text(json.dumps({'input': {**snapshot(tmp_path), 'stage_data': data}}, ensure_ascii=False), encoding='utf-8')
    link.load_evidence(folder)
    link.load_analysis(folder, run=RUN)
    assert link.audit_citations(folder)['ok'] is True


def test_a_failed_load_removes_a_half_written_db_and_a_retry_can_succeed(tmp_path, monkeypatch):
    """적재 도중 실패하면 반쯤 쓴 파일을 남기지 않는다. 남기면 다음 호출이 '정본이 바뀌었다'고 잘못 거절한다."""
    folder = folder_with_snapshot(tmp_path)
    real = estore.load

    def half_written(path, **kw):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        Path(path).write_bytes(b'partial')
        raise RuntimeError('disk_full')
    monkeypatch.setattr(estore, 'load', half_written)
    first = link.load_evidence(folder)
    assert first['ok'] is False and 'disk_full' in first['error']
    assert not (folder / 'store' / 'evidence.db').exists()
    monkeypatch.setattr(estore, 'load', real)
    second = link.load_evidence(folder)
    assert second['ok'] is True and second['reused'] is False


def test_a_failed_analysis_load_also_leaves_no_partial_db(tmp_path, monkeypatch):
    folder = folder_with_snapshot(tmp_path)
    (folder / 'analysis.json').write_text(json.dumps({'input': {**snapshot(tmp_path), 'stage_data': stage_data()}}, ensure_ascii=False), encoding='utf-8')

    def half_written(path, data, run):
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        Path(path).write_bytes(b'partial')
        raise RuntimeError('disk_full')
    monkeypatch.setattr(astore, 'load', half_written)
    assert link.load_analysis(folder)['ok'] is False
    assert not (folder / 'store' / 'analysis.db').exists()
