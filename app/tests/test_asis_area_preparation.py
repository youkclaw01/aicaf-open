"""영역 분석도 신규 작업의 입력 복제부터 자원 제어 안에 들어가야 한다."""
import asyncio
from copy import deepcopy
from pathlib import Path
import sys
from types import SimpleNamespace
import weakref
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import asis_analysis
from asis_admission import AdaptiveAdmission
from asis_pipeline import Pipeline
from test_asis_connection import synthetic_input


def test_area_preparation_is_inside_default_resource_admission(tmp_path,monkeypatch):
    data=synthetic_input(tmp_path)
    baseline=asis_analysis.analyze(data)
    sample={'total_memory_bytes':10000,'available_memory_bytes':3200,'cpu_percent':5,
        'logical_cpu_count':4,'worker_tree_rss_bytes':1000,'sample_valid':True}
    gate=AdaptiveAdmission(sampler=lambda:dict(sample),interval=.001)
    pipe=Pipeline(SimpleNamespace(gate=gate),tmp_path/'run')
    original=pipe.area_input
    live=weakref.WeakValueDictionary();peak=[]
    class Scope(dict):pass
    def measured(*args):
        value=Scope(original(*args));live[id(value)]=value;peak.append(len(live))
        return value
    monkeypatch.setattr(pipe,'area_input',measured)
    async def stage(scoped,number,key):
        await asyncio.sleep(.01)
        if number==1:
            area=next(row for row in baseline['stages'][0]['areas'] if row['area_id']==key[5:])
            return {'stage':'area','run':deepcopy(data['run']),'areas':[deepcopy(area)],'unresolved':[]}
        return deepcopy(baseline['stages'][number-1])
    async def synthesis(scoped):return deepcopy(baseline['stages'][1])
    async def actions(scoped):return scoped,deepcopy(baseline['stages'][2])
    monkeypatch.setattr(pipe,'reviewed_stage',stage)
    monkeypatch.setattr(pipe,'reviewed_synthesis',synthesis)
    monkeypatch.setattr(pipe,'reviewed_actions',actions)
    returned,stages=asyncio.run(pipe.compose_stages(baseline['input'],baseline,data))
    assert len(stages[0]['areas'])==len(baseline['stages'][0]['areas'])
    assert max(peak)==1,'queued areas must not retain their own full evidence copies before admission'
    assert not live and gate.active==gate.waiting==0
