"""실제 네이티브 단계는 중앙 원장에 기록하고 공유 Office를 단독 소유한다."""
import asyncio
from pathlib import Path
import sys
import pytest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from asis_tasks import TaskManager,task


def test_native_code_stage_is_recorded_during_execution_not_afterward(tmp_path):
    manager=TaskManager(tmp_path,{'revision':'native-unit'},specs=[
        task('prepare','준비'),task('merge','병합',depends_on=['prepare'],scheduling='exclusive',resource='native-package')])
    seen=[]
    def preparation():
        seen.append(manager.ledger['tasks']['prepare']['status'])
        return {'real_operation':True}
    assert manager.perform_sync('prepare',preparation)=={'real_operation':True}
    assert seen==['running']
    assert manager.perform_sync('merge',lambda:42)==42
    assert all(row['status']=='completed' for row in manager.ledger['tasks'].values())
    assert manager.ledger['tasks']['merge']['completion']['confirmed_by']=='central_task_manager'


def test_native_build_records_freeze_ppt_and_actual_render_before_calls(tmp_path,monkeypatch):
    """같은 제품 builder가 실제 실행 시점에 Excel-first·PPT·렌더를 중앙 원장에 기록한다."""
    import asis_analysis,asis_artifacts
    from test_asis_connection import synthetic_input
    from asis_tasks import initial_graph
    result=asis_analysis.analyze(synthetic_input(tmp_path))
    manager=TaskManager(tmp_path/'ledger',result['input']['run'],specs=initial_graph(asis_analysis.AREA_NAMES))
    for key in ('interpret',*['issue-plan-area-'+k for k in asis_analysis.AREA_NAMES],*['issue-plan-area-'+k+'-review' for k in asis_analysis.AREA_NAMES],'issue-plan',*['area-'+k for k in asis_analysis.AREA_NAMES],
                'synthesis','actions','summary','document','visual-specs','analysis-content'):
        if manager.ledger['tasks'][key]['executor']=='model':
            from asis_model import digest,encoded
            proof=manager.folder/'fixture-prerequisites'/key/'result.json';proof.parent.mkdir(parents=True,exist_ok=True)
            proof.write_bytes(encoded(None))
            def complete_model(key=key,proof=proof):
                manager.report_output(key,input_sha256=digest(result['input']),result_sha256=digest(None),result_path=proof,evidence=[proof])
                return None
            asyncio.run(manager.perform(key,complete_model))
        else:
            asyncio.run(manager.perform(key,lambda:None))
    seen=[]
    real_ppt=asis_artifacts.presentation
    def observed(result,destination):
        assert manager.ledger['tasks']['workbook-freeze']['status']=='completed'
        assert manager.ledger['tasks']['native-presentation']['status']=='running'
        seen.append('ppt')
        return real_ppt(result,destination)
    monkeypatch.setattr(asis_artifacts,'presentation',observed)
    manifest=asis_artifacts.build(__import__('visual_spec_stub').with_visual_specs(result),tmp_path/'native',task_manager=manager)
    assert seen==['ppt'] and manifest['render']['previews']
    assert all(manager.ledger['tasks'][k]['status']=='completed' for k in ('workbook-freeze','native-presentation','actual_render'))
    assert manager.ledger['tasks']['actual_render']['completion']['evidence']


def test_native_code_stage_refuses_model_task_and_missing_dependency(tmp_path):
    manager=TaskManager(tmp_path,{},specs=[task('model','모델',executor='model'),task('native','네이티브',depends_on=['model'])])
    with pytest.raises(ValueError,match='sync_task_requires_code'):
        manager.perform_sync('model',lambda:None)
    with pytest.raises(RuntimeError,match='task_dependency_not_completed'):
        manager.perform_sync('native',lambda:None)


def test_parallel_native_workers_share_runtime_resource_admission_and_central_ledger(tmp_path):
    """서로 다른 파일을 만드는 네이티브 작업도 같은 자원 제어기·중앙 원장으로 진입한다."""
    import time
    from types import SimpleNamespace
    from asis_admission import AdaptiveAdmission
    sample={'total_memory_bytes':1000000000,'available_memory_bytes':900000000,
            'cpu_percent':5,'worker_tree_rss_bytes':1000000,'sample_valid':True}
    gate=AdaptiveAdmission(sampler=lambda:dict(sample),interval=.001)
    async def exercise():
        manager=TaskManager(tmp_path,{},specs=[task('part-'+str(i),'독립 조각') for i in range(6)],runtime=SimpleNamespace(gate=gate))
        def operation(index):
            assert manager.ledger['tasks']['part-'+str(index)]['status']=='running'
            time.sleep(.025)
            p=tmp_path/('part-'+str(index)+'.txt');p.write_text(str(index),encoding='utf-8')
            return index
        values=await asyncio.to_thread(manager.parallel_sync,
            { 'part-'+str(i):(lambda i=i:operation(i)) for i in range(6) })
        assert list(values.values())==list(range(6))
        assert manager.progress()['completed_tasks']==6
        assert gate.peak_active>2 and gate.active==gate.waiting==0
    asyncio.run(exercise())


