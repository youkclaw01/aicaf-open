"""문제 해결 기록(사건)과 지식 노트 저장소.

Azure SRE Agent의 기억 구조(과거 사건·지식 파일)를 따르되, 우리 시스템에 맞게 다음을 코드로 강제한다.
- 사건은 증상·효과 있던 단계·근본 원인·피할 것 4항목을 구조로 저장한다.
- 모든 항목은 출처(원장·체크포인트·이벤트의 실제 파일 경로 또는 사람 결정 ID)를 가져야 하며, 출처가 없거나 실제로 없는 파일이면 저장하지 않는다.
- 지식은 고객(assessment)별로 격리한다. 다른 고객의 기록을 읽거나 쓰는 경로가 없다.
- 지식을 갱신할 때 기존 내용과 충돌하면 병합하지 않고 확인 대기로 남긴다.
"""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import time

INCIDENT_KEYS = ('symptoms', 'steps_that_worked', 'root_cause', 'pitfalls')
OVERVIEW_BUDGET = 2000   # overview.md는 항상 모델 입력에 실리므로 크기를 제한한다.
TOPIC_BUDGET = 1500
_SAFE = re.compile(r'[A-Za-z0-9][A-Za-z0-9._-]{0,80}')


class KnowledgeError(ValueError):
    pass


def _now():
    return datetime.now(timezone.utc).isoformat()


def _atomic(path, text):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + f'.tmp-{os.getpid()}-{secrets.token_hex(6)}')
    temporary.write_text(text, encoding='utf-8')
    for attempt in range(7):
        try:
            os.replace(temporary, path)
            return
        except PermissionError:
            if attempt == 6:
                raise
            time.sleep(.05 * (attempt + 1))


