"""역할별 모델·추론 강도 정책 계약 시험.

방향 작성(direction_author)만 gpt-6-luna high 를 쓰고 나머지 역할은 gpt-6.1-sol xhigh 를 유지한다.
근거: 같은 10개 방향 입력으로 비교한 결과(작업표·코드 검사·가드를 붙인 luna high 가 현행 sol 과 같은 수준의 점수를 낸 반면 비용은 1/3 수준).
정책은 정책 지문·응답 모델 검증·세션 복구 검증·영수증에 같은 값으로 쓰여야 한다. 역할마다 값이 다르므로 단일 전역 상수를 가정한 곳이 남으면 안 된다.
"""
from pathlib import Path
import asyncio
import sys
import types

APP = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP))
import asis_runtime


def test_policy_declares_role_specific_models_and_efforts():
    policy = asis_runtime.policy()
    assert set(policy['models']) == {'interpret', 'author', 'content_review', 'visual_review', 'direction_author'}
    assert policy['models']['direction_author'] == 'gpt-6-luna'
    assert policy['reasoning_by_role']['direction_author'] == 'high'
    for role in ('interpret', 'author', 'content_review', 'visual_review'):
        assert policy['models'][role] == 'gpt-6.1-sol' and policy['reasoning_by_role'][role] == 'xhigh'
    assert policy['fallback_allowed'] is False


def test_model_and_effort_lookup_per_role():
    assert asis_runtime.model_for('direction_author') == 'gpt-6-luna'
    assert asis_runtime.effort_for('direction_author') == 'high'
    assert asis_runtime.model_for('content_review') == 'gpt-6.1-sol'
    assert asis_runtime.effort_for('content_review') == 'xhigh'


def test_unknown_role_is_rejected():
    for fn in (asis_runtime.model_for, asis_runtime.effort_for):
        try:
            fn('unknown_role')
        except ValueError:
            continue
        raise AssertionError('알 수 없는 역할은 기본값으로 대체하지 않고 거부해야 한다')


class Agent:
    seen = []

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        Agent.seen.append(kwargs)

    async def run(self, messages, *, session, options=None):
        model = self.kwargs['default_options']['model']
        return types.SimpleNamespace(text='{"ok":true}', response_id='r-' + session.session_id,
                                     additional_properties={'model': model}, finish_reason='stop', usage_details={})


class Client:
    async def start(self):
        pass

    async def stop(self):
        pass


def test_runtime_sends_role_model_and_effort_and_records_them_in_receipt():
    Agent.seen = []

    async def exercise():
        async with asis_runtime.Runtime(client=Client(), agent_factory=Agent, timeout=120) as runtime:
            _, luna = await runtime.ask('direction_author', 'payload', session_id='unit-dir-1')
            _, sol = await runtime.ask('content_review', 'payload', session_id='unit-rev-1')
            return luna, sol

    luna, sol = asyncio.run(exercise())
    by_session = {k['default_options']['model']: k['default_options'] for k in Agent.seen}
    assert by_session['gpt-6-luna']['reasoning_effort'] == 'high'
    assert by_session['gpt-6.1-sol']['reasoning_effort'] == 'xhigh'
    assert luna['requested_model'] == luna['actual_model'] == 'gpt-6-luna' and luna['requested_reasoning_effort'] == 'high'
    assert sol['requested_model'] == sol['actual_model'] == 'gpt-6.1-sol' and sol['requested_reasoning_effort'] == 'xhigh'


def test_response_from_unexpected_model_is_rejected_per_role():
    class WrongAgent(Agent):
        async def run(self, messages, *, session, options=None):
            return types.SimpleNamespace(text='{"ok":true}', response_id='r', additional_properties={'model': 'gpt-6.1-sol'},
                                         finish_reason='stop', usage_details={})

    async def exercise():
        async with asis_runtime.Runtime(client=Client(), agent_factory=WrongAgent, timeout=120) as runtime:
            await runtime.ask('direction_author', 'payload', session_id='unit-dir-wrong')

    try:
        asyncio.run(exercise())
    except RuntimeError as exc:
        assert 'unexpected_response_model' in str(exc)
        return
    raise AssertionError('luna 를 요청했는데 sol 응답이 와도 통과하면 모델 대체가 숨는다')


def _restore_record(role, model, effort, session_id):
    return {'role': role, 'session_id': session_id, 'service_session_id': 'svc', 'instructions_sha256': 'a' * 64,
            'requested_model': model, 'actual_model': model, 'requested_reasoning_effort': effort, 'fallback_used': False, 'session_turn': 1}


def _restore_rejected(record):
    runtime = asis_runtime.Runtime(client=Client(), agent_factory=Agent, timeout=120)
    try:
        runtime.restore_existing_sessions([record])
    except ValueError as exc:
        return 'existing_session_restore_binding_failed' in str(exc)
    return False


def test_session_restore_accepts_only_the_role_policy_model_and_effort():
    runtime = asis_runtime.Runtime(client=Client(), agent_factory=Agent, timeout=120)
    runtime.restore_existing_sessions([_restore_record('direction_author', 'gpt-6-luna', 'high', 'unit-dir-ok')])
    runtime.restore_existing_sessions([_restore_record('content_review', 'gpt-6.1-sol', 'xhigh', 'unit-rev-ok')])
    # 요청 모델만 다름 / 실제 응답 모델만 다름 / 강도만 다름: 각각 따로 거부해야 어느 검증이 빠져도 드러난다.
    # 요청 모델만 다르고 실제 응답 모델은 정책과 같은 기록: 두 검증이 중복 방어라서 한쪽만 빠져도 드러나도록 분리해 시험한다.
    assert _restore_rejected(dict(_restore_record('direction_author', 'gpt-6-luna', 'high', 'unit-bad-1'), requested_model='gpt-6.1-sol'))
    assert _restore_rejected(dict(_restore_record('direction_author', 'gpt-6-luna', 'high', 'unit-bad-2'), actual_model='gpt-6.1-sol'))
    assert _restore_rejected(_restore_record('direction_author', 'gpt-6-luna', 'xhigh', 'unit-bad-3'))
    assert _restore_rejected(_restore_record('content_review', 'gpt-6-luna', 'xhigh', 'unit-bad-4'))
    assert _restore_rejected(_restore_record('content_review', 'gpt-6.1-sol', 'high', 'unit-bad-5'))
    assert _restore_rejected(dict(_restore_record('direction_author', 'gpt-6-luna', 'high', 'unit-bad-6'), fallback_used=True))
