"""실제 네이티브 생성 경로의 픽셀 작업은 실행 중 소단위의 자원 예약 밖에서 시작하지 않는다."""
import asyncio
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import asis_service
from asis_artifacts import build
from asis_admission import AdaptiveAdmission
from test_asis_connection import SyntheticRuntime,synthetic_input


def test_native_pixel_preparation_follows_work_unit_admission(tmp_path,monkeypatch):
    instances=[]
    class Runtime(SyntheticRuntime):
        def __init__(self,**kwargs):
            super().__init__()
            sample={'total_memory_bytes':10000000000,'available_memory_bytes':8000000000,'cpu_percent':5,
                'logical_cpu_count':8,'worker_tree_rss_bytes':1000000000,'sample_valid':True}
            self.gate=AdaptiveAdmission(sampler=lambda:dict(sample),interval=.001)
            instances.append(self)
        async def __aenter__(self):return self
        async def __aexit__(self,*args):return None
    original=asis_service._page_batches
    counts=[]
    def guarded(*args,**kwargs):
        assert instances and instances[0].gate.active>0,'pixel contacts must not be prepared before resource admission'
        counts.append(1)
        yield from original(*args,**kwargs)
    monkeypatch.setattr(asis_service,'_page_batches',guarded)
    data=synthetic_input(tmp_path)
    result,manifest=asyncio.run(asis_service.execute(data,tmp_path/'run',builder=build,runtime_factory=Runtime))
    assert all(row['verdict']=='pass' for row in manifest['quality_review']['item_reviews'])
    assert counts and instances[0].gate.active==instances[0].gate.waiting==0
