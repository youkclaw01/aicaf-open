"""공식 지침·실제 질문·고객 진술을 동결하는 독립 인터뷰 보고서 실행 경로."""
from __future__ import annotations

import asyncio
from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import io
import json
from pathlib import Path

from jsonschema import validate,ValidationError
from openpyxl import Workbook
from pptx import Presentation
from pptx.util import Inches

from asis_model import digest, encoded, strict_json
from asis_runtime import Runtime, policy
from asis_view import atomic
from assessment_flow_artifacts import _rows
import assessment_workflow as workflow

REVIEW_SCHEMA = {'type': 'object', 'additionalProperties': False,
    'required': ['verdict', 'content_checked', 'render_checked', 'defects'],
    'properties': {'verdict': {'enum': ['pass', 'revise', 'blocked']}, 'content_checked': {'const': True},
        'render_checked': {'type': 'boolean'}, 'defects': {'type': 'array', 'items': {
            'type': 'object', 'additionalProperties': False, 'required': ['where', 'quote', 'why', 'fix'],
            'properties': {key: {'type': 'string', 'minLength': 1} for key in ('where', 'quote', 'why', 'fix')}}}}}


def _pointer(candidate, pointer):
    value = candidate
    if not pointer.startswith('/'):raise ValueError('interview_review_pointer_invalid')
    for segment in pointer[1:].split('/'):
        key = segment.replace('~1', '/').replace('~0', '~')
        if isinstance(value, list) and (not key.isdecimal() or str(int(key)) != key):
            raise ValueError('interview_review_array_pointer_invalid')
        value = value[int(key)] if isinstance(value, list) else value[key]
    return value


async def _review(runtime, draft, folder, session, *, image=None, rounds=3, shared_cache=None):
    """실제 후보·이미지 바이트에 결합한 검토만 저장·재사용한다."""
    folder = Path(folder)
    system = ('DOCUMENT-PRO-001. 전문 컨설턴트 문체. 이 산출물은 인터뷰 보고서다. '
        '고객 진술·실제 질문·공식 지침·자체 평가를 구분한다. 고객 진술을 인프라 실측 사실로 승격하지 않는다. '
        '제공된 원문과 질문의 목적·요약·점수 표시의 일관성 및 누락만 검사한다. '
        '구두 점수의 정의·가중치·고객 원문·원점수는 변경하지 않는다. 점수는 Microsoft 공식 점수가 아니다. '
        '최종 종합 시사점·사업 영향 확정·개선과제를 요구하지 않는다. 미확인은 정상적인 보고 내용이다. '
        '결함은 후보의 실제 JSON pointer와 그 위치의 원문 quote, why, fix로 반환한다. '
        '지적이 없으면 verdict=pass, defects=[]다. JSON schema만 반환한다.')
    if image is not None:
        system += (' 이번에는 실제 렌더 이미지 전수를 담당 묶음별로 검토한다. '
                   '잘림·겹침·가독성·표시 누락·이미지와 텍스트의 불일치만 지적한다. render_checked=true다. '
                   '요약표의 생략 표시는 같은 보고서의 상세별첨과 전체 원장으로 보존되므로 그 자체는 결함이 아니다.')
    else:system += ' render_checked=false다. 실제 원문과 보고 요약의 의미가 다른 경우만 수정한다.'
    binding = digest({'draft': draft, 'system': system, 'policy': policy(),
                      'image': hashlib.sha256(image).hexdigest() if image is not None else None})
    cached = (Path(shared_cache) if shared_cache is not None else folder/'accepted')/f'{binding}.json'
    if cached.is_file():
        prior_accepted = strict_json(cached.read_bytes())
        validate(prior_accepted['review'], REVIEW_SCHEMA)
        if prior_accepted.get('binding') != binding or prior_accepted['review']['verdict'] != 'pass':
            raise ValueError('interview_review_cache_binding_invalid')
        if prior_accepted['review']['defects'] or prior_accepted['review']['render_checked'] is not (image is not None):
            raise ValueError('interview_review_cache_scope_invalid')
        atomic(folder/'reused-review.json', {'accepted_path':str(cached), 'binding':binding,
            'accepted_sha256':hashlib.sha256(cached.read_bytes()).hexdigest(), 'review':prior_accepted['review']})
        return prior_accepted['review']
    prior = None;error = None
    for attempt in range(1, rounds+1):
        payload = {'draft': draft, 'prior_review': prior, 'contract_error': error, 'output_json_schema': REVIEW_SCHEMA}
        raw, receipt = await runtime.ask('visual_review' if image is not None else 'content_review',
            encoded(payload).decode('utf-8'), session_id=session, instructions=system,
            image=image)
        run = folder/('contract-'+str(attempt))/binding
        atomic(run/'response.txt', raw.encode('utf-8'));atomic(run/'receipt.json', receipt)
        try:
            value = strict_json(raw.encode('utf-8'));validate(value, REVIEW_SCHEMA)
            if value['render_checked'] is not (image is not None):raise ValueError('interview_review_scope_flag')
            if value['verdict'] == 'pass' and value['defects']:raise ValueError('interview_review_pass_with_defects')
            if value['verdict'] == 'revise' and not value['defects']:raise ValueError('interview_review_revision_without_defects')
            for defect in value['defects']:
                target = _pointer(draft, defect['where'])
                text = target if isinstance(target, str) else json.dumps(target, ensure_ascii=False)
                if defect['quote'] not in text:raise ValueError('interview_review_quote_not_at_pointer')
        except (ValueError, KeyError, IndexError, TypeError, __import__('jsonschema').ValidationError) as exc:
            prior = raw;error = str(exc)
            atomic(run/'contract-error.json', {'error': error})
            if attempt == rounds:raise
            continue
        if value['verdict'] == 'pass':atomic(cached, {'review': value, 'receipt': receipt, 'binding': binding})
        return value
    raise RuntimeError('interview_review_contract_exhausted')


