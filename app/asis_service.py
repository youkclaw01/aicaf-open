"""AS-IS 작성·검토·네이티브 산출물·실제 렌더 검토의 제품 실행 경로."""
from copy import deepcopy
from io import BytesIO
from pathlib import Path
import asyncio
import hashlib
import json
import re

from PIL import Image, ImageDraw
from pptx import Presentation

from asis_model import digest, encoded
from asis_pipeline import Pipeline
from asis_runtime import Runtime, policy


def artifact_record(manifest, role):
    return next(item for item in manifest['artifacts'] if item['role']==role)


def _page_metadata(folder, manifest):
    """픽셀 contact를 생성하지 않고 실제 장표 원문·소유·PNG 해시와 크기만 준비한다."""
    folder = Path(folder)
    presentation = Presentation(folder/artifact_record(manifest,'asis_report_pptx')['name'])
    pngs = manifest['render']['previews']
    if len(presentation.slides) != len(pngs):
        raise ValueError('render_slide_count_mismatch')
    map_path = (folder/artifact_record(manifest,'asis_report_pptx')['name']).with_suffix('.layout.json')
    import json
    mapping = json.loads(map_path.read_text(encoding='utf-8')) if map_path.is_file() else {'pages': []}
    page_refs = {i+1:row.get('item_refs',[]) for i,row in enumerate(mapping['pages'])}
    pages = []
    for index, record in enumerate(pngs):
        raw = (folder/record['name']).read_bytes()
        if hashlib.sha256(raw).hexdigest() != record['sha256']:
            raise ValueError('render_png_hash_mismatch')
        with Image.open(BytesIO(raw)) as image:
            width, height = image.size
        texts = []
        for shape in presentation.slides[index].shapes:
            if shape.has_text_frame:
                texts.append(getattr(shape,'text',''))
            if shape.has_table:
                texts.extend(cell.text for row in getattr(shape,'table').rows for cell in row.cells)
        pages.append({'page':index+1, 'texts':texts, 'png_sha256':record['sha256'],
                      'item_refs':page_refs.get(index+1,[]), 'width':width, 'height':height})
    return pages


