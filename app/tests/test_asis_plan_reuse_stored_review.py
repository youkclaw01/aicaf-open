"""보존된 영역 계획이 입력 바인딩 변화로 모델 재검토를 받아 실패하지 않고, 저장된 통과 검토를 재사용한다."""
import asyncio
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import asis_analysis
from asis_pipeline import Pipeline
from test_asis_connection import SyntheticRuntime,synthetic_input


def test_reuse_stored_review_makes_no_model_call(tmp_path):
    baseline=asis_analysis.analyze(synthetic_input(tmp_path));data=baseline['input'];draft={'stages':baseline['stages']}
    class NoCall(SyntheticRuntime):
        async def ask(self,*a,**k):raise AssertionError('model_called')
    engine=Pipeline(NoCall(),tmp_path/'run')
    review={'stage':'review','run':data['run'],'verdict':'pass','content_checked':True,'render_checked':False,'defects':[],'unresolved':[]}
    assert asyncio.run(engine.reuse_stored_review(data,draft,'unit-review',review))==review
    row=engine.task_manager(data).ledger['tasks']['unit-review']
    assert row['status']=='completed' and row['completion']['candidate_sha256']
