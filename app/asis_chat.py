"""분석 담당 채팅의 상태 저장과 사람 결정 집행.

원칙
- 에이전트는 질문(question)과 해결 제안(proposal)만 올린다. 제안에는 허용된 조치 종류 중 하나만 담을 수 있고 pass 선언은 없다.
- 사람의 결정은 선택지 ID 또는 자유 문장으로 오며, 코드가 질문이 열려 있는지·선택지가 그 질문의 것인지·결정자의 권한을 검증한 뒤 기록한다.
- 한 질문은 한 번만 결정된다. 같은 결정을 다시 보내면 같은 결과를 돌려주고 새 기록을 만들지 않는다(멱등).
- 채팅은 개정 폴더 안의 chat/ 아래에만 쓴다. 분석 단계가 끝나면 닫히고 이력은 보존된다.
"""
from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import secrets
import threading
import time

# 사람이 승인해도 코드가 집행할 수 있는 조치는 이 목록뿐이다. 검토 결과를 pass로 바꾸는 조치는 없다.
ACTIONS = ('defer_to_merge', 'retry_author', 'supply_evidence', 'accept_unresolved', 'stop_revision')
CHAT_ROLES = ('analysis_owner', 'area_owner', 'global_manager', 'human')
_ID = re.compile(r'[A-Za-z0-9][A-Za-z0-9._-]{0,80}')
MAX_TEXT = 4000


class ChatError(ValueError):
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


