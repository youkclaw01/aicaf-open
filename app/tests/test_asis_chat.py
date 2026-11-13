"""분석 담당 채팅: 근거 없는 질문 금지, 결정은 코드가 검증, 한 질문 한 번 결정, 닫힌 채팅은 쓰기 불가."""
from pathlib import Path
import sys, threading
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import pytest
from asis_chat import Chat, ChatError, ACTIONS

OPTIONS = [{'id': 'defer', 'label': '병합 검토로 넘긴다', 'action': 'defer_to_merge'},
           {'id': 'retry', 'label': '작성자에게 되돌린다', 'action': 'retry_author'}]
EVIDENCE = [{'path': 'checkpoints/area-cost-O_P0226/round-001/author-response.txt'}]


def ask(chat, **overrides):
    base = dict(sender='area_owner', task_key='area-cost-O_P0226', summary='검토자가 다른 방향 본문이 없어 확인하지 못했다고 합니다.',
                options=OPTIONS, evidence=EVIDENCE)
    base.update(overrides)
    return chat.ask(**base)


def test_question_requires_evidence_and_only_allowed_actions(tmp_path):
    chat = Chat(tmp_path)
    with pytest.raises(ChatError, match='question_evidence_required'):
        ask(chat, evidence=[])
    with pytest.raises(ChatError, match='question_option_shape'):
        ask(chat, options=[{'id': 'x', 'label': '통과로 바꾼다', 'action': 'declare_pass'}])
    assert 'declare_pass' not in ACTIONS and 'pass' not in ACTIONS
    with pytest.raises(ChatError, match='question_options'):
        ask(chat, options=[])


def test_same_task_keeps_a_single_open_question_after_restart(tmp_path):
    first = ask(Chat(tmp_path))
    second = ask(Chat(tmp_path))     # 새 인스턴스 = 프로세스 재시작
    assert first['id'] == second['id'] and len(Chat(tmp_path).open_questions()) == 1


def test_option_decision_becomes_a_code_enforced_action_and_is_idempotent(tmp_path):
    chat = Chat(tmp_path)
    q = ask(chat)
    assert chat.pending_action('area-cost-O_P0226') is None
    decided = chat.decide(q['id'], option_id='defer', decided_by='manager@example.com', can_decide=True)
    assert decided['decision']['action'] == 'defer_to_merge'
    again = chat.decide(q['id'], option_id='defer', decided_by='manager@example.com', can_decide=True)
    assert again['decision']['id'] == decided['decision']['id']
    assert len([m for m in chat.messages() if m['kind'] == 'decision']) == 1, 'a resend must not create a second record'
    with pytest.raises(ChatError, match='question_already_decided'):
        chat.decide(q['id'], option_id='retry', decided_by='manager@example.com', can_decide=True)


def test_decision_checks_permission_option_ownership_and_shape(tmp_path):
    chat = Chat(tmp_path)
    q = ask(chat)
    with pytest.raises(ChatError, match='decision_not_permitted'):
        chat.decide(q['id'], option_id='defer', decided_by='viewer@example.com', can_decide=False)
    with pytest.raises(ChatError, match='option_not_in_question'):
        chat.decide(q['id'], option_id='made-up', decided_by='m', can_decide=True)
    with pytest.raises(ChatError, match='decision_one_of_option_or_text'):
        chat.decide(q['id'], option_id='defer', free_text='둘 다', decided_by='m', can_decide=True)
    with pytest.raises(ChatError, match='question_not_found'):
        chat.decide('q-unknown', option_id='defer', decided_by='m', can_decide=True)
    assert chat.question(q['id'])['status'] == 'open', 'a rejected decision must leave the question open'


def test_free_text_is_an_instruction_not_an_action(tmp_path):
    chat = Chat(tmp_path)
    q = ask(chat)
    decided = chat.decide(q['id'], free_text='  이 이슈는 비용 영역에서 다시 작성해 주세요.  ', decided_by='m', can_decide=True)
    assert decided['decision']['action'] is None and decided['decision']['free_text'].startswith('이 이슈')


def test_decision_is_consumed_once_and_chat_closes_only_without_open_questions(tmp_path):
    chat = Chat(tmp_path)
    q = ask(chat)
    with pytest.raises(ChatError, match='open_questions_remain'):
        chat.close('analysis_completed')
    chat.decide(q['id'], option_id='retry', decided_by='m', can_decide=True)
    assert chat.pending_action('area-cost-O_P0226')['id'] == q['id']
    chat.consume(q['id'])
    assert chat.pending_action('area-cost-O_P0226') is None, 'an enforced decision must not be applied twice'
    chat.close('analysis_completed')
    assert chat.is_closed()
    with pytest.raises(ChatError, match='chat_closed'):
        chat.post(sender='analysis_owner', text='닫힌 뒤의 메시지')
    assert chat.messages(), 'history must survive closing'


def test_concurrent_decisions_record_exactly_one(tmp_path):
    chat = Chat(tmp_path)
    q = ask(chat)
    results = []

    def decide(option):
        try:
            results.append(chat.decide(q['id'], option_id=option, decided_by='m', can_decide=True)['decision']['option_id'])
        except ChatError as exc:
            results.append(str(exc))
    threads = [threading.Thread(target=decide, args=('defer' if n % 2 else 'retry',)) for n in range(8)]
    [t.start() for t in threads]; [t.join() for t in threads]
    final = Chat(tmp_path).question(q['id'])['decision']['option_id']
    assert results.count('question_already_decided') + results.count(final) == 8
    assert len([m for m in chat.messages() if m['kind'] == 'decision']) == 1