def _page_batches(folder, manifest, size=4, page_numbers=None, page_metadata=None):
    """실행 중 소단위에서만 원본 PNG contact를 만든다. 원문 메타데이터는 공유할 수 있다."""
    folder = Path(folder)
    metadata = _page_metadata(folder,manifest) if page_metadata is None else page_metadata
    pngs = manifest['render']['previews']
    if len(metadata) != len(pngs):
        raise ValueError('render_slide_count_mismatch')
    selected = list(range(len(pngs))) if page_numbers is None else [number-1 for number in page_numbers]
    if any(type(index) is not int or not 0 <= index < len(pngs) for index in selected):
        raise ValueError('item_render_page_number_invalid')
    for start in range(0, len(selected), size):
        indices = selected[start:start+size]
        images, pages = [], []
        for index in indices:
            record = pngs[index]
            raw = (folder/record['name']).read_bytes()
            if hashlib.sha256(raw).hexdigest() != record['sha256']:
                raise ValueError('render_png_hash_mismatch')
            images.append(Image.open(BytesIO(raw)).convert('RGB'))
            if (metadata[index]['width'],metadata[index]['height']) != images[-1].size:
                raise ValueError('render_png_dimensions_changed')
            pages.append(metadata[index])
        width = max(image.width for image in images)
        height = max(image.height for image in images)
        columns = 2 if len(images)>1 else 1
        rows = (len(images)+columns-1)//columns
        contact = Image.new('RGB',(columns*width,rows*(height+32)), 'white')
        draw = ImageDraw.Draw(contact)
        for index,image in enumerate(images):
            x=(index%columns)*width
            y=(index//columns)*(height+32)
            draw.text((x+12,y+9), 'PAGE '+str(pages[index]['page']), fill='black')
            contact.paste(image,(x,y+32))
        stream=BytesIO();contact.save(stream,format='PNG')
        yield pages,stream.getvalue()


def _relocate_manifest(manifest, prefix):
    """후보 개정별 산출물을 보존하며 공개 원장의 파일 경로만 상대 이동한다."""
    manifest=deepcopy(manifest)
    for item in manifest['artifacts']:
        item['name']=prefix+'/'+item['name']
    if manifest['render'].get('pdf'):
        manifest['render']['pdf']=prefix+'/'+manifest['render']['pdf']
    for page in manifest['render'].get('previews',[]):
        page['name']=prefix+'/'+page['name']
    return manifest


def _page_batch(folder, manifest, numbers, metadata):
    """선택된 소단위의 픽셀만 작업 thread에서 생성한다."""
    return next(_page_batches(folder,manifest,page_numbers=numbers,page_metadata=metadata))


async def _review_rendered_items(pipeline, result, native, manifest, attempt, render_record, page_metadata=None):
    """담당 항목의 실제 전장 검토를 마친 후 지적을 모아 원래 쌍으로 반환한다."""
    from asis_item_agents import item_input, validate_item
    stages = result['stages']; data = result['input']
    plan = data['context']['action_item_plan']
    all_pages = await asyncio.to_thread(_page_metadata,native,manifest) if page_metadata is None else page_metadata
    ownership = {row['id']: [page['page'] for page in all_pages if row['id'] in page['item_refs']]
                 for family in ('diagnoses', 'actions') for row in plan[family]}
    if any(not pages for pages in ownership.values()):
        raise RuntimeError('item_render_page_coverage_missing')
    pipeline.save(pipeline.folder/'visual'/('r'+str(attempt))/'item-page-map.json',
                  {'pptx_sha256':render_record['pptx_sha256'],'items':ownership})
    diagnoses = {row['id']:row for row in stages[2]['diagnoses']}

    async def inspect_item(kind, slot):
        unit = kind+'-'+slot['id']
        async def admitted_body():
            async with pipeline.item_gate:
                return await inspect_item_body(kind,slot)
        return await pipeline.tasks.perform('native-'+unit,admitted_body,allow_review_revision=True)

    async def inspect_item_body(kind, slot):
        unit = kind+'-'+slot['id']
        family = 'diagnoses' if kind == 'diagnosis' else 'actions'
        row = next(row for row in stages[2][family] if row['id'] == slot['id'])
        scoped = item_input(data, kind, slot, diagnoses)
        draft = {'stage':'actions','run':deepcopy(data['run']),'diagnoses':[],
                 'actions':[],'edges':[],'unresolved':[]}
        draft[family] = [deepcopy(row)]
        field, relation = ('direction_refs','diagnoses') if kind == 'diagnosis' else ('diagnosis_refs','addresses')
        draft['edges'] = [{'from_id':ref,'to_id':row['id'],'relation':relation} for ref in row[field]]
        validate_item(pipeline, scoped, draft)
        reports = []
        for index, start in enumerate(range(0,len(ownership[slot['id']]),4)):
            numbers = ownership[slot['id']][start:start+4]
            pages, image = await asyncio.to_thread(_page_batch,native,manifest,numbers,all_pages)
            key = 'item-visual-r'+str(attempt)+'-'+unit+'-'+str(index+1).zfill(3)
            item_draft = {**deepcopy(draft),'pages':pages}
            async with pipeline.review_gate:
                review = await pipeline.independent_review(scoped, item_draft, key, item_key=unit, image=image,
                    render_record={**render_record,'artifact_sha256':render_record['pptx_sha256'],
                                   'preview_sha256':hashlib.sha256(image).hexdigest()})
            reports.append({'pages':[page['page'] for page in pages],'review':review,'review_key':key,
                            'image_sha256':hashlib.sha256(image).hexdigest()})
        # 같은 항목의 모든 페이지 검토가 완료되어야 corrective author 호출이 허용된다.
        defects = []
        for report in reports:
            for defect in report['review']['defects']:
                item = deepcopy(defect)
                if item['where'].startswith('/pages/'):
                    parts = item['where'].split('/')
                    page = report['pages'][int(parts[2])]
                    item['where'] = '/render_pages/'+str(page)+'/texts/'+parts[-1]
                elif item['where'].startswith('/'+family+'/0'):
                    position = next(i for i,value in enumerate(stages[2][family]) if value['id'] == slot['id'])
                    item['where'] = '/stages/2/'+family+'/'+str(position)+item['where'][len('/'+family+'/0'):]
                else:
                    raise RuntimeError('item_visual_defect_target_unbound')
                if item not in defects: defects.append(item)
        verdict = ('blocked' if any(r['review']['verdict']=='blocked' for r in reports) else
                   'revise' if any(r['review']['verdict']=='revise' for r in reports) else 'pass')
        record = {'item_id':slot['id'],'item_kind':kind,'unit':unit,'verdict':verdict,
                  'reviewed_pages':ownership[slot['id']],'reports':reports,'defects':defects,
                  'reviewed_native_pptx_sha256':render_record['pptx_sha256']}
        record_path=pipeline.folder/'visual'/('r'+str(attempt))/'items'/unit/'review.json'
        pipeline.save(record_path,record)
        native_key='native-'+unit
        binding=digest({'item':draft,'pages':[p for p in all_pages if p['page'] in ownership[slot['id']]],
                        'pptx_sha256':render_record['pptx_sha256']})
        checks={'verdict':verdict,'content_checked':True,'render_checked':True,
                'defects':defects,'unresolved':[]}
        for role in ('author','reviewer'):
            pipeline.tasks.report(native_key,role,session_id=pipeline.tasks.ledger['tasks'][native_key]['pair'][role],
                candidate_sha256=binding,evidence=[record_path,native/artifact_record(manifest,'asis_report_pptx')['name']],
                checks=checks if role=='reviewer' else {'deterministic_passed':True},
                summary='기존 작성 후보와 담당 항목 전장 렌더 독립 검토를 중앙에 보고')
        if verdict!='pass':
            pipeline.tasks.resolve(native_key,role='author',action='revise_candidate' if defects else 'inspect_review_block',
                candidate=draft,defects=defects,reason='native_review_revision_required')
        pipeline.event(unit,'visual_review','completed' if verdict=='pass' else 'blocked',
                       item_id=slot['id'],verdict=verdict,reviewed_pages=len(ownership[slot['id']]))
        return record

    tasks = [inspect_item(kind, slot) for kind,family in (('diagnosis','diagnoses'),('action','actions'))
             for slot in plan[family]]
    return await asyncio.gather(*tasks)


def _attach_chat(pipeline, folder):
    """분석 단계 동안만 열리는 현황분석 담당 채팅과 고객별 사건 저장소를 파이프라인에 연결한다.
    연결 실패는 생성 자체를 막지 않는다(채팅이 없으면 기존처럼 실패로 둔다). 고객 폴더는 평가 폴더 아래 `knowledge`이며
    출처는 개정 폴더 안의 실제 파일이어야 한다."""
    try:
        import asis_chat, asis_incidents
        chat = asis_chat.Chat(folder)
        if not chat.is_closed():
            chat.post(sender='analysis_owner', text='현황분석을 시작했습니다. 영역별 이슈를 동시에 작성·검토하며, 해결하지 못한 문제가 생기면 이곳에서 확인을 요청드립니다.')
        pipeline.chat = chat
        # 출처 파일은 이 개정 폴더 안의 실제 파일이어야 한다(다른 개정의 같은 이름 파일로 검증되지 않도록 개정 폴더를 기준으로 한다).
        pipeline.incident_store = asis_incidents.Store(Path(folder).parent.parent / 'knowledge', source_root=Path(folder))
    except Exception as exc:
        pipeline.event('chat', 'chat', 'attach_failed', error=type(exc).__name__ + ':' + str(exc)[:120])


def _close_analysis_chat(pipeline, reason):
    """분석 단계가 끝나면 채팅을 닫는다. 열린 질문이 남아 있으면 닫지 않고 이벤트만 남긴다(이력은 보존)."""
    chat = getattr(pipeline, 'chat', None)
    if chat is None:
        return
    try:
        chat.post(sender='analysis_owner', text='분석 단계가 끝났습니다. 산출물 단계로 넘어갑니다.')
        chat.close(reason)
    except Exception as exc:
        pipeline.event('chat', 'chat', 'close_skipped', error=type(exc).__name__ + ':' + str(exc)[:120])


def next_native_round(folder):
    """같은 개정의 기존 후보·렌더·검토 기록을 덮어쓰지 않는 다음 번호다."""
    candidates=[int(p.name[6:]) for p in (Path(folder)/'artifact-rounds').glob('round-*')
        if p.is_dir() and p.name[6:].isdigit()]
    if candidates:
        latest = max(candidates)
        review_index = Path(folder)/'visual'/('r'+str(latest))/'review-index.json'
        if review_index.is_file():
            try:
                rows = json.loads(review_index.read_text(encoding='utf-8'))
                if isinstance(rows, list) and rows and all((r.get('review') or {}).get('verdict') == 'pass' for r in rows):
                    return latest
            except (ValueError, OSError):
                pass
    return max(candidates,default=0)+1


async def execute(data, folder, *, builder, progress=None, revision_context=None,
                  runtime_factory=Runtime, rounds=3):
    """실제 검토가 끝난 동일 네이티브 파일만 최종 후보로 등록한다. 제출 승인은 별도다."""
    from assessment_current_state_presentation import prepare_editorial,editorial_task_specs
    folder=Path(folder)
    async with runtime_factory(base_directory=folder/'sessions') as runtime:
        pipeline=Pipeline(runtime, folder, progress=progress, rounds=rounds)
        _attach_chat(pipeline, folder)
        current_only = data.get('context', {}).get('report_kind') == 'current_state'
        result=(await pipeline.analyze_current_state(data) if current_only else
                await pipeline.analyze(data, revision_context=revision_context))
        if current_only:
            from asis_tasks import task
            dependencies = ['current-editorial-'+row['area_id'] for row in result['stages'][0]['areas']]
            specs = editorial_task_specs(result)+[task('workbook-freeze','현황분석 Excel 동결',executor='code',depends_on=dependencies),
                     task('native-presentation','동결 Excel 기반 현황분석 PPT',executor='code',depends_on=['workbook-freeze']),
                     task('native_pair','현황분석 네이티브·실렌더',executor='code',depends_on=dependencies),
                     task('actual_render','현황분석 실렌더',executor='code'),
                     task('visual_pages','현황분석 전체 장표 시각 검토',executor='code',depends_on=['native_pair']),
                     task('pair_consistency','현황분석 Excel·PPT 근거 일관성',executor='code',depends_on=['visual_pages'])]
            pipeline.tasks.register([s for s in specs if s['id'] not in pipeline.tasks.ledger['tasks']])
        _close_analysis_chat(pipeline, 'analysis_stages_completed')
        start_round=next_native_round(folder)
        for round_index in range(1,rounds+1):
            attempt=start_round+round_index-1
            if current_only:result=await prepare_editorial(pipeline,result)
            native=folder/'artifact-rounds'/('round-'+str(attempt).zfill(3))
            manifest_path=native/'artifact-manifest.json'
            if not manifest_path.is_file():manifest_path=native/'manifest.json'
            if manifest_path.is_file():
                manifest=json.loads(manifest_path.read_text(encoding='utf-8'))
                for sub in ('workbook-freeze','native-presentation','native_pair','actual_render'):
                    if sub in pipeline.tasks.ledger['tasks']:
                        pipeline.tasks.ledger['tasks'][sub]['status']='completed'
                        pipeline.tasks._events.setdefault(sub,asyncio.Event()).set()
                pipeline.event('native_pair','native','reused',round=attempt)
            else:
                native.mkdir(parents=True,exist_ok=True)
                pipeline.event('native_pair','native','running',round=attempt)
                async def build_native():
                    from asis_tasks import active_task_manager
                    token=active_task_manager.set(pipeline.tasks)
                    try:return await asyncio.to_thread(builder,result,native)
                    finally:active_task_manager.reset(token)
                manifest=await pipeline.run_task(result['input'],'native_pair',build_native)
            pipeline.event('native_pair','native','completed',round=attempt)
            render=manifest.get('render',{})
            if not render.get('previews') or len(render['previews'])!=render['page_count']:
                raise RuntimeError('actual_render_required')
            pipeline.event('actual_render','render','completed',total_pages=render['page_count'])
            page_metadata=await asyncio.to_thread(_page_metadata,native,manifest)
            batches=[list(range(start+1,min(start+5,len(page_metadata)+1)))
                     for start in range(0,len(page_metadata),4)]
            render_record={'verified':True,'page_count':render['page_count'],
                'pptx_sha256':artifact_record(manifest,'asis_report_pptx')['sha256'],
                'pdf_sha256':artifact_record(manifest,'asis_render_pdf')['sha256']}
            stale_visual=[k for k,row in pipeline.tasks.ledger['tasks'].items()
                          if ((k.startswith(('visual-r'+str(attempt)+'-','item-visual-r'+str(attempt)+'-'))
                               and (row.get('status') in ('revision_required','failed','running')
                                    or not (folder/'visual'/('r'+str(attempt))/'review-index.json').is_file()))
                              or (k.startswith(('native-diagnosis-','native-action-'))
                                  and (row.get('status') in ('revision_required','failed','running')
                                       or not (folder/'visual'/('r'+str(attempt))/'items'/k[7:]/'review.json').is_file())))]
            if stale_visual:
                pipeline.tasks.invalidate(stale_visual,reason='render_pptx_changed',downstream=False)
            reviewed=0
            async def inspect_batch(batch_index:int,numbers:list[int])->dict:
                nonlocal reviewed
                key='visual-r'+str(attempt)+'-'+str(batch_index+1).zfill(3)
                async with pipeline.item_gate:
                    pages,image=await asyncio.to_thread(_page_batch,native,manifest,numbers,page_metadata)
                    pipeline.save(folder/'visual'/('r'+str(attempt))/('batch-'+str(batch_index+1).zfill(3)+'.png'),image)
                    async with pipeline.review_gate:
                        draft={'pages':pages,'stages':result['stages']}
                        if current_only:draft['presentation']=result['presentation']
                        review=await pipeline.independent_review(result['input'],draft,key,
                                                                 image=image,render_record={**render_record,'artifact_sha256':render_record['pptx_sha256'],
                                                                                           'preview_sha256':hashlib.sha256(image).hexdigest()})
                    if review['verdict']=='pass':
                        reviewed+=len(pages)
                        pipeline.event('visual_pages','visual_review','running',round=attempt,
                            reviewed_pages=reviewed,total_pages=render['page_count'])
                    return {'pages':[page['page'] for page in pages], 'review':review,
                            'image_sha256':hashlib.sha256(image).hexdigest(),
                            'review_key':key,'reviewed_native_pptx_sha256':render_record['pptx_sha256']}
            async def inspect_items():
                if current_only:
                    return []  # 선행 현황분석에는 종합 진단·개선과제가 없다. 장표 전수 검토는 동일하게 수행한다.
                return await _review_rendered_items(pipeline,result,native,manifest,attempt,render_record,page_metadata)
            verdicts,item_reviews=await asyncio.gather(
                asyncio.gather(*(inspect_batch(i,numbers) for i,numbers in enumerate(batches))),
                inspect_items())
            pipeline.save(folder/'visual'/('r'+str(attempt))/'review-index.json',verdicts)
            failures=[row for row in verdicts if row['review']['verdict']!='pass']
            item_failures=[row for row in item_reviews if row['verdict']!='pass']
            failures.extend({'review':{'verdict':row['verdict'],'defects':row['defects']}} for row in item_failures)
            if failures:
                pipeline.event('visual_pages','repair','required',round=attempt,defects=sum(len(row['review']['defects']) for row in failures))
                if any(row['review']['verdict']=='blocked' for row in failures):
                    raise RuntimeError('visual_review_blocked')
                content_defects=[defect for row in failures for defect in row['review']['defects'] if defect['where'].startswith('/stages/')]
                editorial_revisions={}
                if current_only:
                    from assessment_current_state_presentation import editorial_defects_by_area
                    editorial_revisions=editorial_defects_by_area(result,[d for row in failures for d in row['review']['defects']])
                affected=list(pipeline.repair_plan(result['stages'],content_defects)) if content_defects else ['native_pair']
                pipeline.event('visual_pages','repair','required',round=attempt,affected_units=affected,
                    invalidated_dependents=['native_pair','actual_render','visual_pages','pair_consistency'])
                superseded=[k for k in pipeline.tasks.ledger['tasks'] if k.startswith(('visual-r'+str(attempt)+'-','item-visual-r'+str(attempt)+'-'))]
                for key in superseded:
                    pipeline.tasks.ledger['tasks'][key].update(status='superseded',superseded_by_round=attempt+1)
                pipeline.tasks.invalidate(['workbook-freeze','native_pair'],reason='native_review_revision_required')
                if content_defects:
                    result=(await pipeline.analyze_current_state(data,repair_seed=result,repair_defects=content_defects)
                            if current_only else await pipeline.analyze(data,revision_context=revision_context,repair_seed=result,repair_defects=content_defects))
                result=deepcopy(result)
                if editorial_revisions:
                    history=deepcopy(result.get('editorial_revision_context',{}))
                    for area_id,defects in editorial_revisions.items():history.setdefault(area_id,[]).extend(defects)
                    result['editorial_revision_context']=history
                result['layout_policy']={'body_max_lines':max(12,21-round_index*4),
                                         'observation_max_lines':max(7,15-round_index*3),
                                         'configuration_force_rows':True,'trace_edge_rows':True}
                result['visual_revision_context']={'prior_artifact_pptx_sha256':render_record['pptx_sha256'],
                    'literal_defects':[defect for row in failures for defect in row['review']['defects']],
                    'layout_reflow_attempt':attempt}
                continue
            if reviewed!=render['page_count']:
                raise RuntimeError('visual_review_coverage_incomplete')
            await pipeline.run_task(result['input'],'visual_pages',lambda:{'reviewed_pages':reviewed,'verdicts':verdicts,'item_reviews':item_reviews})
            pipeline.event('visual_pages','visual_review','completed',reviewed_pages=reviewed,
                           total_pages=render['page_count'],round=attempt)
            pipeline.event('pair_consistency','consistency','running')
            # 등록 직전 파일 바이트를 다시 확인한다. 이전 개정의 검토를 새 파일에 이식하지 않는다.
            for item in manifest['artifacts']:
                if hashlib.sha256((native/item['name']).read_bytes()).hexdigest()!=item['sha256']:
                    raise RuntimeError('artifact_changed_after_review')
            from asis_artifacts import verify_pair
            if current_only:
                from assessment_flow_artifacts import verify_current_state as verify_pair
            pair_check=await pipeline.run_task(result['input'],'pair_consistency',
                lambda:verify_pair(result,native/artifact_record(manifest,'asis_report_pptx')['name'],native/artifact_record(manifest,'asis_backdata_xlsx')['name']))
            quality={'schema':'ai-assessment-asis-quality-review/v1',
                'submission_approved':False,'all_render_pages_reviewed':True,
                'reviewed_pages':reviewed,'render_page_count':render['page_count'],
                'content_review_passed':True,'model':policy()['models']['content_review'],
                'model_policy':policy(),'framework':'microsoft-agent-framework',
                'pptx_sha256':render_record['pptx_sha256'],'xlsx_sha256':artifact_record(manifest,'asis_backdata_xlsx')['sha256'],
                'analysis_sha256':digest(result),'visual_batches':verdicts,'native_pair_check':pair_check,
                'item_reviews':item_reviews, 'item_lifecycle_complete':True}
            pipeline.save(folder/'quality-review.json',quality)
            final=_relocate_manifest(manifest,native.relative_to(folder).as_posix())
            final.update(quality_status='automated_review_passed',quality_review=quality,submission_approved=False)
            pipeline.save(folder/'artifact-manifest.json',final)
            pipeline.event('pair_consistency','consistency','completed')
            return result,final
        raise RuntimeError('visual_revision_budget_exhausted')