class Chat:
    """개정 하나의 채팅. 상태는 messages.jsonl(추가만)과 questions/*.json(질문별 상태)로 둔다."""

    def __init__(self, revision_folder):
        self.root = Path(revision_folder) / 'chat'
        self.lock = threading.RLock()

    # ---- 읽기 -------------------------------------------------------------------------------
    def _read_lines(self):
        path = self.root / 'messages.jsonl'
        rows = []
        if path.is_file():
            for line in path.read_text(encoding='utf-8').splitlines():
                if line.strip():
                    rows.append(json.loads(line))
        return rows

    def messages(self, *, after=0):
        return [row for row in self._read_lines() if row['seq'] > after]

    def question(self, question_id):
        path = self.root / 'questions' / (question_id + '.json')
        if not _ID.fullmatch(str(question_id)) or not path.is_file():
            raise ChatError('question_not_found')
        return json.loads(path.read_text(encoding='utf-8'))

    def open_questions(self):
        folder = self.root / 'questions'
        rows = [json.loads(p.read_text(encoding='utf-8')) for p in sorted(folder.glob('*.json'))] if folder.is_dir() else []
        return [row for row in rows if row['status'] == 'open']

    def is_closed(self):
        return (self.root / 'closed.json').is_file()

    # ---- 쓰기 -------------------------------------------------------------------------------
    def _append(self, row):
        with self.lock:
            rows = self._read_lines()
            row = {'seq': (rows[-1]['seq'] + 1) if rows else 1, 'at': _now(), **row}
            path = self.root / 'messages.jsonl'
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(path, 'a', encoding='utf-8') as handle:
                handle.write(json.dumps(row, ensure_ascii=False) + '\n')
            return row

    def post(self, *, sender, text, kind='message', refs=None):
        """에이전트·시스템 메시지를 남긴다. 닫힌 채팅에는 쓰지 않는다."""
        if self.is_closed():
            raise ChatError('chat_closed')
        if sender not in CHAT_ROLES or not isinstance(text, str) or not text.strip() or len(text) > MAX_TEXT:
            raise ChatError('message_shape')
        return self._append({'sender': sender, 'kind': kind, 'text': text.strip(), 'refs': refs or []})

    def ask(self, *, sender, task_key, summary, options, evidence, default_action=None):
        """사람에게 질문한다. options는 {id, label, action} 목록이며 action은 허용된 조치여야 한다."""
        if self.is_closed():
            raise ChatError('chat_closed')
        if sender not in ('area_owner', 'global_manager', 'analysis_owner') or not isinstance(summary, str) or not summary.strip():
            raise ChatError('question_shape')
        if not isinstance(task_key, str) or not task_key:
            raise ChatError('question_task')
        if not isinstance(options, list) or not 1 <= len(options) <= 5:
            raise ChatError('question_options')
        seen = set()
        for option in options:
            if (not isinstance(option, dict) or set(option) != {'id', 'label', 'action'} or not _ID.fullmatch(str(option['id']))
                    or option['id'] in seen or option['action'] not in ACTIONS or not str(option['label']).strip()):
                raise ChatError('question_option_shape')
            seen.add(option['id'])
        if not isinstance(evidence, list) or not evidence:
            raise ChatError('question_evidence_required')   # 근거 없이 사람을 부르지 않는다
        with self.lock:
            for existing in self.open_questions():
                if existing['task_key'] == task_key:
                    return existing        # 같은 작업의 열린 질문은 하나만 둔다(재시작해도 중복되지 않음)
            identifier = 'q-' + secrets.token_hex(5)
            record = {'id': identifier, 'status': 'open', 'task_key': task_key, 'sender': sender, 'summary': summary.strip(),
                      'options': options, 'evidence': evidence, 'asked_at': _now(), 'decision': None}
            _atomic(self.root / 'questions' / (identifier + '.json'), json.dumps(record, ensure_ascii=False, indent=2))
            self._append({'sender': sender, 'kind': 'question', 'text': summary.strip(), 'refs': [identifier]})
            return record

    def decide(self, question_id, *, option_id=None, free_text=None, decided_by, can_decide):
        """사람의 결정을 검증해 기록한다. 선택지 결정만 조치가 되고, 자유 문장은 에이전트에게 전달되는 지시로만 남는다."""
        if can_decide is not True:
            raise ChatError('decision_not_permitted')
        if (option_id is None) == (free_text is None):
            raise ChatError('decision_one_of_option_or_text')
        if not isinstance(decided_by, str) or not decided_by:
            raise ChatError('decision_identity')
        with self.lock:
            record = self.question(question_id)
            if record['status'] != 'open':
                previous = record['decision']
                same = previous and ((option_id is not None and previous.get('option_id') == option_id)
                                     or (free_text is not None and previous.get('free_text') == free_text.strip()))
                if same:
                    return record      # 같은 결정의 재전송은 같은 결과를 돌려준다
                raise ChatError('question_already_decided')
            decision = {'id': 'dec-' + secrets.token_hex(5), 'decided_by': decided_by, 'decided_at': _now(),
                        'option_id': None, 'action': None, 'free_text': None}
            if option_id is not None:
                option = next((o for o in record['options'] if o['id'] == option_id), None)
                if option is None:
                    raise ChatError('option_not_in_question')
                decision['option_id'], decision['action'], shown = option_id, option['action'], str(option['label'])
            else:
                if not isinstance(free_text, str) or not free_text.strip() or len(free_text) > MAX_TEXT:
                    raise ChatError('decision_text_shape')
                decision['free_text'] = shown = free_text.strip()
            record = {**record, 'status': 'decided', 'decision': decision}
            _atomic(self.root / 'questions' / (question_id + '.json'), json.dumps(record, ensure_ascii=False, indent=2))
            self._append({'sender': 'human', 'kind': 'decision', 'text': shown, 'refs': [question_id, decision['id']]})
            return record

    def pending_action(self, task_key):
        """파이프라인이 읽는 결정 결과. 결정 전이면 None, 선택지 결정이면 허용된 조치, 자유 문장이면 지시 문장을 돌려준다."""
        folder = self.root / 'questions'
        for path in sorted(folder.glob('*.json'), reverse=True) if folder.is_dir() else []:
            record = json.loads(path.read_text(encoding='utf-8'))
            if record['task_key'] == task_key and record['status'] == 'decided' and not record['decision'].get('consumed'):
                return record
        return None

    def consume(self, question_id):
        """파이프라인이 결정을 한 번만 집행했음을 기록한다."""
        with self.lock:
            record = self.question(question_id)
            if record['status'] != 'decided':
                raise ChatError('question_not_decided')
            if record['decision'].get('consumed'):
                return record
            record['decision']['consumed'] = _now()
            _atomic(self.root / 'questions' / (question_id + '.json'), json.dumps(record, ensure_ascii=False, indent=2))
            return record

    def close(self, reason):
        """분석 단계가 끝나면 채팅을 닫는다. 열린 질문이 있으면 닫지 않는다."""
        with self.lock:
            if self.open_questions():
                raise ChatError('open_questions_remain')
            if not self.is_closed():
                _atomic(self.root / 'closed.json', json.dumps({'closed_at': _now(), 'reason': reason}, ensure_ascii=False))
        return json.loads((self.root / 'closed.json').read_text(encoding='utf-8'))