def interview_items(data, summaries=None):
    snapshot = data['context']['interview_snapshot'];summaries = summaries or {}
    return [{'question_id': q['id'], 'origin_id': q['origin_id'], 'area': q['area'], 'topic': q['topic'],
             'question': q['question'], 'followups': q['followups'], 'guidelines': q['guidelines'],
             'basis_fact_ids': q['fact_refs'], 'derivation_reason': q['derivation_reason'],
             'evaluation_use': q['evaluation_use'], 'transcript': snapshot['answers'][q['id']]['transcript'],
             'summary': summaries.get(q['id'], snapshot['answers'][q['id']].get('summary') or ''),
             'score': snapshot['answers'][q['id']]['score']}
            for q in snapshot['questionnaire']['questions']]


def fit_summary_layout(deck,meta):
    """잠긴 원문·점수·폰트는 유지하고 요약의 좁은 슬롯과 기준선 교차만 보정한다."""
    from collections import Counter
    import re
    from typing import Any,cast
    from pptx.dml.color import RGBColor
    from pptx.util import Pt
    summaries=[s for s in deck.slides if any(x.has_text_frame and x.text=='평가 결과 요약' for x in s.shapes)]
    if len(summaries)!=1:raise ValueError('interview_summary_family_not_unique')
    slide=summaries[0];shapes=cast(list[Any],list(slide.shapes))
    before=Counter(s.text for s in shapes if s.has_text_frame)
    units=[s for s in shapes if s.has_text_frame and re.fullmatch(r'(?:/\d+|개)',s.text)]
    labels=[]
    for key,name in meta['short'].items():
        matches=[s for s in shapes if s.has_text_frame and s.text==f'{key} {name}']
        if len(matches)!=1:raise ValueError('interview_summary_domain_not_unique')
        labels.append(matches[0])
    if not units:raise ValueError('interview_summary_unit_slots_missing')
    def tight(shape):
        frame=shape.text_frame;frame.word_wrap=False
        frame.margin_left=frame.margin_right=frame.margin_top=frame.margin_bottom=0
    for shape in units:tight(shape)
    labels.sort(key=lambda s:s.left+s.width/2);centers=[s.left+s.width/2 for s in labels]
    unit_bottom=max(s.top+s.height for s in units)
    plot_lines=[s for s in shapes if s.shape_type==9 and abs(s.height)<=Pt(.5) and s.left<=min(centers) and s.left+s.width>=max(centers) and unit_bottom<s.top<min(x.top for x in labels)]
    if not plot_lines:raise ValueError('interview_summary_plot_bounds_missing')
    plot_top=min(s.top for s in plot_lines)
    scores=[];deltas=[]
    for index,label in enumerate(labels):
        center=centers[index]
        distances=[abs(center-other) for j,other in enumerate(centers) if j!=index]
        if not distances:raise ValueError('interview_summary_domain_spacing_unavailable')
        runs=[r for p in label.text_frame.paragraphs for r in p.runs]
        point=next((r.font.size.pt for r in runs if r.font.size),9.0)
        width=int(min(distances)-Pt(point))
        if width<=0:raise ValueError('interview_summary_domain_spacing_invalid')
        label.width=width;label.left=int(center-width/2);tight(label)
        for shape in shapes:
            if not shape.has_text_frame or abs(shape.left+shape.width/2-center)>Pt(1):continue
            if re.fullmatch(r'[+-]\d+(?:\.\d+)?',shape.text) and label.top<=shape.top<label.top+label.height*3:
                shape.width=width;shape.left=int(center-width/2);tight(shape);deltas.append(shape)
            elif re.fullmatch(r'\d+(?:\.\d+)?',shape.text) and unit_bottom<shape.top<label.top:
                if shape.top<plot_top:shape.top=plot_top+Pt(point*.25)
                tight(shape);shape.fill.solid();shape.fill.fore_color.rgb=RGBColor(255,255,255)
                tree=slide.shapes._spTree;tree.remove(shape._element);tree.insert_element_before(shape._element,'p:extLst')
                scores.append(shape)
    after=Counter(s.text for s in slide.shapes if s.has_text_frame)
    if before!=after:raise ValueError('interview_summary_values_changed')
    page=next(i+1 for i,s in enumerate(deck.slides) if s.part is slide.part)
    return {'schema':'interview-summary-layout/1','page':page,'units':len(units),'domains':len(labels),'scores':len(scores),'deltas':len(deltas),'values_unchanged':True}


