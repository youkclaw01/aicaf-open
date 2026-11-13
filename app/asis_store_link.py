"""개정 폴더의 정본(snapshot.json, analysis.json)을 근거·분석 저장소로 적재하는 연결부.

설계: docs/EVIDENCE-STORE-DESIGN-V2.md 14.5. 저장소는 정본에서 만든 파생물이다.
  - 정본 파일은 읽기만 한다. 적재가 실패해도 예외를 던지지 않고 결과(ok=False, error)로 돌려준다. 개정 생성은 이 결과에 의존하지 않는다.
  - 저장소 파일은 <개정 폴더>/store/ 에 둔다. 매니페스트에 정본의 SHA-256을 남겨, 정본이 바뀌었는데 옛 DB를 재사용하는 일을 막는다.
  - 기존 DB는 덮어쓰지 않는다. 같은 정본이면 재사용(reused=True)한다.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path

import asis_analysis_store as astore
import asis_evidence_store as estore

STORE = 'store'
APPROVED = ('approved_areas', 'approved_directions', 'approved_actions')


def _sha(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _manifest(folder: Path) -> Path:
    return Path(folder) / STORE / 'manifest.json'


def _read_manifest(folder: Path) -> dict:
    path = _manifest(folder)
    if not path.is_file():
        return {}
    try:
        return json.loads(path.read_text(encoding='utf-8'))
    except ValueError:
        return {}


def _write_manifest(folder: Path, key: str, value: dict) -> None:
    data = _read_manifest(folder)
    data[key] = value
    path = _manifest(folder)
    tmp = path.with_name(path.name + '.tmp')
    tmp.write_text(json.dumps(data, ensure_ascii=False, sort_keys=True, indent=1), encoding='utf-8')
    tmp.replace(path)


def _cleanup(db: Path) -> None:
    for leftover in (db, db.with_name(db.name + '.loading')):
        if leftover.exists():
            leftover.chmod(0o644)
            leftover.unlink()


def load_evidence(folder) -> dict:
    """snapshot.json을 evidence.db로 적재한다."""
    folder = Path(folder)
    snap = folder / 'snapshot.json'
    if not snap.is_file():
        return {'ok': False, 'error': 'snapshot_missing'}
    db = folder / STORE / 'evidence.db'
    snap_sha = _sha(snap)
    known = _read_manifest(folder).get('evidence')
    if db.exists():
        if known and known.get('snapshot_sha256') == snap_sha and known.get('sha256') == _sha(db):
            return {'ok': True, 'reused': True, 'path': str(db), 'sha256': known['sha256'], 'snapshot_sha256': snap_sha}
        return {'ok': False, 'error': 'snapshot_changed_since_load'}
    try:
        data = json.loads(snap.read_text(encoding='utf-8'))
        db.parent.mkdir(parents=True, exist_ok=True)
        estore.load(db, facts=data['facts'], basis=data['context']['collection_basis'], criteria=data['criteria'],
                    assessments=data['context'].get('criterion_assessments', []), run=data['run'])
    except Exception as exc:  # 파생물의 실패가 개정 생성을 막지 않도록 결과로 돌려준다
        _cleanup(db)
        return {'ok': False, 'error': type(exc).__name__ + ':' + str(exc)[:200]}
    sha = _sha(db)
    _write_manifest(folder, 'evidence', {'snapshot_sha256': snap_sha, 'sha256': sha})
    return {'ok': True, 'reused': False, 'path': str(db), 'sha256': sha, 'snapshot_sha256': snap_sha}


def _stage_data(result: dict) -> dict:
    """analysis.json에서 승인된 단계 산출물을 꺼낸다. 입력의 stage_data가 우선이고, 없으면 최상위 stage_data를 쓴다."""
    for candidate in ((result.get('input') or {}).get('stage_data'), result.get('stage_data')):
        if isinstance(candidate, dict) and any(key in candidate for key in APPROVED):
            return {key: candidate[key] for key in APPROVED if key in candidate}
    return {}


def load_analysis(folder, run=None) -> dict:
    """analysis.json의 승인 단계 산출물을 analysis.db로 적재하고 참조 무결성을 확인한다."""
    folder = Path(folder)
    path = folder / 'analysis.json'
    if not path.is_file():
        return {'ok': False, 'error': 'analysis_missing'}
    db = folder / STORE / 'analysis.db'
    src_sha = _sha(path)
    known = _read_manifest(folder).get('analysis')
    try:
        result = json.loads(path.read_text(encoding='utf-8'))
        stages = _stage_data(result)
        if not stages:
            return {'ok': False, 'error': 'no_approved_stage_data'}
        use_run = run if run is not None else next(iter(stages.values()))['run']
        if db.exists():
            if known and known.get('analysis_sha256') == src_sha and known.get('sha256') == _sha(db):
                return {'ok': True, 'reused': True, 'path': str(db), 'validate': astore.open_analysis(db).validate()}
            return {'ok': False, 'error': 'analysis_changed_since_load'}
        db.parent.mkdir(parents=True, exist_ok=True)
        astore.load(db, stages, run=use_run)
    except Exception as exc:
        _cleanup(db)
        return {'ok': False, 'error': type(exc).__name__ + ':' + str(exc)[:200]}
    sha = _sha(db)
    _write_manifest(folder, 'analysis', {'analysis_sha256': src_sha, 'sha256': sha})
    return {'ok': True, 'reused': False, 'path': str(db), 'sha256': sha, 'validate': astore.open_analysis(db).validate()}


def audit_citations(folder) -> dict:
    """분석 산출물이 인용한 사실이 모두 근거 저장소에 있는지 확인한다. 둘 중 하나가 없으면 확인할 수 없다고 돌려준다."""
    folder = Path(folder)
    ev, an = folder / STORE / 'evidence.db', folder / STORE / 'analysis.db'
    if not ev.is_file() or not an.is_file():
        return {'ok': False, 'error': 'store_missing', 'missing_in_evidence': []}
    e = sqlite3.connect(f'file:{ev}?mode=ro', uri=True)
    a = sqlite3.connect(f'file:{an}?mode=ro', uri=True)
    try:
        have = {r[0] for r in e.execute('select fact_id from facts')} | {r[0] for r in e.execute('select fact_id from interview_items')}
        cited = {r[0] for r in a.execute("select distinct to_id from edges where to_kind='fact' and relation not like 'edge:%'")}
        cited |= {r[0] for r in a.execute("select distinct from_id from edges where relation like 'edge:%' and from_id like 'F\\_%' escape '\\'")}
    finally:
        e.close()
        a.close()
    missing = sorted(cited - have)
    return {'ok': not missing, 'cited': len(cited), 'missing_in_evidence': missing}
