"""사람 에스컬레이션: 두 단계 관리자가 모두 upheld면 채팅에 질문하고, 사람의 결정이 허용된 조치로 집행되며, 사건이 출처와 함께 기록된다."""
from pathlib import Path
import asyncio, json, sys, threading
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import pytest
from asis_pipeline import Pipeline
from asis_chat import Chat
from asis_incidents import Store
from test_asis_arbiter_pipeline_flow import BlockingReviewer


def build(tmp_path, *, poll=0.02):
    from asis_item_agents import allocate_plan, item_input
    from test_asis_item_agents import metadata_plan, item_fixture
    data = allocate_plan(item_fixture(), metadata_plan(), 'synthetic-plan')
    scoped = item_input(data, 'diagnosis', data['context']['action_item_plan']['diagnoses'][0])
    runtime = BlockingReviewer('upheld')
    pipe = Pipeline(runtime, tmp_path / 'run')
    pipe.chat = Chat(tmp_path / 'run')
    pipe.incident_store = Store(tmp_path / 'customer', source_root=tmp_path / 'run')
    pipe.HUMAN_POLL_SECONDS = poll
    return runtime, pipe, scoped


def answer_when_asked(chat, **decision):
    def worker():
        for _ in range(500):
            questions = chat.open_questions()
            if questions:
                chat.decide(questions[0]['id'], decided_by='fixture-manager', can_decide=True, **decision)
                return
            threading.Event().wait(0.02)
    thread = threading.Thread(target=worker)
    thread.start()
    return thread


def test_unresolved_blocked_asks_the_human_then_retry_decision_returns_to_the_author(tmp_path):
    runtime, pipe, scoped = build(tmp_path)
    thread = answer_when_asked(pipe.chat, option_id='retry')
    candidate = asyncio.run(pipe.reviewed_stage(scoped, 3, 'diagnosis-J_ITEM_0001'))
    thread.join()
    assert candidate
    assert len([c for c in runtime.calls if c['role'] in ('author', 'direction_author')]) == 2, 'the human chose retry: writer gets one more attempt'
    decided = [q for q in (tmp_path / 'run' / 'chat' / 'questions').glob('*.json')]
    record = json.loads(decided[0].read_text(encoding='utf-8'))
    assert record['status'] == 'decided' and record['decision']['consumed'], 'the decision must be enforced exactly once'
    assert record['sender'] == 'global_manager' and record['evidence'], 'the question must cite real evidence files'
    incidents = list((tmp_path / 'customer' / 'incidents').glob('*.json'))
    assert incidents, 'a human-resolved block must be recorded as an incident'
    incident = json.loads(incidents[0].read_text(encoding='utf-8'))
    assert incident['resolution'] == 'resolved_by_human' and any('decision_id' in s for s in incident['sources'])


def test_free_text_instruction_reaches_the_author_as_a_defect(tmp_path):
    runtime, pipe, scoped = build(tmp_path)
    thread = answer_when_asked(pipe.chat, free_text='이 항목은 확인 불가로 명시하고 단정하지 마세요.')
    asyncio.run(pipe.reviewed_stage(scoped, 3, 'diagnosis-J_ITEM_0001'))
    thread.join()
    authors = [c for c in runtime.calls if c['role'] in ('author', 'direction_author')]
    assert len(authors) == 2
    assert '확인 불가로 명시하고 단정하지 마세요' in json.dumps(authors[1]['input'], ensure_ascii=False)


def test_stop_decision_stops_the_revision_and_is_not_swallowed(tmp_path):
    runtime, pipe, scoped = build(tmp_path)
    thread = answer_when_asked(pipe.chat, option_id='stop')
    with pytest.raises(RuntimeError, match='stopped_by_human_decision'):
        asyncio.run(pipe.reviewed_stage(scoped, 3, 'diagnosis-J_ITEM_0001'))
    thread.join()


def test_without_a_chat_the_old_failure_behaviour_is_unchanged(tmp_path):
    from asis_item_agents import allocate_plan, item_input
    from test_asis_item_agents import metadata_plan, item_fixture
    data = allocate_plan(item_fixture(), metadata_plan(), 'synthetic-plan')
    scoped = item_input(data, 'diagnosis', data['context']['action_item_plan']['diagnoses'][0])
    pipe = Pipeline(BlockingReviewer('upheld'), tmp_path / 'run')
    with pytest.raises(RuntimeError, match='independent_review_blocked'):
        asyncio.run(pipe.reviewed_stage(scoped, 3, 'diagnosis-J_ITEM_0001'))


def test_wait_expiry_falls_back_to_the_failure_instead_of_hanging(tmp_path):
    runtime, pipe, scoped = build(tmp_path)
    pipe.HUMAN_WAIT_SECONDS = 0.1
    with pytest.raises(RuntimeError, match='independent_review_blocked'):
        asyncio.run(pipe.reviewed_stage(scoped, 3, 'diagnosis-J_ITEM_0001'))
    assert Chat(tmp_path / 'run').open_questions(), 'the question stays open so a human can still see it'