def summary_layout_failures(verdicts,summary_page):
    """고정 요약 가족의 시각 결함을 본문 재페이지화 대상으로 오분류하지 않는다."""
    found=[]
    for row in verdicts:
        pages=row.get('pages',[])
        for defect in row.get('review',{}).get('defects',[]):
            parts=str(defect.get('where','')).strip('/').split('/')
            if len(parts)<2 or parts[0]!='pages' or not parts[1].isdigit():continue
            index=int(parts[1])
            if index<len(pages) and pages[index]==summary_page:found.append(dict(defect,page=summary_page))
    return found


def build_pair(result, directory):
    """원래 인터뷰 양식·점수 계산을 보존하고 공식 지침·실제 파생 질문·전체 요약을 별첨한다."""
    import interview_report as report
    from questions import TRACK_META
    import scoring
    from asis_artifacts import paragraphs_pages, wrapped_lines, sha, convert
    data = result['input'];snapshot = data['context']['interview_snapshot']
    directory = Path(directory);directory.mkdir(parents=True, exist_ok=True)
    book = Workbook();book.remove(book.active)
    _rows(book, '개요', ['항목', '값'], [('보고서', '인터뷰 보고서'), ('프로젝트', data['run']['assessment_id']),
          ('실행', data['run']), ('근거 결합', data['context']['assessment_workflow']),
          ('판단 범위', '고객 진술·평가 근거·요구·제약. 인프라 관측 사실과 최종 종합 판단은 별도')])
    _rows(book, '질문과답변', ['문항', '원 문항', '질문', '후속 질문', '고객 원문', '보고 요약', '원 점수', '목적', '현황 근거'],
          ((i['question_id'], i['origin_id'], i['question'], i['followups'], i['transcript'], i['summary'], i['score'],
            i['evaluation_use'], i['basis_fact_ids']) for i in result['items']))
    _rows(book, '공식지침', ['문항', '기준', '공식 원문', '공식 출처', '조회본 해시', '자체 파생 목적'],
          ((i['question_id'], c['id'], c['quote'], c['source_url'], c.get('source_snapshot_sha256'), i['derivation_reason'])
           for i in result['items'] for c in i['guidelines']))
    frozen = book.create_sheet('_frozen_analysis');text = encoded(result).decode('utf-8')
    frozen.cell(1, 1, digest(result))
    for index, start in enumerate(range(0, len(text), 30000), 2):
        cell = frozen.cell(index, 1, text[start:start+30000]);cell.data_type = 's'
    frozen.sheet_state = 'veryHidden'
    xlsx = directory/'Interview-Report.xlsx';book.save(xlsx);book.close()
    from openpyxl import load_workbook
    book = load_workbook(xlsx, read_only=True)
    try:
        saved = strict_json(''.join(str(r[0] or '') for r in book['_frozen_analysis'].iter_rows(min_row=2, values_only=True)).encode('utf-8'))
        if digest(saved) != book['_frozen_analysis'].cell(1, 1).value or saved != result:
            raise ValueError('interview_excel_freeze_mismatch')
    finally:book.close()
    result = saved;data = result['input'];snapshot = data['context']['interview_snapshot']
    aliases = {i['question_id']: i['origin_id'] for i in result['items']}
    by_id = {i['question_id']: i for i in result['items']}
    from asis_consolidated_deck import to_polite_korean
    qs = deepcopy(snapshot['questionnaire']['questions']);answers = {}
    for q in qs:
        current_id = q['id'];q['id'] = aliases[current_id]
        ans = deepcopy(snapshot['answers'][current_id])
        ans['summary'] = to_polite_korean(by_id[current_id]['summary'])
        if ans.get('ai_reason'):ans['ai_reason'] = to_polite_korean(ans['ai_reason'])
        if ans.get('ai_detail'):
            try:
                det = json.loads(ans['ai_detail'])
                for dk in ('opinion', 'improve', 'next'):
                    if det.get(dk):det[dk] = to_polite_korean(det[dk])
                ans['ai_detail'] = json.dumps(det, ensure_ascii=False)
            except (ValueError, TypeError):
                pass
        answers[q['id']] = ans
    track = data['run']['track']
    topic_names = {q['topic']: q.get('display_topic_name') or q['topic_name'] for q in qs}
    assessment = deepcopy(data['context']['assessment_metadata'])
    raw = report.build(assessment, qs, answers, scoring.compute(qs, answers), TRACK_META[track])
    deck = Presentation(io.BytesIO(raw))
    summary_layout=fit_summary_layout(deck,TRACK_META[track])
    for shape in deck.slides[0].shapes:
        if shape.has_text_frame and ' – 인터뷰 종합' in shape.text:
            runs = [r for p in shape.text_frame.paragraphs for r in p.runs]
            if runs:
                runs[0].text = assessment['customer']+' 인터뷰 보고서'
                for run in runs[1:]:run.text = ''
    limit = int(result.get('layout_policy', {}).get('body_max_lines', 17))
    from asis_consolidated_deck import to_polite_korean
    topics = list(dict.fromkeys(i['topic'] for i in result['items']))
    for topic in topics:
        items = [i for i in result['items'] if i['topic'] == topic]
        guides = {c['id']: c for item in items for c in item['guidelines']}
        blocks = [('공식 지침 · '+c['id'], c['quote']+'\n'+c['source_url']) for c in guides.values()]
        for item in items:
            blocks.extend([(item['origin_id']+' · 실제 파생 질문', item['question']),
                           ('파생 목적', to_polite_korean(item['derivation_reason'])),
                           ('후속 확인', '\n'.join(item['followups'])),
                           ('고객 진술 요약', to_polite_korean(item['summary'] or '원문은 동일 문항의 인터뷰 근거 원장에 보존되어 있습니다.'))])
        for page in paragraphs_pages(blocks, width=11.96, pt=11.7, max_lines=min(17, limit)):
            slide = report.new(deck)
            report.header(slide, '공식 지침과 실제 인터뷰 · '+topic_names.get(topic, topic),
                          '공식 원문 → 현황 기반 자체 질문 → 실제 고객 진술 · 최종 종합 판단은 후속 보고서')
            y = 1.20
            for label, body in page:
                report.text(slide, int(Inches(.48)), int(Inches(y)), int(Inches(12.35)), int(Inches(.27)), label, 11.2, report.NAVY, bold=True)
                y += .32
                lines = wrapped_lines(body, 11.96, 11.7);height = max(.22, len(lines)*.215)
                report.text(slide, int(Inches(.48)), int(Inches(y)), int(Inches(12.35)), int(Inches(height)), '\n'.join(lines), 11.7, report.TXT)
                y += height+.15
            if y > 6.82:raise ValueError('interview_appendix_capacity')
            report.footer(slide, len(deck.slides))
    for index, slide in enumerate(deck.slides, 1):
        frame = slide.notes_slide.notes_text_frame
        if frame is not None:frame.text = json.dumps({'report_kind': 'interview', 'page': index,
            'analysis_sha256': digest(result), 'workflow': data['context']['assessment_workflow']}, ensure_ascii=False)
    deck.core_properties.title = '인터뷰 보고서'
    deck.core_properties.subject = data['run']['source_bundle_sha256']
    pptx = directory/'Interview-Report.pptx';deck.save(pptx)
    (directory/'input.json').write_bytes(encoded(data));(directory/'analysis.json').write_bytes(encoded(result))
    return {'pptx': pptx, 'xlsx': xlsx, 'result': result, 'pages': len(deck.slides),'summary_layout':summary_layout}


