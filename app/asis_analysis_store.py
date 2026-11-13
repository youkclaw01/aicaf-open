"""분석 산출물 저장소(analysis.db): 관측·방향·진단·과제와 그 관계(edges)를 정본으로 저장하고, 고정 조회 함수로만 읽는다.

설계: docs/EVIDENCE-STORE-DESIGN-V2.md 6절.
  - 정본은 DB다. 기존 단계 JSON(approved_areas / approved_directions / approved_actions)은 DB에서 다시 만들어 낸 파생물이며,
    export_stage()의 결과는 적재한 원본과 같아야 한다(왕복 동일성).
  - 모든 항목은 한 행(items)이고, 참조(근거 사실·기준·목표·방향 ...)는 모두 한 행씩 edges에 남긴다.
  - 근거 저장소(evidence.db)와 파일이 분리된다. 항목이 인용한 사실 ID는 evidence.db의 키와 같다.
  - 읽기 핸들은 한 프로젝트 파일에만 묶이고, 경로·SQL을 호출 인자로 받지 않는다. 모든 읽기는 접근 기록에 남긴다.
"""
from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path

SCHEMA_VERSION = 1
STAGES = ('observation', 'area', 'direction', 'diagnosis', 'action')
MAX_ROWS = 200

SCHEMA = """
create table meta(k text primary key, v text not null);
create table stages(stage_key text primary key, header text not null, tail text not null);
create table items(item_id text primary key, stage text not null, stage_key text not null, area text, seq int not null, body text not null);
create index items_by_stage on items(stage, seq);
create index items_by_area on items(area, stage, seq);
create table edges(edge_id integer primary key, from_id text not null, relation text not null, to_id text not null, to_kind text not null);
create index edges_by_from on edges(from_id);
create index edges_by_to on edges(to_id);
create table unresolved(stage_key text not null, seq int not null, body text not null, primary key(stage_key, seq));
create virtual table fts_items using fts5(item_id unindexed, body, tokenize='trigram');
"""

# 항목 안의 참조 필드 -> 참조 종류
REF_FIELDS = {'fact_refs': 'fact', 'criterion_refs': 'criterion', 'goal_refs': 'goal', 'judgment_refs': 'judgment',
              'direction_refs': 'direction', 'diagnosis_refs': 'diagnosis', 'area_implication_refs': 'implication'}
# 항목 ID에서 종류를 알아내는 접두 규칙(수집기·분석기 ID 체계)
KIND_BY_PREFIX = (('O_', 'observation'), ('D_', 'direction'), ('J_', 'diagnosis'), ('A_', 'action'), ('F_', 'fact'), ('G_', 'goal'), ('I_', 'implication'))
ITEM_KINDS = {'observation', 'direction', 'diagnosis', 'action', 'area', 'implication'}
TEXT_FIELDS = ('text', 'title', 'headline', 'implication', 'customer_impact', 'change_direction', 'impact', 'rationale', 'deliverable', 'target')


def _json(value) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def _kind_of(identifier: str) -> str:
    for prefix, kind in KIND_BY_PREFIX:
        if identifier.startswith(prefix):
            return kind
    return 'other'


def _stage_items(stage_key: str, data: dict):
    """단계 JSON에서 (stage, area, body) 항목을 뽑는다. 항목의 순서는 원본 순서를 그대로 보존한다."""
    if stage_key == 'approved_areas':
        for area in data['areas']:
            yield 'area', area['area_id'], area['area_id'], {k: v for k, v in area.items() if k != 'observations'}, [o['id'] for o in area['observations']]
            for obs in area['observations']:
                yield 'observation', obs['id'], area['area_id'], obs, None
    elif stage_key == 'approved_directions':
        for d in data['directions']:
            yield 'direction', d['id'], None, d, None
    elif stage_key == 'approved_actions':
        for d in data['diagnoses']:
            yield 'diagnosis', d['id'], None, d, None
        for a in data['actions']:
            yield 'action', a['id'], None, a, None
    else:
        raise ValueError('unknown_stage_key:' + stage_key)


_LISTS = {'approved_areas': ('areas',), 'approved_directions': ('directions',), 'approved_actions': ('diagnoses', 'actions')}


