"""신규·재개 분석 모두 실행 슬롯 밖의 전체 입력 복제·상주를 금지한다."""
import asyncio
from pathlib import Path
import sys
import weakref

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import asis_item_agents
from asis_pipeline import Pipeline
from test_asis_item_agents import ItemRuntime,item_fixture


def test_item_input_lifetime_is_bounded_by_unit_slots(tmp_path,monkeypatch):
    """실제 계획·항목 경로·결정 검사를 사용하고 외부 모델 전송만 합성한다."""
    original=asis_item_agents.item_input
    live=weakref.WeakValueDictionary()
    peak=[]
    class ScopedInput(dict):
        pass
    def measured_input(*args,**kwargs):
        value=ScopedInput(original(*args,**kwargs))
        live[id(value)]=value
        peak.append(len(live))
        return value
    monkeypatch.setattr(asis_item_agents,'item_input',measured_input)
    runtime=ItemRuntime()
    pipe=Pipeline(runtime,tmp_path/'run',author_workers=2)
    data,result=asyncio.run(pipe.reviewed_actions(item_fixture()))
    assert len(result['diagnoses'])==2 and len(result['actions'])==3
    assert max(peak)<=2,'full item inputs must not be constructed or retained beyond active unit slots'
    assert runtime.max_active==2,'independent units still execute in parallel within the bound'
    assert len(list((tmp_path/'run'/'checkpoints').rglob('accepted.json')))>=6
    assert not live,'completed units must release their full evidence copies'