def test_workbook_sheets_are_prepared_independently_then_merged_once(tmp_path):
    """각 시트는 별도 객체로 병렬 준비하고 공유 파일 저장·실제 재계산은 단독 gate다."""
    import asis_analysis,asis_artifacts
    from test_asis_connection import synthetic_input
    from asis_tasks import initial_graph
    from openpyxl import load_workbook
    result=asis_analysis.analyze(synthetic_input(tmp_path))
    manager=TaskManager(tmp_path/'ledger',result['input']['run'],specs=initial_graph(asis_analysis.AREA_NAMES))
    for key in ('interpret',*['issue-plan-area-'+k for k in asis_analysis.AREA_NAMES],*['issue-plan-area-'+k+'-review' for k in asis_analysis.AREA_NAMES],'issue-plan',*['area-'+k for k in asis_analysis.AREA_NAMES],
                'synthesis','actions','summary','document','visual-specs','analysis-content'):
        if manager.ledger['tasks'][key]['executor']=='model':
            from asis_model import digest,encoded
            proof=manager.folder/'fixture-prerequisites'/key/'result.json';proof.parent.mkdir(parents=True,exist_ok=True)
            proof.write_bytes(encoded(None))
            def complete_model(key=key,proof=proof):
                manager.report_output(key,input_sha256=digest(result['input']),result_sha256=digest(None),result_path=proof,evidence=[proof])
                return None
            asyncio.run(manager.perform(key,complete_model))
        else:
            asyncio.run(manager.perform(key,lambda:None))
    path=tmp_path/'workbook.xlsx'
    from asis_tasks import active_task_manager
    token=active_task_manager.set(manager)
    try:manager.perform_sync('workbook-freeze',lambda:asis_artifacts.workbook(result,path))
    finally:active_task_manager.reset(token)
    ledger=manager.ledger['tasks']
    prepared=[v for k,v in ledger.items() if k.startswith('excel-sheet-')]
    assert len(prepared)>10 and all(v['status']=='completed' and v['scheduling']=='parallel' for v in prepared)
    assert ledger['excel-merge']['status']=='completed' and ledger['excel-recalculate']['scheduling']=='exclusive'
    wb=load_workbook(path,data_only=True)
    assert wb['실행 정보']['B7'].value==len(result['input']['facts'])
    assert wb['사실원장']['A7'].font.name==asis_artifacts.FONT


def test_approved_native_fragment_generates_only_its_owned_section(tmp_path):
    """같은 동결 분석·승인 틀에서 독립 PPT 조각만 생성하며 샘플 본문을 재사용하지 않는다."""
    import json,asis_analysis,asis_approved_presentation
    from test_asis_connection import synthetic_input
    from pptx import Presentation
    result=asis_analysis.analyze(synthetic_input(tmp_path))
    ppt=tmp_path/'area.pptx'
    asis_approved_presentation.presentation(__import__('visual_spec_stub').with_visual_specs(result),ppt,section='area-network')
    rows=json.loads(ppt.with_suffix('.layout.json').read_text(encoding='utf-8'))['pages']
    assert rows and {row['family'] for row in rows}<={'area_fact','area_detail'}
    assert 'cover' not in {row['family'] for row in rows}
    texts='\n'.join(shape.text for slide in Presentation(ppt).slides for shape in slide.shapes if shape.has_text_frame)
    assert result['stages'][0]['areas'][0]['headline'] in texts


def test_parallel_approved_fragments_merge_preserves_text_notes_and_navigation(tmp_path):
    import json,asis_analysis,asis_approved_presentation
    from asis_native import parallel_presentation
    from test_asis_connection import synthetic_input
    from pptx import Presentation
    result=asis_analysis.analyze(synthetic_input(tmp_path))
    reference=tmp_path/'reference.pptx';asis_approved_presentation.presentation(__import__('visual_spec_stub').with_visual_specs(result),reference)
    target=tmp_path/'parallel.pptx';parallel_presentation(__import__('visual_spec_stub').with_visual_specs(result),target)
    def content(path):
        deck=Presentation(path)
        return [(tuple(s.text for s in slide.shapes if s.has_text_frame and not s.text.startswith('다음 상세') and not __import__('re').fullmatch(r'\d+ / \d+',s.text)),slide.notes_slide.notes_text_frame.text) for slide in deck.slides]
    assert content(target)==content(reference)
    deck=Presentation(target)
    masters={master.part for master in deck.slide_masters}
    assert all(slide.slide_layout.slide_master.part in masters for slide in deck.slides), 'merged_slide_master_not_registered'
    assert all(next(s for s in slide.shapes if getattr(s,'text','')=='다음 상세').click_action.target_slide is deck.slides[i+1] for i,slide in enumerate(list(deck.slides)[:-1]))
    rows=json.loads(target.with_suffix('.layout.json').read_text(encoding='utf-8'))['pages']
    assert len(rows)==len(deck.slides)
