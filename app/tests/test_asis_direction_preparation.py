"""종합 방향도 SDK 요청보다 앞선 입력 복제 단계부터 소단위 자원 제어를 따른다."""
import asyncio
from pathlib import Path
import sys
import weakref
import asis_synthesis
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from asis_admission import AdaptiveAdmission
from asis_pipeline import Pipeline
from test_asis_parallel_synthesis import synthesis_fixture,DirectionRuntime


def test_direction_preparation_is_inside_default_resource_admission(tmp_path,monkeypatch):
    data,plan=synthesis_fixture(tmp_path)
    runtime=DirectionRuntime(plan)
    sample={'total_memory_bytes':10000,'available_memory_bytes':3200,'cpu_percent':5,
      'logical_cpu_count':4,'worker_tree_rss_bytes':1000,'sample_valid':True}
    runtime.gate=AdaptiveAdmission(sampler=lambda:dict(sample),interval=.001)
    pipe=Pipeline(runtime,tmp_path/'run')
    original=asis_synthesis.direction_input
    live=weakref.WeakValueDictionary();peak=[]
    class Scope(dict):pass
    def measured(*args):
        value=Scope(original(*args));live[id(value)]=value;peak.append(len(live))
        return value
    monkeypatch.setattr(asis_synthesis,'direction_input',measured)
    result=asyncio.run(pipe.reviewed_synthesis(data))
    assert len(result['directions'])==2
    assert max(peak)==1,'queued directions must not retain their own full evidence copies before admission'
    assert not live and runtime.gate.active==runtime.gate.waiting==0
