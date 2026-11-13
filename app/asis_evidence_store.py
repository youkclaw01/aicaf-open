"""프로젝트별 근거 저장소(evidence.db): 수집 사실을 레코드 단위로 적재하고, 고정된 조회 함수로만 읽는다.

설계: docs/EVIDENCE-STORE-DESIGN-V2.md (3 데이터 배치, 4 조회 방식).
  - 적재는 한 번만 한다. 같은 입력은 같은 파일(바이트)을 만든다. 파일 SHA-256이 재사용 키다.
  - 읽기 핸들은 한 프로젝트 파일에만 묶인다. 프로젝트·경로·SQL을 호출 인자로 받지 않는다.
  - 모델에는 SQL을 열지 않는다. 조건은 (필드, 연산자, 값)만 받고 연산자는 고정 목록이다.
  - 모든 읽기는 작업별 접근 기록(JSONL)에 남긴다.
"""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from pathlib import Path

SCHEMA_VERSION = 1
RECORD_ID_RULE = 'sha256_16_of_canonical_json_v1'
OPERATORS = {'=': '=', '!=': '<>', '<': '<', '<=': '<=', '>': '>', '>=': '>=', 'in': 'in', 'contains': 'contains', 'prefix': 'prefix'}
# 한 번의 조회가 돌려줄 수 있는 상한. 넘으면 잘라서 돌려주고 truncated로 알린다.
MAX_ROWS = 200
MAX_CHARS = 60_000
FLAT_STRING_LIMIT = 300  # 평탄화 필드의 문자열 값 상한. 긴 본문은 조건 조회 대상이 아니라 record()로 읽는다.
INTERVIEW_ID = re.compile(r'^F_IV_(?P<qid>.+)_(?P<part>transcript|summary|score)$')

SCHEMA = """
create table meta(k text primary key, v text not null);
create table sources(source_id text primary key, kind text not null);
create table datasets(dataset_id text primary key, kind text not null, key text not null, source_file text, source_pointer text,
    source_status text, collection_status text not null, scope_complete int not null, declared_count int, observed_count int not null,
    error_count int not null, reason text, area_refs text not null, metadata text not null);
create table records(record_id text primary key, dataset_id text not null references datasets(dataset_id), seq int not null, body text not null);
create index records_by_dataset on records(dataset_id, seq);
create table record_fields(record_id text not null, field text not null, vtype text not null, vstr text, vnum real, primary key(record_id, field));
create index record_fields_by_value on record_fields(field, vstr);
create table facts(fact_id text primary key, kind text not null, source_id text not null, field_path text not null,
    collection_status text not null, scope_complete int not null, record_count int, body_chars int not null, dataset_id text);
create table fact_records(fact_id text not null, seq int not null, record_id text not null, primary key(fact_id, seq));
create table fact_values(fact_id text primary key, vtype text not null, body text not null);
create table interviews(interview_id text primary key, prefix text not null);
create table interview_items(fact_id text primary key, interview_id text not null, part text not null, body text not null);
create table criteria(criterion_id text primary key, area text, unit text, quote text, source_url text, modality text, mapping text not null);
create table matches(match_id integer primary key, criterion_id text not null, area text, unit text, target_id text, target_name text,
    assessment_status text, discovery_status text);
create table match_records(match_id integer not null, record_id text not null, association text, primary key(match_id, record_id));
create table criterion_assessments(judgment_id text primary key, criterion_id text not null, status text, applicability_confirmed int, conclusion text, fact_refs text not null);
create virtual table fts_records using fts5(record_id unindexed, body, tokenize='trigram');
create virtual table fts_interviews using fts5(interview_id unindexed, body, tokenize='trigram');
"""


def canonical(value) -> str:
    """수집기(Build-AssessmentBasis.ps1)와 같은 정규 직렬화. 레코드 ID를 두 쪽이 같게 만드는 기준이다."""
    if value is None:
        return 'null'
    if value is True:
        return 'true'
    if value is False:
        return 'false'
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        if value == int(value) and abs(value) < 1e15:
            return str(int(value))
        text = repr(value)
        if 'e' in text:
            mantissa, exponent = text.split('e')
            sign = '-' if exponent.startswith('-') else '+'
            return mantissa + 'e' + sign + (exponent.lstrip('+-').lstrip('0') or '0')
        return text
    if isinstance(value, str):
        escapes = {'"': '\\"', '\\': '\\\\'}
        return '"' + ''.join(escapes.get(ch) or ('\\u%04x' % ord(ch) if ord(ch) < 0x20 else ch) for ch in value) + '"'
    if isinstance(value, list):
        return '[' + ','.join(canonical(x) for x in value) + ']'
    return '{' + ','.join(canonical(k) + ':' + canonical(value[k]) for k in sorted(value, key=lambda s: s.encode('utf-8'))) + '}'


