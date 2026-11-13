"""보완 중 이전 산출물 검토 통과 표시를 재사용하지 않는 진행 원장 시험."""
from test_asis_worker import controller,running_job


def test_repair_invalidates_dependent_progress_and_preserves_unrelated_area(tmp_path):
    control=controller(tmp_path);aid,revision,_=running_job(control)
    completed=[('area-network','author'),('area-network','content_review'),('area-ai','author'),('area-ai','content_review'),
        ('synthesis','content_review'),('actions','content_review'),('summary','content_review'),('document','content_review'),
        ('native_pair','native'),('actual_render','render'),('pair_consistency','consistency')]
    for unit,phase in completed:control.record_progress(aid,revision,{'unit':unit,'phase':phase,'status':'completed'})
    control.record_progress(aid,revision,{'unit':'visual_pages','phase':'visual_review','status':'completed','total_pages':20,'reviewed_pages':20})
    before=control.job(aid,revision)['progress']['overall_percent']
    control.record_progress(aid,revision,{'unit':'document','phase':'repair','status':'required','affected_units':['area-network'],
        'invalidated_dependents':['native_pair','actual_render','visual_pages','pair_consistency']})
    progress=control.job(aid,revision)['progress']
    assert progress['overall_percent']<before
    rows=progress['units']
    assert next(r for r in rows if r['unit']=='area-ai' and r['phase']=='content_review')['status']=='completed'
    for unit in ('area-network','synthesis','actions','summary','document','native_pair','actual_render','visual_pages','pair_consistency'):
        checks=[r for r in rows if r['unit']==unit and r['phase']!='repair']
        assert checks and all(r['status']=='invalidated' for r in checks)
    assert progress['reviewed_pages']==0


def test_central_task_progress_is_authoritative_and_role_events_cannot_inflate_it(tmp_path):
    """새 작업 원장의 세부 완료만 비율에 반영하고 실행·심박은 완료로 승격하지 않는다."""
    control=controller(tmp_path);aid,revision,_=running_job(control)
    value={'schema':'asis-task-progress/1','overall_percent':12.5,'completed_tasks':1,'total_tasks':8,
           'running_tasks':[{'id':'action-1','title':'과제별 작성·검토'}],'plan_version':2,'blocked_tasks':[]}
    control.record_progress(aid,revision,{'unit':'tasks','phase':'tasks','status':'updated','task_progress':value})
    control.record_progress(aid,revision,{'unit':'actions','phase':'content_review','status':'completed'})
    progress=control.job(aid,revision)['progress']
    assert progress['overall_percent']==12.5
    assert progress['completed_tasks']==1 and progress['total_tasks']==8
    assert progress['running_tasks'][0]['title']=='과제별 작성·검토'
    value.update(overall_percent=0,completed_tasks=0)
    control.record_progress(aid,revision,{'unit':'tasks','phase':'tasks','status':'updated','task_progress':value})
    assert control.job(aid,revision)['progress']['overall_percent']==0


def test_controller_preserves_dependency_waits_from_central_task_progress(tmp_path):
    control=controller(tmp_path);aid,revision,_=running_job(control)
    value={'schema':'asis-task-progress/1','overall_percent':8.33,'completed_tasks':1,'total_tasks':12,
        'running_tasks':[],'waiting_tasks':[{'id':'next','title':'진단 승인 대기','reason':'dependency_wait'}],
        'plan_version':2,'blocked_tasks':[]}
    control.record_progress(aid,revision,{'unit':'tasks','phase':'tasks','status':'updated','task_progress':value})
    assert control.job(aid,revision)['progress']['waiting_tasks']==value['waiting_tasks']
