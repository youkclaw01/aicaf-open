"""Office가 없는 포털 인터프리터의 격리 호출 계약. 실제 Office 품질은 별도 Windows 실행으로 검증한다."""
from pathlib import Path
from types import SimpleNamespace
import hashlib,json,os,subprocess
import pytest
import asis_office


def test_office_bridge_uses_qualified_worker_and_does_not_forward_model_credentials(tmp_path,monkeypatch):
    bridge=getattr(asis_office,'convert_isolated',None)
    assert callable(bridge),'포털과 작업자 인터프리터의 Office 의존성 경계를 넘는 공통 호출기가 없다.'
    source=tmp_path/'source.xlsx';source.write_bytes(b'SYNTHETIC_UNIT_INPUT')
    worker=tmp_path/'qualified-python.exe';worker.write_bytes(b'SYNTHETIC_UNIT_NOT_EXECUTED')
    output=tmp_path/'output';expected=output/'source.xlsx';calls=[]
    monkeypatch.setattr(asis_office,'os',SimpleNamespace(name='nt',environ={**os.environ,
        'AICAF_ASIS_PYTHON':str(worker),'OPENAI_API_KEY':'SYNTHETIC_UNIT_NOT_A_SECRET','GITHUB_TOKEN':'SYNTHETIC_UNIT_NOT_A_TOKEN'}))
    def run(command,**kwargs):
        calls.append((command,kwargs));output.mkdir(parents=True,exist_ok=True);expected.write_bytes(b'SYNTHETIC_UNIT_OUTPUT')
        return subprocess.CompletedProcess(command,0,'NATIVE_OFFICE_RESULT='+json.dumps({'output':str(expected.resolve()),
            'sha256':hashlib.sha256(expected.read_bytes()).hexdigest()})+'\n','')
    monkeypatch.setattr(subprocess,'run',run)
    actual=bridge(source,output,'xlsx')
    assert actual==expected.resolve() and source.read_bytes()==b'SYNTHETIC_UNIT_INPUT'
    assert Path(calls[0][0][0])==worker
    assert not calls[0][1].get('shell',False)
    env=calls[0][1]['env'];assert 'OPENAI_API_KEY' not in env and 'GITHUB_TOKEN' not in env
    assert env['PYTHONIOENCODING']=='utf-8' and 'PYTHONUTF8' not in env


def test_office_bridge_rejects_a_child_output_outside_the_requested_location(tmp_path,monkeypatch):
    bridge=getattr(asis_office,'convert_isolated',None);assert callable(bridge)
    source=tmp_path/'source.xlsx';source.write_bytes(b'SYNTHETIC_UNIT_INPUT')
    worker=tmp_path/'worker.exe';worker.write_bytes(b'SYNTHETIC_UNIT_NOT_EXECUTED')
    monkeypatch.setattr(asis_office,'os',SimpleNamespace(name='nt',environ={'AICAF_ASIS_PYTHON':str(worker)}))
    monkeypatch.setattr(subprocess,'run',lambda command,**kw:subprocess.CompletedProcess(command,0,
        'NATIVE_OFFICE_RESULT='+json.dumps({'output':str(tmp_path/'not-requested.xlsx'),'sha256':'0'*64})+'\n',''))
    with pytest.raises(RuntimeError,match='output'):bridge(source,tmp_path/'output','xlsx')