def load(path, stage_data: dict, *, run: dict) -> Path:
    """승인된 단계 산출물을 한 번에 적재한다. 같은 입력은 같은 파일을 만든다. 기존 파일은 덮어쓰지 않는다."""
    path = Path(path)
    if path.exists():
        raise FileExistsError(str(path))
    path.parent.mkdir(parents=True, exist_ok=True)
    seen: set[str] = set()
    for key, data in stage_data.items():
        if data.get('run') != run:
            raise ValueError('run_mismatch:' + key)
    tmp = path.with_name(path.name + '.loading')
    if tmp.exists():
        tmp.unlink()
    con = sqlite3.connect(tmp)
    try:
        con.execute('pragma journal_mode=OFF')
        con.executescript(SCHEMA)
        con.execute('insert into meta values(?,?)', ('schema_version', str(SCHEMA_VERSION)))
        con.execute('insert into meta values(?,?)', ('run', _json(run)))
        edge_seen: set[tuple[str, str, str]] = set()
        for key in ('approved_areas', 'approved_directions', 'approved_actions'):
            if key not in stage_data:
                continue
            data = stage_data[key]
            list_names = _LISTS[key]
            # 단계 머리말(리스트와 unresolved 이외의 모든 키)과 꼬리(unresolved·edges)를 따로 보존해 export가 원본과 같게 한다.
            header = {k: v for k, v in data.items() if k not in list_names and k not in ('unresolved', 'edges')}
            tail_keys = [k for k in data if k in ('unresolved', 'edges')]
            con.execute('insert into stages values(?,?,?)', (key, _json({'header': header, 'order': list(data.keys()), 'lists': list(list_names)}), _json(tail_keys)))
            seq = 0
            for stage, ident, area, body, child_ids in _stage_items(key, data):
                if ident in seen:
                    raise ValueError('duplicate_id:' + ident)
                seen.add(ident)
                con.execute('insert into items values(?,?,?,?,?,?)', (ident, stage, key, area, seq, _json(body)))
                text = ' '.join(str(body.get(f, '')) for f in TEXT_FIELDS if body.get(f))
                con.execute('insert into fts_items(item_id, body) values(?,?)', (ident, text))
                for field, kind in REF_FIELDS.items():
                    for ref in body.get(field) or []:
                        k = (ident, field, ref)
                        if k not in edge_seen:
                            edge_seen.add(k)
                            con.execute('insert into edges(from_id, relation, to_id, to_kind) values(?,?,?,?)', (ident, field, ref, kind))
                if stage == 'observation':
                    con.execute('insert into edges(from_id, relation, to_id, to_kind) values(?,?,?,?)', (ident, 'in_area', area, 'area'))
                seq += 1
            for n, edge in enumerate(data.get('edges', []) if 'edges' in data else []):
                con.execute('insert into edges(from_id, relation, to_id, to_kind) values(?,?,?,?)', (edge['from_id'], 'edge:' + edge['relation'], edge['to_id'], _kind_of(edge['to_id'])))
            for n, u in enumerate(data.get('unresolved', [])):
                con.execute('insert into unresolved values(?,?,?)', (key, n, _json(u)))
            if 'edges' in data:
                con.execute('insert into meta values(?,?)', ('edges:' + key, _json(data['edges'])))
        con.commit()
    finally:
        con.close()
    tmp.replace(path)
    path.chmod(0o444)
    return path


