"""현재·과거 후보의 독립 검토 근거는 다른 호출과 동일 경로로 덮어쓰지 않는다."""
from copy import deepcopy
from pathlib import Path
import asyncio
import hashlib
import json
import sys

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import asis_analysis
from asis_pipeline import Pipeline


def test_multiple_review_invocations_keep_historical_receipts_and_original_candidate(tmp_path):
    data,draft=asis_analysis.contract().synthetic_fixture()
    class ReviewRuntime:
        count=0
        async def ask(self,role,message,**kwargs):
            self.count+=1
            result={'stage':'review','run':data['run'],'verdict':'pass','content_checked':True,
                    'render_checked':False,'defects':[],'unresolved':[]}
            return json.dumps(result,ensure_ascii=False),{'test_only':True,'response_id':str(self.count)}
    pipe=Pipeline(ReviewRuntime(),tmp_path)
    async def run():
        await pipe.independent_review(data,draft,'repeated-review')
        current=deepcopy(pipe.tasks.ledger['tasks']['repeated-review']['reports']['reviewer'])
        changed=deepcopy(draft);changed['areas'][0]['headline']='변경한 합성 후보'
        await pipe.independent_review(data,changed,'repeated-review')
        return current
    old=asyncio.run(run())
    assert all(hashlib.sha256((tmp_path/file['path']).read_bytes()).hexdigest()==file['sha256'] for file in old['evidence']), 'previous review evidence overwritten'
    latest=pipe.tasks.ledger['tasks']['repeated-review']['reports']['reviewer']
    assert {file['path'] for file in old['evidence']}.isdisjoint({file['path'] for file in latest['evidence']})
    assert pipe.runtime.count==2


def test_identical_candidate_rechecked_after_revision_keeps_first_receipt(tmp_path):
    data,draft=asis_analysis.contract().synthetic_fixture()
    class Reviewer:
        count=0
        async def ask(self,role,message,**kwargs):
            self.count+=1
            defect={'where':'/areas/0/headline','quote':draft['areas'][0]['headline'],
                    'why':'합성 검토 범위 표현 보완','fix':'동일 범위를 명확히 표현','evidence_refs':[]}
            value={'stage':'review','run':data['run'],'verdict':'revise' if self.count==1 else 'pass',
                'content_checked':True,'render_checked':False,'defects':[defect] if self.count==1 else [],'unresolved':[]}
            return json.dumps(value,ensure_ascii=False),{'test_only':True,'response_id':str(self.count)}
    engine=Pipeline(Reviewer(),tmp_path)
    async def run():
        await engine.independent_review(data,draft,'same-review')
        first=deepcopy(engine.tasks.ledger['tasks']['same-review']['reports']['reviewer'])
        await engine.independent_review(data,draft,'same-review')
        return first
    first=asyncio.run(run())
    assert all(hashlib.sha256((tmp_path/file['path']).read_bytes()).hexdigest()==file['sha256'] for file in first['evidence']), 'same binding overwrote earlier invocation'


def test_cached_review_evidence_remains_immutable_after_new_candidate(tmp_path):
    data,draft=asis_analysis.contract().synthetic_fixture()
    class Reviewer:
        count=0
        async def ask(self,role,message,**kwargs):
            self.count+=1
            value={'stage':'review','run':data['run'],'verdict':'pass',
                'content_checked':True,'render_checked':False,'defects':[],'unresolved':[]}
            return json.dumps(value,ensure_ascii=False),{'test_only':True,'response_id':str(self.count)}
    engine=Pipeline(Reviewer(),tmp_path)
    async def run():
        await engine.independent_review(data,draft,'cache-review')
        await engine.independent_review(data,draft,'cache-review')
        cached=deepcopy(engine.tasks.ledger['tasks']['cache-review']['reports']['reviewer'])
        changed=deepcopy(draft);changed['areas'][0]['headline']='다른 합성 후보'
        await engine.independent_review(data,changed,'cache-review')
        return cached
    cached=asyncio.run(run())
    assert all(hashlib.sha256((tmp_path/file['path']).read_bytes()).hexdigest()==file['sha256'] for file in cached['evidence']), 'cached alias overwritten'
    assert engine.runtime.count==2


def test_cached_pair_reports_keep_immutable_checkpoint_before_candidate_changes(tmp_path):
    data,draft=asis_analysis.contract().synthetic_fixture()
    class Runtime:
        def __init__(self):
            self.count=0
            self.draft=deepcopy(draft)
        async def ask(self,role,message,**kwargs):
            self.count+=1
            result=self.draft if role in ('author', 'direction_author') else {'stage':'review','run':data['run'],'verdict':'pass',
                'content_checked':True,'render_checked':False,'defects':[],'unresolved':[]}
            return json.dumps(result,ensure_ascii=False),{'test_only':True,'response_id':str(self.count)}
    runtime=Runtime();engine=Pipeline(runtime,tmp_path)
    async def run():
        await engine.reviewed_stage(data,1,'immutable-pair')
        await engine.reviewed_stage(data,1,'immutable-pair')
        old=deepcopy(engine.tasks.ledger['tasks']['immutable-pair']['reports'])
        changed=deepcopy(data);changed['stage_data']['revision_scope']='changed'
        runtime.draft['areas'][0]['headline']='변경된 합성 후보'
        await engine.reviewed_stage(changed,1,'immutable-pair')
        return old
    old=asyncio.run(run())
    assert all(hashlib.sha256((tmp_path/file['path']).read_bytes()).hexdigest()==file['sha256']
               for report in old.values() for file in report['evidence']), 'cached pair alias overwritten'


def test_same_input_failed_pair_invocations_do_not_overwrite_prior_author_receipt(tmp_path):
    data,draft=asis_analysis.contract().synthetic_fixture()
    class Runtime:
        count=0
        async def ask(self,role,message,**kwargs):
            self.count+=1
            defect={'where':'/areas/0/headline','quote':draft['areas'][0]['headline'],
                    'why':'실제 합성 지적','fix':'합성 범위를 명시','evidence_refs':[]}
            result=draft if role in ('author', 'direction_author') else {'stage':'review','run':data['run'],'verdict':'revise',
                'content_checked':True,'render_checked':False,'defects':[defect],'unresolved':[]}
            return json.dumps(result,ensure_ascii=False),{'test_only':True,'response_id':str(self.count)}
    import pytest
    engine=Pipeline(Runtime(),tmp_path,rounds=1)
    old=[]
    async def run():
        for index in range(3):
            with pytest.raises(RuntimeError,match='revision_budget_exhausted'):
                await engine.reviewed_stage(data,1,'same-input-pair')
            if index==1:
                old.extend(deepcopy(engine.tasks.ledger['tasks']['same-input-pair']['reports']['author']['evidence']))
    asyncio.run(run())
    assert all(hashlib.sha256((tmp_path/file['path']).read_bytes()).hexdigest()==file['sha256'] for file in old), 'same input author evidence overwritten'
