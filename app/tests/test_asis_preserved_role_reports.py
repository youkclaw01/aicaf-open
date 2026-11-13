"""코드 복구 재개에서 완료된 원래 역할 보고 원장을 다시 쓰지 않는 계약."""
from copy import deepcopy
from pathlib import Path
import asyncio
import json
import sys
import pytest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from asis_tasks import TaskManager,task
from asis_model import digest


def completed_reviewer_only(folder):
    run={'revision':'synthetic-preserved-synthesis'}
    manager=TaskManager(folder,run,specs=[task('synthesis','합성 종합',executor='model')])
    manager.bind_review('synthesis',reviewer_session='fixed-reviewer')
    candidate={'directions':[{'id':'D1','text':'합성 검증'}]}
    checks={'verdict':'pass','content_checked':True,'render_checked':False,'defects':[],'unresolved':[]}
    evidence=folder/'original-review.json';evidence.write_text(json.dumps(checks),encoding='utf-8')
    def original():
        manager.report('synthesis','reviewer',session_id='fixed-reviewer',candidate_sha256=digest(candidate),
            evidence=[evidence],checks=checks,summary='원래 독립 검토')
        return candidate
    asyncio.run(manager.perform('synthesis',original))
    prior=deepcopy(manager.ledger['tasks']['synthesis'])
    manager.preserved_completed={'synthesis'}
    return manager,prior,candidate,checks


def test_preserved_completed_cached_role_report_does_not_overwrite_original_ledger(tmp_path):
    manager,prior,candidate,checks=completed_reviewer_only(tmp_path)
    cache=tmp_path/'cached-review.json';cache.write_text(json.dumps(checks),encoding='utf-8')
    manager.report('synthesis','reviewer',session_id='fixed-reviewer',candidate_sha256=digest(candidate),
        evidence=[cache],checks=checks,summary='새 코드 캐시 바인딩 확인')
    assert manager.ledger['tasks']['synthesis']==prior
    stored=json.loads((tmp_path/'task-ledger.json').read_text(encoding='utf-8'))
    assert stored['tasks']['synthesis']==prior


@pytest.mark.parametrize('drift',['candidate','review'])
def test_preserved_completed_role_report_still_rejects_changed_candidate_or_review(tmp_path,drift):
    manager,prior,candidate,checks=completed_reviewer_only(tmp_path)
    if drift=='candidate':candidate['directions'][0]['text']='서로 다른 합성 후보'
    if drift=='review':checks['verdict']='revise';checks['defects']=[{'where':'/directions/0/text'}]
    cache=tmp_path/'changed-review.json';cache.write_text(json.dumps(checks),encoding='utf-8')
    with pytest.raises(ValueError,match='preserved_role_report_changed'):
        manager.report('synthesis','reviewer',session_id='fixed-reviewer',candidate_sha256=digest(candidate),
            evidence=[cache],checks=checks,summary='이전 승인으로 새 결과를 통과시키면 안 된다')
    assert manager.ledger['tasks']['synthesis']==prior