class Analysis:
    """한 프로젝트의 분석 저장소에 묶인 읽기 핸들."""

    def __init__(self, path, access_log=None, task=None):
        self.path = Path(path)
        self._con = sqlite3.connect('file:' + str(self.path) + '?mode=ro', uri=True)
        self._con.row_factory = sqlite3.Row
        self._log = Path(access_log) if access_log else None
        self._task = task

    def _access(self, function, args, returned, chars):
        if self._log is None:
            return
        with self._log.open('a', encoding='utf-8') as handle:
            handle.write(json.dumps({'task': self._task, 'function': function, 'args': args, 'returned': returned, 'chars': chars}, ensure_ascii=False, sort_keys=True) + '\n')

    def _item(self, row):
        body = json.loads(row['body'])
        return {**body, 'stage': row['stage'], 'item_id': row['item_id'], 'area': row['area']}

    def get(self, item_id):
        row = self._con.execute('select * from items where item_id=?', (item_id,)).fetchone()
        if row is None and item_id.startswith('I_'):
            row = self._con.execute("select * from items where item_id=? and stage='area'", (item_id[2:],)).fetchone()
        out = self._item(row) if row else None
        self._access('get', {'item_id': item_id}, 1 if out else 0, len(_json(out)) if out else 0)
        return out

    def list(self, stage, *, area=None, limit=MAX_ROWS):
        if stage not in ITEM_KINDS:
            raise ValueError('unknown_stage:' + str(stage))
        limit = max(1, min(int(limit), MAX_ROWS))
        if area is None:
            rows = self._con.execute('select * from items where stage=? order by seq limit ?', (stage, limit)).fetchall()
        else:
            rows = self._con.execute('select * from items where stage=? and area=? order by seq limit ?', (stage, area, limit)).fetchall()
        out = [self._item(r) for r in rows]
        self._access('list', {'stage': stage, 'area': area, 'limit': limit}, len(out), len(_json(out)))
        return out

    def refs(self, item_id):
        out = [{'relation': r['relation'], 'kind': r['to_kind'], 'id': r['to_id']} for r in
               self._con.execute('select relation, to_kind, to_id from edges where from_id=? and relation not like "edge:%" order by edge_id', (item_id,))]
        inn = [{'relation': r['relation'], 'id': r['from_id']} for r in
               self._con.execute('select relation, from_id from edges where to_id=? and relation not like "edge:%" order by edge_id', (item_id,))]
        result = {'out': out, 'in': inn}
        self._access('refs', {'item_id': item_id}, len(out) + len(inn), len(_json(result)))
        return result

    def used_by(self, ref_id, *, limit=MAX_ROWS):
        """이 사실·기준·방향을 인용한 항목."""
        limit = max(1, min(int(limit), MAX_ROWS))
        rows = self._con.execute(
            'select distinct i.item_id, i.stage from edges e join items i on i.item_id=e.from_id where e.to_id=? and e.relation not like "edge:%" order by i.stage, i.seq limit ?',
            (ref_id, limit)).fetchall()
        out = [{'id': r['item_id'], 'stage': r['stage']} for r in rows]
        self._access('used_by', {'ref_id': ref_id}, len(out), len(_json(out)))
        return out

    def chain(self, item_id, *, limit=MAX_ROWS):
        """항목에서 시작해 참조를 끝까지 따라간 모든 항목·사실. 순환이 있어도 한 번씩만 돌려준다."""
        limit = max(1, min(int(limit), MAX_ROWS))
        seen: dict[str, str] = {}
        stack = [item_id]
        while stack and len(seen) < limit:
            cur = stack.pop()
            for r in self._con.execute('select to_id, to_kind from edges where from_id=? and relation not like "edge:%" order by edge_id', (cur,)):
                if len(seen) >= limit:
                    break  # 항목 하나의 참조가 상한을 넘어도 돌려주는 양은 상한을 넘지 않는다
                if r['to_id'] not in seen and r['to_id'] != item_id:
                    seen[r['to_id']] = r['to_kind']
                    if r['to_kind'] in ITEM_KINDS:
                        stack.append(r['to_id'])
        out = [{'id': k, 'kind': v} for k, v in seen.items()]
        self._access('chain', {'item_id': item_id}, len(out), len(_json(out)))
        return out

    def unresolved(self, stage_key):
        out = [json.loads(r['body']) for r in self._con.execute('select body from unresolved where stage_key=? order by seq', (stage_key,))]
        self._access('unresolved', {'stage_key': stage_key}, len(out), len(_json(out)))
        return out

    def search(self, token, *, limit=20):
        limit = max(1, min(int(limit), MAX_ROWS))
        literal = '"' + str(token).replace('"', '""') + '"'
        try:
            rows = self._con.execute('select item_id from fts_items where fts_items match ? limit ?', (literal, limit)).fetchall()
        except sqlite3.OperationalError:
            rows = []
        out = [{'id': r['item_id']} for r in rows]
        self._access('search', {'token': str(token)[:80], 'limit': limit}, len(out), len(_json(out)))
        return out

    def validate(self):
        """참조가 가리키는 항목이 실제로 있는지 확인한다. 사실·기준·목표는 다른 저장소의 키라 여기서는 검사하지 않는다."""
        ids = {r[0] for r in self._con.execute('select item_id from items')}
        ids |= {'I_' + r[0] for r in self._con.execute("select item_id from items where stage='area'")}
        dangling = []
        for r in self._con.execute('select from_id, relation, to_id, to_kind from edges where relation not like "edge:%" order by edge_id'):
            if r['to_kind'] in ITEM_KINDS and r['to_id'] not in ids:
                dangling.append({'item': r['from_id'], 'ref': r['to_id'], 'kind': r['to_kind']})
        out = {'ok': not dangling, 'dangling': dangling, 'items': len(ids)}
        self._access('validate', {}, len(dangling), len(_json(out)))
        return out

    def export_stage(self, stage_key):
        """단계 JSON을 DB에서 다시 만든다. 적재한 원본과 같아야 한다."""
        row = self._con.execute('select header, tail from stages where stage_key=?', (stage_key,)).fetchone()
        if row is None:
            raise ValueError('unknown_stage_key:' + stage_key)
        info = json.loads(row['header'])
        header, order, lists = info['header'], info['order'], info['lists']
        data = dict(header)
        if stage_key == 'approved_areas':
            areas = []
            for a in self._con.execute("select body from items where stage_key=? and stage='area' order by seq", (stage_key,)):
                body = json.loads(a['body'])
                body['observations'] = [json.loads(o['body']) for o in self._con.execute(
                    "select body from items where stage_key=? and stage='observation' and area=? order by seq", (stage_key, body['area_id']))]
                areas.append(body)
            data['areas'] = areas
        else:
            stage_of = {'directions': 'direction', 'diagnoses': 'diagnosis', 'actions': 'action'}
            for name in lists:
                data[name] = [json.loads(r['body']) for r in self._con.execute('select body from items where stage_key=? and stage=? order by seq', (stage_key, stage_of[name]))]
        unresolved = [json.loads(r['body']) for r in self._con.execute('select body from unresolved where stage_key=? order by seq', (stage_key,))]
        if 'unresolved' in order:
            data['unresolved'] = unresolved
        edges_row = self._con.execute('select v from meta where k=?', ('edges:' + stage_key,)).fetchone()
        if 'edges' in order and edges_row is not None:
            data['edges'] = json.loads(edges_row['v'])
        out = {k: data[k] for k in order if k in data}
        self._access('export_stage', {'stage_key': stage_key}, 1, len(_json(out)))
        return out


def open_analysis(path, *, access_log=None, task=None) -> Analysis:
    return Analysis(path, access_log=access_log, task=task)
