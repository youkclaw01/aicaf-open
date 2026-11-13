"""저장 문서 스킬의 실제 본문 전송을 증명하고 기존 승인 검토의 원문 바인딩은 유지한다."""
import asyncio
import hashlib
import json
from pathlib import Path
import sys
import types
import pytest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from asis_runtime import Runtime,transport_message
from asis_admission import AdaptiveAdmission


def material_fixture(folder):
    text='SAVED_DOCUMENT_SKILL_ACTUAL_BODY: 근거·판정·가정을 분리한다.'
    value={'schema':'asis-document-materials/1',
        'source_type':'stored_skills_explicitly_approved_for_this_final_report',
        'template_content_is_not_customer_evidence':True,
        'precedence':['latest_user_scope','frozen_approved_template_and_report','shared_document_rules','stored_skill_guidance'],
        'files':[{'name':'SKILL.md','source_path':'stored/approved/SKILL.md',
            'sha256':hashlib.sha256(text.encode('utf-8')).hexdigest(),
            'utf8_bytes':len(text.encode('utf-8')),'text':text}]}
    raw=json.dumps(value,ensure_ascii=False,sort_keys=True).encode('utf-8')
    (folder/'document-materials.json').write_bytes(raw)
    digest=hashlib.sha256(raw).hexdigest()
    (folder/'document-materials-binding.json').write_text(json.dumps({
        'schema':'asis-document-materials-binding/1','materials_sha256':digest,
        'classification':'procedural_guidance_not_customer_evidence'}),encoding='utf-8')
    return text,digest


class Client:
    async def start(self):pass
    async def stop(self):pass


def resources():
    return {'sample_valid':True,'total_memory_bytes':1000000,'available_memory_bytes':900000,
        'worker_tree_rss_bytes':1000,'cpu_percent':5,'logical_cpu_count':2}


def test_pending_document_roles_receive_actual_saved_skill_text(tmp_path):
    text,digest=material_fixture(tmp_path);messages=[]
    class Agent:
        def __init__(self,**kwargs):pass
        async def run(self,message,*,session,options=None):
            messages.append(message.text)
            return types.SimpleNamespace(text='{"ok":true}',response_id='synthetic-material-check',
                additional_properties={'model':'gpt-6.1-sol'},finish_reason='stop',usage_details={})
    async def exercise():
        async with Runtime(client=Client(),agent_factory=Agent,base_directory=tmp_path/'sessions',
            admission=AdaptiveAdmission(sampler=resources,interval=.001)) as runtime:
            for role in ('author','content_review','visual_review'):
                _,receipt=await runtime.ask(role,'customer-candidate-bound-message',session_id='new-'+role,
                    instructions='unchanged-role-instructions')
                assert text in messages[-1]
                assert receipt['document_materials_applied'] is True
                assert receipt['document_materials_sha256']==digest
                assert receipt['instructions_sha256']==hashlib.sha256(b'unchanged-role-instructions').hexdigest()
                assert 'customer-candidate-bound-message' in messages[-1]
    asyncio.run(exercise())
    assert len(messages)==3


def test_protected_original_review_keeps_exact_message_and_native_review_receives_materials(tmp_path):
    text,digest=material_fixture(tmp_path);messages=[];prompt='canonical-approved-review';instructions='fixed-review-instructions'
    request={'schema':'asis-unstarted-review-continuation/1','approval_id':'original-once',
        'target_session_id':'protected-review','target_author_session_id':'protected-author','review_call_budget':1,
        'candidate_sha256':'c'*64,'canonical_input_sha256':hashlib.sha256(prompt.encode('utf-8')).hexdigest(),
        'instructions_sha256':hashlib.sha256(instructions.encode('utf-8')).hexdigest()}
    (tmp_path/'unstarted-review-continuation.json').write_text(json.dumps(request),encoding='utf-8')
    class Agent:
        def __init__(self,**kwargs):pass
        async def run(self,message,*,session,options=None):
            messages.append(message.text);session.service_session_id='synthetic-service-session'
            return types.SimpleNamespace(text='{"ok":true}',response_id='synthetic-protected-check',
                additional_properties={'model':'gpt-6.1-sol'},finish_reason='stop',usage_details={})
    async def exercise():
        async with Runtime(client=Client(),agent_factory=Agent,base_directory=tmp_path/'sessions',
            admission=AdaptiveAdmission(sampler=resources,interval=.001)) as runtime:
            _,original=await runtime.ask('content_review',prompt,session_id='protected-review',instructions=instructions)
            assert messages[-1]==transport_message(prompt) and text not in messages[-1]
            assert original['document_materials_applied'] is False
            claim_before=(tmp_path/'unstarted-review-call-once.json').read_bytes()
            _,native=await runtime.ask('content_review','new-native-page-review',session_id='protected-review',
                instructions=instructions,image=b'synthetic-image-only-for-transport-test')
            assert text in messages[-1] and native['document_materials_applied'] is True
            assert native['document_materials_sha256']==digest and native['agent_reused'] is True
            assert (tmp_path/'unstarted-review-call-once.json').read_bytes()==claim_before
    asyncio.run(exercise())
    assert len(messages)==2


def test_material_change_during_runtime_fails_before_provider(tmp_path):
    material_fixture(tmp_path);calls=[]
    class Agent:
        def __init__(self,**kwargs):pass
        async def run(self,*args,**kwargs):calls.append(True);raise AssertionError('must not send changed materials')
    async def exercise():
        async with Runtime(client=Client(),agent_factory=Agent,base_directory=tmp_path/'sessions',
            admission=AdaptiveAdmission(sampler=resources,interval=.001)) as runtime:
            path=tmp_path/'document-materials.json';path.write_bytes(path.read_bytes()+b'\n')
            with pytest.raises(ValueError,match='document_materials_changed_during_runtime'):
                await runtime.ask('author','unchanged-request',session_id='new-author')
    asyncio.run(exercise());assert not calls


def test_named_material_without_bound_actual_text_is_rejected(tmp_path):
    material_fixture(tmp_path);path=tmp_path/'document-materials.json'
    value=json.loads(path.read_text(encoding='utf-8'));value['files'][0]['text']=''
    raw=json.dumps(value,ensure_ascii=False).encode('utf-8');path.write_bytes(raw)
    binding=tmp_path/'document-materials-binding.json';value=json.loads(binding.read_text(encoding='utf-8'))
    value['materials_sha256']=hashlib.sha256(raw).hexdigest();binding.write_text(json.dumps(value),encoding='utf-8')
    runtime=Runtime(client=Client(),agent_factory=lambda **kwargs:None,base_directory=tmp_path/'sessions')
    with pytest.raises(ValueError,match='document_materials_actual_text_missing'):
        runtime.load_document_materials()
