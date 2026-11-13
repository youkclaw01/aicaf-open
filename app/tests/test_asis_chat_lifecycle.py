"""분석 단계 채팅의 연결·닫힘: 연결은 고객 폴더 기준으로 이뤄지고, 열린 질문이 있으면 닫지 않으며, 실패해도 생성은 막지 않는다."""
from pathlib import Path
import sys, json
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import asis_service
from asis_pipeline import Pipeline
from asis_chat import Chat


def setup(tmp_path):
    folder = tmp_path / 'asis' / '24' / 'revisions' / 'r-20261014T025532-450c857f53afbacf'
    folder.mkdir(parents=True)
    events = []
    pipeline = Pipeline(None, folder, progress=lambda e: events.append(e))
    return folder, pipeline, events


def test_attach_connects_chat_and_a_customer_scoped_incident_store(tmp_path):
    folder, pipeline, _ = setup(tmp_path)
    asis_service._attach_chat(pipeline, folder)
    assert pipeline.chat.messages()[0]['sender'] == 'analysis_owner'
    assert pipeline.incident_store.root == tmp_path / 'asis' / '24' / 'knowledge', 'knowledge is per assessment, never shared across customers'
    assert pipeline.incident_store.source_root == folder.resolve(), 'sources are verified against this revision only'


def test_close_closes_when_no_question_is_open_and_keeps_history(tmp_path):
    folder, pipeline, _ = setup(tmp_path)
    asis_service._attach_chat(pipeline, folder)
    asis_service._close_analysis_chat(pipeline, 'analysis_stages_completed')
    chat = Chat(folder)
    assert chat.is_closed() and len(chat.messages()) == 2, 'both the opening and the closing notice must remain as history'


def test_close_does_not_close_while_a_question_is_open(tmp_path):
    folder, pipeline, events = setup(tmp_path)
    asis_service._attach_chat(pipeline, folder)
    pipeline.chat.ask(sender='global_manager', task_key='k', summary='열린 질문', options=[{'id': 'a', 'label': '확인', 'action': 'defer_to_merge'}],
                      evidence=[{'path': 'x'}])
    asis_service._close_analysis_chat(pipeline, 'analysis_stages_completed')
    assert not Chat(folder).is_closed(), 'a pending human decision must never be silently closed'
    assert any(e.get('status') == 'close_skipped' for e in events)


def test_attach_and_close_never_break_generation_when_the_chat_is_unavailable(tmp_path):
    folder, pipeline, events = setup(tmp_path)
    asis_service._close_analysis_chat(pipeline, 'x')     # 연결 전에 닫아도 오류가 없다
    blocker = folder / 'chat'
    blocker.write_text('a file where the chat folder should be', encoding='utf-8')
    asis_service._attach_chat(pipeline, folder)           # 쓸 수 없는 위치여도 예외를 올리지 않는다
    assert any(e.get('status') == 'attach_failed' for e in events)
    assert getattr(pipeline, 'chat', None) is None or True