def record_hash(record) -> str:
    return hashlib.sha256(canonical(record).encode('utf-8')).hexdigest()[:16]


def _json(value) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(',', ':'), sort_keys=True)


def _kind(fact_id: str) -> str:
    if fact_id.startswith('F_IV_'):
        return 'interview'
    return {'F_T_': 'configuration', 'F_E_': 'evidence', 'F_D_': 'derived'}.get(fact_id[:4], 'other')


def _dataset_of(fact_id: str, dataset_ids: set[str]) -> str | None:
    for prefix, ds in (('F_T_', 'DS_T_'), ('F_E_', 'DS_E_')):
        if fact_id.startswith(prefix):
            candidate = ds + fact_id[len(prefix):]
            return candidate if candidate in dataset_ids else None
    return None


def _flatten(record: dict):
    """최상위 스칼라 필드만 조건 조회용으로 평탄화한다. 중첩 값과 긴 문자열은 record()로 본다."""
    for field, value in record.items():
        if isinstance(value, bool):
            yield field, 'bool', str(value).lower(), None
        elif isinstance(value, (int, float)):
            yield field, 'num', None, float(value)
        elif isinstance(value, str) and len(value) <= FLAT_STRING_LIMIT:
            yield field, 'str', value, None
        elif value is None:
            yield field, 'null', None, None


def _insert_records(con, dataset_id, records, declared=None):
    """한 데이터셋의 레코드를 적재하고 레코드 ID 목록을 돌려준다. 수집기 데이터셋과 사실 기반 가상 데이터셋이 같은 규칙을 쓴다."""
    if declared is not None and len(declared) != len(records):
        raise ValueError('record_id_count_mismatch:' + dataset_id)
    seen: dict[str, int] = {}
    ids = []
    for seq, record in enumerate(records):
        base = 'R:' + dataset_id + ':' + record_hash(record)
        seen[base] = seen.get(base, 0) + 1
        rid = base if seen[base] == 1 else base + '#' + str(seen[base])
        if declared is not None and declared[seq] != rid:
            raise ValueError('record_id_mismatch:' + dataset_id + ':' + str(seq))
        ids.append(rid)
        body = _json(record)
        con.execute('insert into records values(?,?,?,?)', (rid, dataset_id, seq, body))
        con.execute('insert into fts_records(record_id, body) values(?,?)', (rid, body))
        if isinstance(record, dict):
            for field, vtype, vstr, vnum in _flatten(record):
                con.execute('insert into record_fields values(?,?,?,?,?)', (rid, field, vtype, vstr, vnum))
    return ids


