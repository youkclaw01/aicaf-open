"""제미나이 실측의 표시 출력·총 출력 차이를 보존해 과금 사용량을 누락하지 않는다."""
from pathlib import Path
import sys
import pytest
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from asis_chat_transport import normalize_usage


def test_total_usage_keeps_non_display_output_in_the_accounted_output():
    raw = {'input_token_count': 46, 'output_token_count': 14, 'total_token_count': 191,
           'cache_read_input_token_count': 0}
    normalized = normalize_usage(raw)
    assert normalized['output_token_count'] == 145
    assert normalized['provider_reported_output_token_count'] == 14
    assert normalized['non_display_output_token_count'] == 131
    assert raw['output_token_count'] == 14


def test_output_already_including_reasoning_is_not_added_twice():
    normalized = normalize_usage({'input_token_count': 100, 'output_token_count': 25, 'total_token_count': 125})
    assert normalized['output_token_count'] == 25
    assert normalized['non_display_output_token_count'] == 0


@pytest.mark.parametrize('usage', [
    {'input_token_count': -1, 'output_token_count': 1, 'total_token_count': 0},
    {'input_token_count': 100, 'output_token_count': 25, 'total_token_count': 120},
    {'input_token_count': 1, 'output_token_count': 1, 'total_token_count': 2, 'cache_read_input_token_count': 3},
])
def test_inconsistent_provider_usage_is_rejected(usage):
    with pytest.raises(ValueError, match='invalid_chat_usage'):
        normalize_usage(usage)


def test_serialized_framework_history_is_bound_to_role_model_effort_and_instructions(tmp_path):
    from agent_framework import AgentSession
    from asis_chat_transport import ChatBackend
    policy = {'models': {'author': 'gemini-4-argon', 'content_review': 'gemini-4-argon'},
              'reasoning_by_role': {'author': 'high', 'content_review': 'high'}}
    first = ChatBackend(model_policy=policy, base_directory=tmp_path)
    session = AgentSession(session_id='bound-history')
    session.state['retained_text'] = '보존된 원문'
    first.save_session('author', session, '고정 지시')
    second = ChatBackend(model_policy=policy, base_directory=tmp_path)
    restored = second.restore_session('author', 'bound-history', '고정 지시')
    assert restored is not None
    loaded, turn = restored
    assert loaded.state['retained_text'] == '보존된 원문' and turn == 1
    assert loaded.service_session_id is None
    assert second.restore_session('content_review', 'bound-history', '고정 지시') is None
    with pytest.raises(RuntimeError, match='chat_session_restore_binding_changed'):
        second.restore_session('author', 'bound-history', '변경된 지시')


@pytest.mark.parametrize('endpoint', ['http://api.githubcopilot.com', 'https://api.githubcopilot.com.evil.example',
                                    'https://example.com', 'https://' + 'synthetic-user:synthetic-pass@api.githubcopilot.com',
                                    'https://api.githubcopilot.com/private', 'https://api.githubcopilot.com?token=secret'])
def test_provider_credentials_never_go_to_an_untrusted_endpoint(endpoint):
    from asis_chat_transport import _trusted_endpoint
    with pytest.raises(ValueError, match='untrusted_copilot_chat_endpoint'):
        _trusted_endpoint(endpoint)