def build_native(result, directory):
    """동결 네이티브 쌍을 실제 렌더하고 원본·PDF·장표 이미지를 해시에 결합한다."""
    import interview_report as report
    from asis_artifacts import sha, convert
    directory = Path(directory)
    pair = build_pair(result, directory)
    pptx, xlsx, result = pair['pptx'], pair['xlsx'], pair['result']
    data = result['input']
    pdf = convert(pptx, directory/'render', 'pdf', directory/'render-profile')
    import fitz
    previews = []
    with fitz.open(pdf) as rendered:
        if len(rendered) != pair['pages']:raise ValueError('interview_render_page_count_mismatch')
        for index, page in enumerate(rendered):
            path = directory/'render'/f'slide-{index+1:03d}.png'
            page.get_pixmap(matrix=fitz.Matrix(1.1, 1.1), alpha=False).save(path)
            previews.append({'page': index+1, 'name': str(path.relative_to(directory)), 'sha256': sha(path)})
    return {'schema': 'assessment-interview-artifacts/1', 'report_kind': 'interview', 'run': deepcopy(data['run']),
        'workflow': deepcopy(data['context']['assessment_workflow']), 'analysis_sha256': digest(result),
        'template_sha256': sha(report.TEMPLATE), 'renderer_sha256': sha(Path(report.__file__)),
        'artifacts': [{'role': role, 'name': str(path.relative_to(directory)), 'sha256': sha(path), 'bytes': path.stat().st_size}
            for role, path in [('asis_report_pptx', pptx), ('asis_backdata_xlsx', xlsx), ('asis_render_pdf', pdf)]],
        'render': {'source_pptx_sha256': sha(pptx), 'pdf_sha256': sha(pdf), 'page_count': pair['pages'], 'previews': previews},
        'quality_status': 'content_review_passed', 'model_call_performed': True,'summary_layout':pair['summary_layout'],
        'generated_at': datetime.now(timezone.utc).isoformat()}