class Store:
    """고객 하나의 기록 저장소. 루트는 고객별 폴더이며 이 클래스는 그 안으로만 읽고 쓴다."""

    def __init__(self, customer_root, *, source_root):
        self.root = Path(customer_root)
        self.source_root = Path(source_root).resolve()
        self.incidents = self.root / 'incidents'
        self.knowledge = self.root / 'knowledge'

    # ---- 출처 검증 ---------------------------------------------------------------------------
    def _verify_sources(self, sources):
        if not isinstance(sources, list) or not sources:
            raise KnowledgeError('source_required')
        checked = []
        for row in sources:
            if not isinstance(row, dict) or set(row) - {'path', 'sha256', 'decision_id', 'note'}:
                raise KnowledgeError('source_shape')
            if row.get('decision_id') is not None:
                if not isinstance(row['decision_id'], str) or not _SAFE.fullmatch(row['decision_id']):
                    raise KnowledgeError('source_decision_id')
                checked.append({'decision_id': row['decision_id']})
                continue
            path = row.get('path')
            if not isinstance(path, str) or not path:
                raise KnowledgeError('source_path_required')
            target = (self.source_root / path).resolve()
            if not target.is_relative_to(self.source_root) or not target.is_file():
                raise KnowledgeError('source_file_missing:' + path)
            digest = hashlib.sha256(target.read_bytes()).hexdigest()
            if row.get('sha256') is not None and row['sha256'] != digest:
                raise KnowledgeError('source_hash_mismatch:' + path)
            checked.append({'path': path, 'sha256': digest})
        return checked

    # ---- 사건 ---------------------------------------------------------------------------------
    def record_incident(self, *, kind, subject, symptoms, steps_that_worked, root_cause, pitfalls, sources, resolution):
        """사건 하나를 4항목 구조로 기록한다. 같은 대상(subject)의 과거 사건은 같은 파일 묶음에서 먼저 찾는다."""
        if not isinstance(kind, str) or not _SAFE.fullmatch(kind) or not isinstance(subject, str) or not subject.strip():
            raise KnowledgeError('incident_identity')
        if resolution not in ('resolved_by_code', 'resolved_by_agent', 'resolved_by_human', 'unresolved'):
            raise KnowledgeError('incident_resolution')
        values = {'symptoms': symptoms, 'steps_that_worked': steps_that_worked, 'root_cause': root_cause, 'pitfalls': pitfalls}
        for key, value in values.items():
            if not isinstance(value, list) or any(not isinstance(item, str) or not item.strip() for item in value):
                raise KnowledgeError('incident_field:' + key)
        if not values['symptoms']:
            raise KnowledgeError('incident_symptoms_required')
        if resolution != 'unresolved' and not values['root_cause'] and not values['steps_that_worked']:
            raise KnowledgeError('resolved_incident_needs_cause_or_steps')
        verified = self._verify_sources(sources)
        stamp = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S')
        identifier = f'{stamp}-{kind}-{secrets.token_hex(3)}'
        record = {'id': identifier, 'recorded_at': _now(), 'kind': kind, 'subject': subject.strip(), 'resolution': resolution,
                  **{key: [item.strip() for item in value] for key, value in values.items()}, 'sources': verified}
        _atomic(self.incidents / (identifier + '.json'), json.dumps(record, ensure_ascii=False, indent=2))
        return record

    def all_incidents(self):
        """저장된 사건 전체를 최근 순으로 읽는다. 깨진 파일은 건너뛰되 건수를 숨기지 않도록 호출자가 비교할 수 있다."""
        rows = []
        for path in sorted(self.incidents.glob('*.json'), reverse=True) if self.incidents.is_dir() else []:
            try:
                rows.append(json.loads(path.read_text(encoding='utf-8')))
            except (ValueError, OSError):
                continue
        return rows

    def similar_incidents(self, *, kind=None, subject=None, limit=5):
        """같은 대상의 과거 사건을 먼저, 그다음 같은 종류를 최근 순으로 돌려준다. 둘 다 맞지 않는 사건은 포함하지 않는다."""
        rows = []
        for row in self.all_incidents():
            score = (2 if subject and row.get('subject') == subject else 0) + (1 if kind and row.get('kind') == kind else 0)
            if score:
                rows.append((score, row['recorded_at'], row))
        rows.sort(key=lambda item: (item[0], item[1]), reverse=True)
        return [row for _, _, row in rows[:limit]]

    # ---- 지식 노트 ----------------------------------------------------------------------------
    def _topic_path(self, topic):
        if not isinstance(topic, str) or not _SAFE.fullmatch(topic) or topic == 'overview':
            raise KnowledgeError('topic_name')
        return self.knowledge / (topic + '.md')

    def read_overview(self):
        path = self.knowledge / 'overview.md'
        return path.read_text(encoding='utf-8') if path.is_file() else ''

    def write_topic(self, topic, text, *, sources, replaces=None):
        """주제 노트를 쓴다. 기존 노트와 내용이 다른데 replaces가 기존 내용 해시와 같지 않으면 병합하지 않고 충돌로 남긴다."""
        if not isinstance(text, str) or not text.strip() or len(text) > TOPIC_BUDGET:
            raise KnowledgeError('topic_text_budget')
        verified = self._verify_sources(sources)
        path = self._topic_path(topic)
        existing = path.read_text(encoding='utf-8') if path.is_file() else None
        if existing is not None and existing != text:
            current = hashlib.sha256(existing.encode('utf-8')).hexdigest()
            if replaces != current:
                conflict = {'topic': topic, 'existing_sha256': current, 'proposed_text': text,
                            'sources': verified, 'recorded_at': _now()}
                _atomic(self.knowledge / 'conflicts' / f'{topic}-{secrets.token_hex(3)}.json',
                        json.dumps(conflict, ensure_ascii=False, indent=2))
                raise KnowledgeError('topic_conflict_needs_confirmation:' + topic)
        header = '<!-- sources: ' + json.dumps(verified, ensure_ascii=False, separators=(',', ':')) + ' -->\n'
        _atomic(path, header + text)
        self._rebuild_overview()
        return path

    def _rebuild_overview(self):
        """overview.md는 주제 목록의 색인이다. 크기 한도를 넘으면 오래된 항목부터 빠진다."""
        lines = ['# 지식 색인', '']
        for path in sorted(self.knowledge.glob('*.md'), key=lambda p: p.stat().st_mtime, reverse=True):
            if path.name == 'overview.md':
                continue
            body = path.read_text(encoding='utf-8').split('\n', 1)[-1].strip().splitlines()
            first = next((line.strip('# ').strip() for line in body if line.strip()), '')
            lines.append(f'- [{path.stem}]({path.name}): {first[:80]}')
        text = '\n'.join(lines)
        while len(text) > OVERVIEW_BUDGET and len(lines) > 3:
            lines.pop()
            text = '\n'.join(lines)
        _atomic(self.knowledge / 'overview.md', text + '\n')

    def search(self, query, *, limit=5):
        """질문에 쓸 사건과 노트를 단순 키워드로 찾는다. 결과마다 출처 목록을 함께 돌려준다."""
        words = [w for w in re.findall(r'[A-Za-z0-9가-힣_.-]{2,}', str(query).lower()) if len(w) >= 2][:12]
        hits = []
        for row in self.all_incidents():
            blob = json.dumps(row, ensure_ascii=False).lower()
            score = sum(blob.count(w) for w in words)
            if score:
                hits.append({'type': 'incident', 'id': row['id'], 'score': score, 'subject': row['subject'],
                             'root_cause': row['root_cause'], 'steps_that_worked': row['steps_that_worked'],
                             'pitfalls': row['pitfalls'], 'sources': row['sources']})
        for path in self.knowledge.glob('*.md') if self.knowledge.is_dir() else []:
            if path.name == 'overview.md':
                continue
            text = path.read_text(encoding='utf-8')
            score = sum(text.lower().count(w) for w in words)
            if score:
                hits.append({'type': 'knowledge', 'topic': path.stem, 'score': score, 'text': text.split('\n', 1)[-1].strip()})
        hits.sort(key=lambda row: row['score'], reverse=True)
        return hits[:limit]