def load(path, *, facts, basis, criteria, assessments, run) -> Path:
    """수집 결과를 한 번에 적재한다. 같은 입력은 같은 파일을 만든다. 기존 파일은 덮어쓰지 않는다."""
    path = Path(path)
    if path.exists():
        raise FileExistsError(str(path))
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + '.loading')
    if tmp.exists():
        tmp.unlink()
    con = sqlite3.connect(tmp)
    try:
        con.execute('pragma journal_mode=OFF')
        con.executescript(SCHEMA)
        datasets = list(basis['datasets'])
        dataset_ids = {d['id'] for d in datasets}
        ids_by_dataset: dict[str, list[str]] = {}
        for d in sorted(datasets, key=lambda x: x['id']):
            con.execute('insert into datasets values(?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                        (d['id'], d['kind'], d['key'], d.get('source_file'), d.get('source_pointer'), d.get('source_status'), d['collection_status'],
                         int(bool(d['scope_complete'])), d.get('declared_record_count'), d['observed_record_count'], d['error_count'],
                         d.get('reason'), _json(d.get('area_refs', [])), _json(d.get('metadata') or {})))
            ids = _insert_records(con, d['id'], d['records'], d.get('record_ids'))
            ids_by_dataset[d['id']] = ids
        interview_prefix = set()
        for f in sorted(facts, key=lambda x: x['id']):
            fid, value = f['id'], f['value']
            ds = _dataset_of(fid, dataset_ids)
            if ds is None and _kind(fid) in ('configuration', 'evidence', 'derived') and isinstance(value, list):
                # 수집기가 데이터셋 없이 사실로만 두는 목록(resources, relationships 등)도 행 단위로 조회할 수 있게 한다.
                ds = 'FACT:' + fid
                con.execute('insert into datasets values(?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                            (ds, 'fact', fid, f['source_id'], f['field_path'], None, f['collection_status'], int(bool(f['scope_complete'])), None, len(value), 0, None, '[]', '{}'))
                ids_by_dataset[ds] = _insert_records(con, ds, value)
                dataset_ids.add(ds)
            con.execute('insert into fact_values values(?,?,?)', (fid, type(value).__name__, _json(value)))
            count = len(value) if isinstance(value, list) else None
            if ds is not None and count is not None and not ds.startswith('FACT:'):
                observed = next(d['observed_record_count'] for d in datasets if d['id'] == ds)
                if count != observed:
                    raise ValueError('fact_dataset_count_mismatch:' + fid)
            con.execute('insert into facts values(?,?,?,?,?,?,?,?,?)',
                        (fid, _kind(fid), f['source_id'], f['field_path'], f['collection_status'], int(bool(f['scope_complete'])), count, len(_json(value)), ds))
            if ds is not None and isinstance(value, list):
                for seq, rid in enumerate(ids_by_dataset[ds]):
                    con.execute('insert into fact_records values(?,?,?)', (fid, seq, rid))
            m = INTERVIEW_ID.match(fid)
            if m:
                qid = m.group('qid')
                if qid not in interview_prefix:
                    interview_prefix.add(qid)
                    con.execute('insert into interviews values(?,?)', (qid, qid[:2]))
                con.execute('insert into interview_items values(?,?,?,?)', (fid, qid, m.group('part'), '' if value is None else str(value)))
                if m.group('part') == 'transcript':
                    con.execute('insert into fts_interviews(interview_id, body) values(?,?)', (qid, '' if value is None else str(value)))
        for c in sorted(criteria, key=lambda x: x['id']):
            mapping = c.get('mapping') or {}
            con.execute('insert into criteria values(?,?,?,?,?,?,?)', (c['id'], mapping.get('area'), mapping.get('unit'), c.get('quote'), c.get('source_url'), c.get('modality'), _json(mapping)))
        for n, mt in enumerate(basis.get('criterionMatches', [])):
            con.execute('insert into matches values(?,?,?,?,?,?,?,?)', (n, mt['criterion_id'], mt.get('area'), mt.get('unit'), mt.get('target_id'), mt.get('target_name'), mt.get('assessment_status'), mt.get('target_discovery_status')))
            for ref in mt.get('record_refs', []):
                rid = ids_by_dataset[ref['dataset_id']][ref['record_index']]
                con.execute('insert or ignore into match_records values(?,?,?)', (n, rid, ref.get('association')))
        for a in sorted(assessments, key=lambda x: x['id']):
            con.execute('insert into criterion_assessments values(?,?,?,?,?,?)', (a['id'], a['criterion_id'], a.get('status'), int(bool(a.get('applicability_confirmed'))), a.get('conclusion'), _json(a.get('fact_refs', []))))
        for k, v in (('schema_version', str(SCHEMA_VERSION)), ('record_id_rule', RECORD_ID_RULE), ('run', _json(run)),
                     ('counts', _json({'datasets': len(datasets), 'facts': len(facts), 'criteria': len(criteria)}))):
            con.execute('insert into meta values(?,?)', (k, v))
        for src in sorted({f['source_id'] for f in facts}):
            con.execute('insert into sources values(?,?)', (src, 'collected'))
        con.commit()
        con.execute('vacuum')
    except Exception:
        con.close()
        tmp.unlink(missing_ok=True)
        raise
    con.close()
    tmp.replace(path)
    return path


class Project:
    """한 프로젝트의 근거 저장소에 묶인 읽기 핸들. 경로·프로젝트·SQL을 받는 메서드가 없다."""

    def __init__(self, path, access_log=None, task=None):
        self.path = Path(path)
        self._con = sqlite3.connect('file:' + str(self.path) + '?mode=ro', uri=True)
        self._con.row_factory = sqlite3.Row
        self._log = Path(access_log) if access_log else None
        self._task = task
        self._fields: dict[str, set[str]] = {}

    # -- 내부 -------------------------------------------------------------
    def _record_access(self, function, args, returned, chars):
        if self._log is None:
            return
        line = {'task': self._task, 'function': function, 'args': args, 'returned': returned, 'chars': chars}
        with self._log.open('a', encoding='utf-8') as handle:
            handle.write(json.dumps(line, ensure_ascii=False, sort_keys=True) + '\n')

    def _dataset_for(self, fact_or_dataset: str):
        if fact_or_dataset.startswith(('DS_', 'FACT:')):
            return fact_or_dataset
        row = self._con.execute('select dataset_id from facts where fact_id=?', (fact_or_dataset,)).fetchone()
        if row is None:
            raise ValueError('unknown_fact:' + fact_or_dataset)
        return row['dataset_id']

    def _known_fields(self, dataset_id):
        if dataset_id not in self._fields:
            self._fields[dataset_id] = {r[0] for r in self._con.execute(
                'select distinct field from record_fields rf join records r on r.record_id=rf.record_id where r.dataset_id=?', (dataset_id,))}
        return self._fields[dataset_id]

    def _where(self, dataset_id, where):
        clauses, params = ['r.dataset_id=?'], [dataset_id]
        known = self._known_fields(dataset_id)
        for field, (op, value) in (where or {}).items():
            if op not in OPERATORS:
                raise ValueError('operator_not_allowed:' + str(op))
            if field not in known:
                raise ValueError('unknown_field:' + field)
            col = 'vnum' if isinstance(value, (int, float)) and not isinstance(value, bool) else 'vstr'
            if op == 'in':
                marks = ','.join('?' * len(value))
                clauses.append(f'exists(select 1 from record_fields f where f.record_id=r.record_id and f.field=? and f.{col} in ({marks}))')
                params += [field, *value]
            elif op in ('contains', 'prefix'):
                like = ('%' + str(value) + '%') if op == 'contains' else (str(value) + '%')
                clauses.append('exists(select 1 from record_fields f where f.record_id=r.record_id and f.field=? and f.vstr like ?)')
                params += [field, like]
            else:
                clauses.append(f'exists(select 1 from record_fields f where f.record_id=r.record_id and f.field=? and f.{col} {OPERATORS[op]} ?)')
                params += [field, value]
        return ' and '.join(clauses), params

    # -- 공개 조회 ----------------------------------------------------------
    def meta(self):
        out = {r['k']: r['v'] for r in self._con.execute('select k,v from meta')}
        out['sha256'] = hashlib.sha256(self.path.read_bytes()).hexdigest()
        return out

    def fact(self, fact_id):
        row = self._con.execute('select * from facts where fact_id=?', (fact_id,)).fetchone()
        if row is None:
            self._record_access('fact', {'fact_id': fact_id}, 0, 0)
            return None
        out = dict(row)
        out['scope_complete'] = bool(out['scope_complete'])
        self._record_access('fact', {'fact_id': fact_id}, 1, len(_json(out)))
        return out

    def area_facts(self, area):
        """수집기가 영역(area_refs)에 연결한 구조화 사실의 메타. 값은 읽지 않는다."""
        rows = self._con.execute("select dataset_id, key, kind, area_refs from datasets where kind in ('configuration','evidence') order by dataset_id").fetchall()
        out = []
        for r in rows:
            if area not in json.loads(r['area_refs']):
                continue
            fid = ('F_T_' if r['kind'] == 'configuration' else 'F_E_') + r['key']
            f = self._con.execute('select collection_status, scope_complete, record_count from facts where fact_id=?', (fid,)).fetchone()
            if f is None:
                continue
            out.append({'fact_id': fid, 'dataset_id': r['dataset_id'], 'collection_status': f['collection_status'],
                        'scope_complete': bool(f['scope_complete']), 'record_count': f['record_count']})
        self._record_access('area_facts', {'area': area}, len(out), len(_json(out)))
        return out

    def all_interview_ids(self):
        """적재된 모든 인터뷰 문항 ID."""
        ids = [r[0] for r in self._con.execute('select interview_id from interviews order by interview_id')]
        self._record_access('all_interview_ids', {}, len(ids), len(_json(ids)))
        return ids

    def fact_value(self, fact_id):
        """사실의 전체 값(작은 값·스칼라용). 큰 목록은 rows()/record()로 필요한 행만 읽는다."""
        row = self._con.execute('select body from fact_values where fact_id=?', (fact_id,)).fetchone()
        if row is None:
            self._record_access('fact_value', {'fact_id': fact_id}, 0, 0)
            return None
        value = json.loads(row['body'])
        self._record_access('fact_value', {'fact_id': fact_id}, 1, len(row['body']))
        return value

    def rows(self, fact_id, fields, where, limit=50):
        dataset_id = self._dataset_for(fact_id)
        if dataset_id is None:
            self._record_access('rows', {'fact_id': fact_id, 'fields': list(fields)}, 0, 0)
            return {'total': 0, 'returned': 0, 'truncated': False, 'rows': []}
        known = self._known_fields(dataset_id)
        if not known and self._con.execute('select count(*) from records where dataset_id=?', (dataset_id,)).fetchone()[0] == 0:
            # 레코드가 없는 데이터셋(미수집·부재)은 필드를 검증할 근거가 없다. 빈 결과로 돌려주고 상태는 fact()/summary()로 본다.
            self._record_access('rows', {'fact_id': fact_id, 'fields': list(fields)}, 0, 0)
            return {'total': 0, 'returned': 0, 'truncated': False, 'rows': []}
        for field in fields:
            if field not in known:
                raise ValueError('unknown_field:' + field)
        clause, params = self._where(dataset_id, where)
        total = self._con.execute(f'select count(*) from records r where {clause}', params).fetchone()[0]
        limit = max(1, min(int(limit), MAX_ROWS))
        ids = [r[0] for r in self._con.execute(f'select r.record_id from records r where {clause} order by r.seq limit ?', [*params, limit])]
        rows, chars = [], 0
        for rid in ids:
            body = {r['field']: (r['vstr'] if r['vtype'] == 'str' else r['vnum'] if r['vtype'] == 'num' else (r['vstr'] == 'true') if r['vtype'] == 'bool' else None)
                    for r in self._con.execute('select field,vtype,vstr,vnum from record_fields where record_id=?', (rid,)) if r['field'] in fields}
            row = {'record_id': rid, **{f: body.get(f) for f in fields}}
            chars += len(_json(row))
            if chars > MAX_CHARS:
                break
            rows.append(row)
        out = {'total': total, 'returned': len(rows), 'truncated': len(rows) < total, 'rows': rows}
        self._record_access('rows', {'fact_id': fact_id, 'fields': list(fields), 'where': {k: list(v) for k, v in (where or {}).items()}, 'limit': limit}, len(rows), chars)
        return out

    def count(self, dataset_id, where=None):
        clause, params = self._where(self._dataset_for(dataset_id), where)
        n = self._con.execute(f'select count(*) from records r where {clause}', params).fetchone()[0]
        self._record_access('count', {'dataset_id': dataset_id, 'where': {k: list(v) for k, v in (where or {}).items()}}, n, len(str(n)))
        return n

    def summary(self, dataset_id, top=5):
        dataset_id = self._dataset_for(dataset_id)
        d = self._con.execute('select * from datasets where dataset_id=?', (dataset_id,)).fetchone()
        if d is None:
            raise ValueError('unknown_dataset:' + str(dataset_id))
        fields = {}
        for field in sorted(self._known_fields(dataset_id)):
            rows = self._con.execute('select f.vstr, f.vnum, count(*) c from record_fields f join records r on r.record_id=f.record_id '
                                     'where r.dataset_id=? and f.field=? group by f.vstr, f.vnum order by c desc, f.vstr limit ?', (dataset_id, field, top)).fetchall()
            distinct = self._con.execute('select count(*) from (select 1 from record_fields f join records r on r.record_id=f.record_id '
                                         'where r.dataset_id=? and f.field=? group by f.vstr, f.vnum)', (dataset_id, field)).fetchone()[0]
            fields[field] = {'distinct': distinct, 'top': [(r['vstr'] if r['vstr'] is not None else r['vnum'], r['c']) for r in rows]}
        out = {'dataset_id': dataset_id, 'collection_status': d['collection_status'], 'scope_complete': bool(d['scope_complete']),
               'declared_count': d['declared_count'], 'observed_count': d['observed_count'], 'error_count': d['error_count'],
               'area_refs': json.loads(d['area_refs']), 'fields': fields}
        self._record_access('summary', {'dataset_id': dataset_id}, len(fields), len(_json(out)))
        return out

    def find(self, dataset_id, token, limit=5):
        """데이터셋 안에서 값에 토큰이 들어 있는 레코드 ID. 토큰은 리터럴로만 다루고 검색 연산자로 해석하지 않는다."""
        limit = max(1, min(int(limit), MAX_ROWS))
        literal = '"' + str(token).replace('"', '""') + '"'
        try:
            rows = self._con.execute(
                'select f.record_id from fts_records f join records r on r.record_id=f.record_id '
                'where r.dataset_id=? and fts_records match ? order by r.seq limit ?', (dataset_id, literal, limit)).fetchall()
        except sqlite3.OperationalError:
            rows = []
        ids = [r[0] for r in rows]
        self._record_access('find', {'dataset_id': dataset_id, 'token': str(token)[:80], 'limit': limit}, len(ids), len(_json(ids)))
        return ids

    def record(self, record_ids):
        out = {}
        for rid in record_ids:
            row = self._con.execute('select body from records where record_id=?', (rid,)).fetchone()
            if row is not None:
                out[rid] = json.loads(row['body'])
        self._record_access('record', {'record_ids': list(record_ids)}, len(out), len(_json(out)))
        return out

    def interview(self, interview_id):
        rows = self._con.execute('select part, body from interview_items where interview_id=?', (interview_id,)).fetchall()
        out = {r['part']: r['body'] for r in rows} or None
        self._record_access('interview', {'interview_id': interview_id}, 1 if out else 0, len(_json(out)) if out else 0)
        return out

    def search_interviews(self, text, limit=10):
        match = '"' + text.replace('"', '""') + '"'
        rows = self._con.execute('select interview_id, snippet(fts_interviews, 1, "[", "]", "...", 12) s from fts_interviews where fts_interviews match ? limit ?',
                                 (match, max(1, min(int(limit), 50)))).fetchall()
        out = [{'interview_id': r['interview_id'], 'excerpt': r['s']} for r in rows]
        self._record_access('search_interviews', {'text': text, 'limit': limit}, len(out), len(_json(out)))
        return out

    def refs(self, criterion_id):
        rows = self._con.execute('select * from matches where criterion_id=? order by match_id', (criterion_id,)).fetchall()
        out = {'criterion_id': criterion_id, 'matches': []}
        for m in rows:
            rids = [r[0] for r in self._con.execute('select record_id from match_records where match_id=? order by record_id', (m['match_id'],))]
            out['matches'].append({'target_id': m['target_id'], 'target_name': m['target_name'], 'unit': m['unit'], 'area': m['area'],
                                   'assessment_status': m['assessment_status'], 'discovery_status': m['discovery_status'], 'record_ids': rids})
        self._record_access('refs', {'criterion_id': criterion_id}, len(out['matches']), len(_json(out)))
        return out

    def assessment(self, criterion_id):
        row = self._con.execute('select * from criterion_assessments where criterion_id=?', (criterion_id,)).fetchone()
        if row is None:
            self._record_access('assessment', {'criterion_id': criterion_id}, 0, 0)
            return None
        out = {'judgment_id': row['judgment_id'], 'status': row['status'], 'applicability_confirmed': bool(row['applicability_confirmed']),
               'conclusion': row['conclusion'], 'fact_refs': json.loads(row['fact_refs'])}
        self._record_access('assessment', {'criterion_id': criterion_id}, 1, len(_json(out)))
        return out


def open_project(path, *, access_log=None, task=None) -> Project:
    """프로젝트 하나의 근거 저장소를 읽기 전용으로 연다."""
    return Project(path, access_log=access_log, task=task)
