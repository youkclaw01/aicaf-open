"""해석·초기/종합/항목 계획 완료는 결과와 원천 체크포인트 바이트에 결합된다."""
from pathlib import Path
import asyncio
import hashlib
import json
import sys

sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from asis_pipeline import Pipeline
from asis_model import digest
from test_asis_connection import SyntheticRuntime,synthetic_input


def test_every_unpaired_model_completion_is_bound_to_actual_output_evidence(tmp_path):
    engine=Pipeline(SyntheticRuntime(),tmp_path/'run')
    result=asyncio.run(engine.analyze(synthetic_input(tmp_path)))
    manager=engine.tasks
    assert manager is not None
    for key in ('interpret','issue-plan','synthesis-plan','action-item-plan'):
        row=manager.ledger['tasks'][key]
        assert row['status']=='completed'
        proof=row.get('output_report')
        assert proof is not None, 'model task completed without bound result: '+key
        assert proof['result_sha256']==digest(json.loads((manager.folder/proof['result_path']).read_text(encoding='utf-8')))
        assert len(proof['input_sha256'])==64
        assert proof['evidence']
        assert all(hashlib.sha256((manager.folder/file['path']).read_bytes()).hexdigest()==file['sha256'] for file in proof['evidence'])
        assert row['completion']['result_sha256']==proof['result_sha256']
    assert result['quality_status']=='content_review_passed'


def test_identical_plan_result_retains_prior_checkpoint_index(tmp_path):
    from asis_tasks import task
    from asis_view import atomic
    data=synthetic_input(tmp_path);engine=Pipeline(SyntheticRuntime(),tmp_path/'run')
    manager=engine.task_manager(data)
    manager.register([task('custom-plan','원문 계획',executor='model')])
    count=0
    def operation():
        nonlocal count
        count+=1
        atomic(engine.folder/'checkpoints/custom-plan/accepted.json',{'output':'same','response_id':str(count)})
        return {'plan':'same'}
    async def run():
        await engine.run_task(data,'custom-plan',operation)
        first=json.loads(json.dumps(manager.ledger['tasks']['custom-plan']['output_report']))
        await engine.run_task(data,'custom-plan',operation)
        return first
    first=asyncio.run(run())
    assert all(hashlib.sha256((manager.folder/file['path']).read_bytes()).hexdigest()==file['sha256']
               for file in first['evidence']), 'identical output overwrote prior invocation evidence'