class SummaryRepairValidationError(ValueError):
    def __init__(self,code,raw,expected,where='/raw_response',quote=None,detail=None):
        super().__init__(code)
        self.feedback={'where':where,'quote':quote if quote else (raw or '(empty response)'),
            'why':code,'fix':'기대 문항 ID를 각각 한 번씩 모두 출력하고 해당 원문의 연속 인용과 요약만 제공한다. 원문·문항·점수는 바꾸지 않는다.',
            'expected_question_ids':list(expected),'detail':detail or {}}


def summary_repair_schema(items):
    ids=[i['question_id'] for i in items]
    return {'type':'object','additionalProperties':False,'required':['summaries'],
        'properties':{'summaries':{'type':'array','minItems':len(ids),'maxItems':len(ids),
            'items':{'type':'object','additionalProperties':False,'required':['question_id','summary','source_quote'],
                'properties':{'question_id':{'enum':ids},'summary':{'type':'string','minLength':1},
                    'source_quote':{'type':'string','minLength':1}}}}}}


def summary_repair_candidate(items,raw):
    """전체 응답을 검증한 후에만 요약 사본을 반환한다. 중간 원문·점수 변경은 없다."""
    from collections import Counter
    ids=[i['question_id'] for i in items];source={i['question_id']:i for i in items}
    try:value=strict_json(raw.encode('utf-8'))
    except (ValueError,TypeError) as error:raise SummaryRepairValidationError('interview_repair_json_invalid',raw,ids) from error
    try:validate(value,summary_repair_schema(items))
    except ValidationError as error:raise SummaryRepairValidationError('interview_repair_schema_invalid',raw,ids) from error
    fixes=value['summaries'];counts=Counter(f['question_id'] for f in fixes)
    if set(counts)!=set(ids) or any(n!=1 for n in counts.values()):
        detail={'missing':sorted(set(ids)-set(counts)),'foreign':sorted(set(counts)-set(ids)),
            'duplicates':[q for q,n in counts.items() if n!=1]}
        raise SummaryRepairValidationError('interview_repair_question_set_changed',raw,ids,'/summaries',raw,detail)
    for index,fix in enumerate(fixes):
        if fix['source_quote'] not in source[fix['question_id']]['transcript']:
            raise SummaryRepairValidationError('interview_repair_quote_not_in_source',raw,ids,
                '/summaries/'+str(index)+'/source_quote',fix['source_quote'])
    out=deepcopy(items);changed={f['question_id']:f['summary'] for f in fixes}
    for item in out:item['summary']=changed[item['question_id']]
    return out


