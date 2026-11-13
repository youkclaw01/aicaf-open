"""대량 모델 입력의 전송 인코딩이 동일 원문과 원장 소유권을 보존하는지 검증한다."""
from copy import deepcopy
from pathlib import Path
import asyncio
import hashlib
import json
import sys
import types

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import asis_runtime


def test_runtime_shares_only_identical_fact_run_in_the_transmitted_message():
    """반복 run만 명시 참조로 전송하고 값·ID·수집 상태·다른 run은 유지한다."""
    run = {'assessment_id': 999999, 'case_id': 'SYNTHETIC_TRANSPORT_ONLY',
           'analysis_revision': 'unit-run-' + 'a' * 80, 'source_bundle_sha256': 'b' * 64}
    data = {'run': run, 'facts': [
        {'id': 'F_1', 'run': deepcopy(run), 'value': [{'setting': False, 'unset': None}],
         'collection_status': 'collected', 'scope_complete': False},
        {'id': 'F_2', 'run': deepcopy(run), 'value': [],
         'collection_status': 'not_present', 'scope_complete': True},
        {'id': 'F_OTHER', 'run': {**run, 'analysis_revision': 'another-unit-revision'},
         'value': '다른 실행의 원문은 같은 실행으로 바꾸지 않는다.',
         'collection_status': 'collected', 'scope_complete': False}],
        'context': {'requirements': '수치·상태·원문을 유지한다.'},
        'stage_data': {'mode': 'issue_plan'}}
    before = deepcopy(data)
    message = '단위 시험 전송 지시\n입력:\n' + json.dumps(data, ensure_ascii=False)
    observed = {}

    class Client:
        async def start(self): pass
        async def stop(self): pass

    class Agent:
        def __init__(self, **kwargs): observed['options'] = kwargs['default_options']
        async def run(self, messages, *, session, options=None):
            observed['message'] = messages.contents[0].text
            return types.SimpleNamespace(text='{"unit_only":true}', response_id='unit-transport',
                additional_properties={'model': 'gpt-6.1-sol'}, usage_details={})

    async def exercise():
        async with asis_runtime.Runtime(client=Client(), agent_factory=Agent) as runtime:
            return await runtime.ask('interpret', message, session_id='unit-compact-transport')

    _, receipt = asyncio.run(exercise())
    transmitted = observed['message']
    prefix, body = transmitted.rsplit('\n입력:\n', 1)
    encoded = json.loads(body)
    assert prefix == '단위 시험 전송 지시'
    assert encoded['facts'][0]['run'] == {'$ref': '/run'}
    assert encoded['facts'][1]['run'] == {'$ref': '/run'}
    assert encoded['facts'][2]['run'] == before['facts'][2]['run']
    declaration = encoded['context'].pop('shared_fact_run_reference')
    assert declaration['marker'] == {'$ref': '/run'} and declaration['target'] == '/run'
    for fact in encoded['facts']:
        if fact['run'] == declaration['marker']:
            fact['run'] = deepcopy(encoded['run'])
    assert encoded == before and data == before
    assert len(transmitted.encode('utf-8')) < len(message.encode('utf-8'))
    assert receipt['input_sha256'] == hashlib.sha256(transmitted.encode('utf-8')).hexdigest()
    assert receipt['canonical_input_sha256'] == hashlib.sha256(message.encode('utf-8')).hexdigest()
    assert observed['options']['reasoning_effort'] == 'xhigh'


def test_transport_message_preserves_non_json_and_noncanonical_inputs():
    """일반 텍스트·잘못된 JSON·이미 선언된 참조·다른 실행 값은 자동 재해석하지 않는다."""
    for message in ('plain synthetic input', 'prefix\n입력:\n{broken',
                    'prefix\n입력:\n[1,2]', 'prefix\n입력:\n{"run":{},"facts":[]}'):
        assert asis_runtime.transport_message(message) == message
    payload = {'run': {'id': 'current'}, 'facts': [{'id': 'F_1', 'run': {'id': 'other'}}],
               'context': {}}
    message = 'prefix\n입력:\n' + json.dumps(payload)
    assert asis_runtime.transport_message(message) == message
    payload['facts'][0]['run'] = payload['run']
    payload['context']['shared_fact_run_reference'] = {'existing': 'unchanged'}
    message = 'prefix\n입력:\n' + json.dumps(payload)
    assert asis_runtime.transport_message(message) == message


def test_transport_message_is_idempotent_and_preserves_full_large_values():
    """압축은 재수집·요약·절단이 아니며 큰 값과 선언 ID를 바꾸지 않는다."""
    run = {'analysis_revision': 'r-' + 'a' * 120, 'source_bundle_sha256': 'b' * 64}
    data = {'run': run, 'facts': [{'id': 'F_' + str(i), 'run': deepcopy(run),
        'value': [{'index': n, 'text': '합성 원문', 'enabled': False, 'missing': None} for n in range(100)],
        'collection_status': 'collected', 'scope_complete': False} for i in range(4)], 'context': {}}
    message = 'prefix\n입력:\n' + json.dumps(data, ensure_ascii=False)
    compact = asis_runtime.transport_message(message)
    assert asis_runtime.transport_message(compact) == compact
    result = json.loads(compact.rsplit('\n입력:\n', 1)[-1])
    for old, new in zip(data['facts'], result['facts']):
        assert old['value'] == new['value']
        assert old['id'] == new['id']
        assert old['scope_complete'] is new['scope_complete']
        assert new['run'] == {'$ref': '/run'}