def restore_topic_candidate(base_items,folder):
    """같은 입력의 승인/직전 보완 후보를 복원한다. 합격 여부는 정상 검토 경로에서 다시 결합한다."""
    folder=Path(folder);base_sha=digest(base_items)
    def immutable(items):
        return [{k:v for k,v in item.items() if k!='summary'} for item in items]
    def compatible(candidate):
        return isinstance(candidate,list) and all(isinstance(i,dict) for i in candidate) and immutable(candidate)==immutable(base_items)
    accepted=folder/'approved-topic.json'
    if accepted.exists():
        value=strict_json(accepted.read_bytes())
        if value.get('base_input_sha256')==base_sha and value.get('model_policy')==policy():
            candidate=value.get('candidate')
            review=value.get('review',{})
            if (value.get('schema')!='interview-approved-topic/1' or not compatible(candidate)
                or value.get('candidate_sha256')!=digest(candidate) or review.get('verdict')!='pass'
                or review.get('content_checked') is not True or review.get('render_checked') is not False
                or review.get('defects')!=[]):
                raise ValueError('interview_approved_topic_binding_invalid')
            return deepcopy(candidate)
        return deepcopy(base_items)
    for path in sorted(folder.glob('author-[0-9]*.json'),key=lambda p:p.stat().st_mtime,reverse=True):
        value=strict_json(path.read_bytes());candidate=value.get('candidate')
        if compatible(candidate):return deepcopy(candidate)
    return deepcopy(base_items)


def topic_checkpoint(path,value):
    """재개 시 같은 경로의 직전 후보·지적·응답을 해시 이력에 보존한다."""
    path=Path(path)
    if path.exists():
        old=path.read_bytes();atomic(path.parent/'checkpoint-history'/(path.stem+'-'+hashlib.sha256(old).hexdigest()+'.json'),old)
    atomic(path,value)


def save_approved_topic(folder,base_items,items,review):
    folder=Path(folder);path=folder/'approved-topic.json'
    if (review.get('verdict')!='pass' or review.get('content_checked') is not True
        or review.get('render_checked') is not False or review.get('defects')!=[]):
        raise ValueError('interview_approved_topic_review_not_passed')
    if ([{k:v for k,v in i.items() if k!='summary'} for i in items]
        !=[{k:v for k,v in i.items() if k!='summary'} for i in base_items]):
        raise ValueError('interview_approved_topic_source_changed')
    if path.exists():
        previous=path.read_bytes();atomic(folder/'approval-history'/(hashlib.sha256(previous).hexdigest()+'.json'),previous)
    atomic(path,{'schema':'interview-approved-topic/1','base_input_sha256':digest(base_items),
        'candidate_sha256':digest(items),'candidate':items,'review':review,'model_policy':policy()})


async def execute(data, folder, *, builder=None, progress=None, revision_context=None, runtime_factory=Runtime, rounds=3):
    """동일 인터뷰 버전의 원문·내용 검토·실렌더 전수 검토를 모두 완료한 파일만 등록한다."""
    from asis_artifacts import sha
    from asis_service import _page_metadata, _page_batch, _relocate_manifest, next_native_round
    folder = Path(folder);emit = progress or (lambda event: None)
    snapshot = data['context']['interview_snapshot'];qs = snapshot['questionnaire']['questions']
    if workflow.unanswered(qs, snapshot['answers']):raise ValueError('interview_answers_incomplete')
    summaries = {};content_reviews = []
    async with runtime_factory(base_directory=folder/'sessions') as runtime:
        async def topic_review(topic):
            base_items = [i for i in interview_items(data) if i['topic'] == topic]
            topic_folder=folder/'content-reviews'/topic
            items=restore_topic_candidate(base_items,topic_folder)
            key = 'interview-topic-'+topic
            session = 'interview-review-'+digest({'run': data['run'], 'topic': topic})[:24]
            previous_review=None;previous_review_source=None;prior_author_response=None;validation_feedback=[]
            repair_state=topic_folder/'repair-state.json'
            if repair_state.exists():
                saved=strict_json(repair_state.read_bytes())
                if saved.get('base_input_sha256')==digest(base_items) and saved.get('candidate_sha256')==digest(items):
                    prior_author_response=saved.get('prior_author_response');validation_feedback=saved.get('validation_feedback',[])
            for attempt in range(1, rounds+1):
                emit({'unit': key, 'phase': 'content_review', 'status': 'running'})
                draft = {'topic': topic, 'source_kind': 'interview_record',
                         'input_classification': data['context'].get('classification'),
                         'requirements': data['context'].get('requirements', ''), 'items': items}
                current_source=digest(draft)
                if previous_review is not None and previous_review_source==current_source:
                    review=previous_review
                else:
                    review = await _review(runtime, draft, topic_folder, session, rounds=rounds,
                                           shared_cache=folder.parent.parent/'review-cache'/'interview-content'/topic)
                    previous_review=review;previous_review_source=current_source
                if review['verdict'] == 'pass':
                    save_approved_topic(topic_folder,base_items,items,review)
                    emit({'unit': key, 'phase': 'content_review', 'status': 'completed'})
                    return items, review
                topic_checkpoint(topic_folder/('defects-'+str(attempt)+'.json'), {'draft': draft, 'review': review})
                if review['verdict'] == 'blocked' or attempt == rounds:raise RuntimeError('interview_content_review_blocked:'+topic)
                author_schema=summary_repair_schema(items)
                raw, receipt = await runtime.ask('author', encoded({'draft': draft, 'review': review,
                    'expected_question_ids':[i['question_id'] for i in items],
                    'prior_author_response':prior_author_response,'author_validation_feedback':validation_feedback,
                                                                  'output_json_schema': author_schema}).decode('utf-8'),
                    session_id='interview-author-'+digest({'run': data['run'], 'topic': topic})[:24],
                    instructions='고객 원문·질문·공식 지침·점수는 바꾸지 않는다. 지적에 따라 각 문항의 보고 요약만 수정한다. '
                    'expected_question_ids의 전체 ID를 각각 정확히 한 번씩 출력한다. 수정 없는 문항도 현재 요약을 그대로 포함한다. '
                    '직전 응답과 검증 지적을 보존하여 같은 세션에서 보완한다. '
                    '출력은 {"summaries":[{"question_id":"입력 ID","summary":"원문에 근거한 요약","source_quote":"해당 고객 원문의 연속 인용"}]} JSON이다.')
                topic_checkpoint(topic_folder/('author-response-'+str(attempt)+'.json'),
                    {'raw_response':raw,'receipt':receipt,'candidate_before_sha256':digest(items)})
                prior_author_response=raw
                try:
                    candidate=summary_repair_candidate(items,raw)
                except SummaryRepairValidationError as error:
                    validation_feedback.append(error.feedback)
                    topic_checkpoint(topic_folder/('author-validation-'+str(attempt)+'.json'),
                        {'candidate_unchanged_sha256':digest(items),'feedback':error.feedback,
                         'author_budget_increased':False,'attempt_consumed':attempt})
                    topic_checkpoint(repair_state,{'base_input_sha256':digest(base_items),'candidate_sha256':digest(items),
                        'prior_author_response':raw,'validation_feedback':validation_feedback})
                    continue
                items=candidate;previous_review=None
                topic_checkpoint(topic_folder/('author-'+str(attempt)+'.json'), {'candidate': items, 'receipt': receipt})
        topics = list(dict.fromkeys(q['topic'] for q in qs))
        completed = await asyncio.gather(*(topic_review(topic) for topic in topics))
        for items, review in completed:
            summaries.update({i['question_id']: i['summary'] for i in items});content_reviews.append(review)
        result = {'schema': 'assessment-interview-analysis/1', 'input': data, 'items': interview_items(data, summaries),
                  'quality_status': 'content_review_passed', 'model_call_performed': True,
                  'analysis_mode': 'frozen_customer_interview', 'content_reviews': content_reviews,
                  'requirements_application': {'items': []}}
        first_native_round=next_native_round(folder)
        for relative_attempt in range(1, rounds+1):
            attempt=first_native_round+relative_attempt-1
            native = folder/'artifact-rounds'/('round-'+str(attempt).zfill(3))
            native.mkdir(exist_ok=False,parents=True)
            emit({'unit': 'native_pair', 'phase': 'native', 'status': 'running'})
            manifest = await asyncio.to_thread(build_native, result, native)
            emit({'unit': 'native_pair', 'phase': 'native', 'status': 'completed'})
            metadata = await asyncio.to_thread(_page_metadata, native, manifest)
            batches = [list(range(start+1, min(start+5, len(metadata)+1))) for start in range(0, len(metadata), 4)]
            async def visual(index, numbers):
                pages, image = await asyncio.to_thread(_page_batch, native, manifest, numbers, metadata)
                key = 'interview-visual-'+str(index)
                review = await _review(runtime, {'pages': pages, 'report_kind': 'interview'},
                    folder/'visual'/str(attempt)/str(index), 'interview-visual-'+digest({'run': data['run'], 'batch': index})[:24],
                    image=image, rounds=rounds)
                emit({'unit': key, 'phase': 'visual_review', 'status': 'completed' if review['verdict']=='pass' else 'blocked'})
                return {'pages': numbers, 'review': review, 'image_sha256': hashlib.sha256(image).hexdigest()}
            reviews = await asyncio.gather(*(visual(i, numbers) for i, numbers in enumerate(batches)))
            failures = [r for r in reviews if r['review']['verdict'] != 'pass']
            atomic(folder/'visual'/str(attempt)/'review-index.json', reviews)
            if failures:
                summary_faults=summary_layout_failures(failures,manifest.get('summary_layout',{}).get('page'))
                if summary_faults:
                    atomic(folder/'summary-layout-blocked.json',{'reason':'baseline_summary_requires_layout_repair','attempt':attempt,'defects':summary_faults})
                    raise ValueError('interview_summary_layout_requires_code_repair')
                if any(r['review']['verdict']=='blocked' for r in failures) or relative_attempt == rounds:
                    raise RuntimeError('interview_actual_render_review_blocked')
                result = deepcopy(result);result['layout_policy'] = {'body_max_lines': max(10, 17-relative_attempt*3)}
                result['visual_revision_context'] = {'prior_pptx_sha256': manifest['render']['source_pptx_sha256'],
                    'defects': [d for r in failures for d in r['review']['defects']]}
                continue
            for artifact in manifest['artifacts']:
                if sha(native/artifact['name']) != artifact['sha256']:raise ValueError('interview_artifact_changed_after_review')
            quality = {'content_review_passed': True, 'all_render_pages_reviewed': True,
                'reviewed_pages': len(metadata), 'render_page_count': len(metadata),
                'content_reviews': content_reviews, 'visual_batches': reviews,
                'analysis_sha256': digest(result), 'submission_approved': False}
            final = _relocate_manifest(manifest, native.relative_to(folder).as_posix())
            final.update(quality_status='automated_review_passed', quality_review=quality, submission_approved=False)
            atomic(folder/'artifact-manifest.json', final);atomic(folder/'quality-review.json', quality)
            return result, final
    raise RuntimeError('interview_report_quality_not_completed')
