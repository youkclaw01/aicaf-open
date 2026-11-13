"""선행 현황분석 보고서의 Excel 동결·승인 템플릿·실렌더를 담당한다."""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path

from openpyxl import Workbook, load_workbook
from openpyxl.styles import Alignment, Font, PatternFill
from pptx import Presentation
from pptx.oxml import parse_xml

from asis_model import ASSET, digest, encoded


def check_current_state(result):
    """종합보고서나 고객 진술이 섞인 입력을 현황분석으로 포장하지 않는다."""
    if (result.get('schema') != 'assessment-current-state-analysis/1'
            or len(result.get('stages', [])) != 1 or result['stages'][0]['stage'] != 'area'
            or any(f['id'].startswith('F_IV_') for f in result['input']['facts'])
            or result['input'].get('goals')):
        raise ValueError('current_state_report_scope_mismatch')


def _cell_text(value):
    if isinstance(value, (int, float, bool)):
        return value
    return value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, sort_keys=True)


def _rows(book, title, headers, rows):
    sheet = book.create_sheet(title)
    sheet.append(headers)
    for row in rows:
        values = [_cell_text(v) for v in row]
        parts = max([1]+[(len(v)+31999)//32000 for v in values if isinstance(v, str)])
        for part in range(parts):
            sheet.append([v[part*32000:(part+1)*32000] if isinstance(v,str) and len(v)>32000 else v for v in values])
    sheet.freeze_panes = 'A2'
    sheet.auto_filter.ref = sheet.dimensions
    for row in sheet:
        for cell in row:
            if isinstance(cell.value, str):cell.data_type = 's'  # 원문은 수식으로 실행하지 않되 실측 숫자는 숫자로 보존한다.
            cell.font = Font(name='맑은 고딕', size=10)
            cell.alignment = Alignment(vertical='top', wrap_text=True)
    for cell in sheet[1]:
        cell.fill = PatternFill('solid', fgColor='326B91')
        cell.font = Font(name='맑은 고딕', size=10, bold=True, color='FFFFFF')
    for col in sheet.columns:
        sheet.column_dimensions[col[0].column_letter].width = 28 if col[0].column < 3 else 56
    return sheet


def _source_table(book, title, records):
    """원천 행의 모든 필드를 합집합 열로 보존한다. 영역이나 리소스 개수를 고정하지 않는다."""
    if records is None:records = []
    if not isinstance(records, list) or any(not isinstance(row, dict) for row in records):
        raise ValueError('current_state_source_table_invalid:'+title)
    headers = list(dict.fromkeys(key for row in records for key in row))
    if headers:
        return _rows(book, title, headers, ([row.get(key) for key in headers] for row in records))
    return _rows(book, title, ['수집 범위 확인'], [('이 원천에서 표시할 행이 없습니다. 미수집·수집 실패·범위 내 부재는 수집현황 원장에서 구분해야 합니다.',)])


def freeze_current_state(result, destination):
    """전체 분석을 Excel에 먼저 고정하며 PPT는 이 파일에서 읽은 동일 개정만 사용한다."""
    check_current_state(result)
    destination = Path(destination);destination.parent.mkdir(parents=True, exist_ok=True)
    data = result['input'];book = Workbook();book.remove(book.active)
    _rows(book, '개요', ['항목', '값'], [
        ('보고서', 'AS-IS 현황분석 보고서'), ('프로젝트', data['run']['assessment_id']),
        ('고객', data['context']['customer']), ('실행', data['run']),
        ('분석 해시', digest(result)), ('입력 해시', digest(data)),
        ('범위', '관측 현황·기술적 해석·수집 범위와 한계. 인터뷰·최종 종합·개선과제는 별도 보고서')])
    topology = data['context'].get('source_documents', {}).get('topology', {})
    _source_table(book, '리소스전체현황', topology.get('resources'))
    _source_table(book, '리소스집계', data['context'].get('inventory_summary'))
    _source_table(book, '기준리소스연결', data['context'].get('collection_basis', {}).get('criterionMatches'))
    _source_table(book, '기준현황대조', data['context'].get('criterion_assessments'))
    _rows(book, '수집현황', ['항목', '수집 메타데이터'],
          ((str(index+1), row) for index, row in enumerate(data['context'].get('dataset_register', []))))
    _rows(book, '현황분석', ['영역', '구분', '식별자', '본문', '사실 근거', '공식 기준'],
          ((a['area_id'], kind, ident, text, facts, criteria)
           for a in result['stages'][0]['areas']
           for kind, ident, text, facts, criteria in
           [('제목', a['area_id'], a['headline'], [], [])]
           + [('확인 현황', o['id'], o['text'], o['fact_refs'], o['criterion_refs']) for o in a['observations']]
           + [('기술적 해석', a['implication']['id'], a['implication']['text'],
               a['implication']['fact_refs'], a['implication']['criterion_refs'])]))
    _rows(book, '근거원장', ['사실 식별자', '수집 상태', '출처', '원문 위치', '조각', '전체 값'],
          ((f['id'], f.get('collection_status'), f.get('source'), f.get('source_pointer'), part+1,
            text[start:start+30000]) for f in data['facts']
           for text in [json.dumps(f, ensure_ascii=False, sort_keys=True)]
           for part, start in enumerate(range(0, max(1, len(text)), 30000))))
    _rows(book, '기준원장', ['기준', '원문', '공식 출처', '위치', '조회본 해시'],
          ((c['id'], c['quote'], c['source_url'], c['quote'], c.get('source_snapshot_sha256'))
           for c in data['criteria']))
    _rows(book, '미확인', ['주제', '사유', '필요한 근거', '관련 사실'],
          ((r['topic'], r['reason'], r['required_evidence'], r['fact_refs']) for r in result['stages'][0]['unresolved']))
    if result.get('presentation') is not None:
        from assessment_current_state_presentation import validate_projection
        projection=validate_projection(result)
        _rows(book,'보고서핵심',['영역','묶음','제목','핵심 현황','기술적 의미','판단 한계','원래 관찰','사실 근거','공식 기준','원문 인용'],
            ((a['area_id'],index+1,g['title'],g['current_state'],g['technical_meaning'],g['limitation'],
              g['observation_refs'],g['fact_refs'],g['criterion_refs'],g['source_quotes'])
             for a in projection['areas'] for index,g in enumerate(a['groups'])))
        selection=[]
        for area in projection['areas']:
            for index,group in enumerate(area['groups'],1):
                selection.extend((area['area_id'],ref,'본문 핵심',index,group['technical_meaning']) for ref in group['observation_refs'])
            selection.extend((area['area_id'],row['observation_ref'],'상세 원장 보존','',row['reason']) for row in area['appendix_only'])
        _rows(book,'장표선정원장',['영역','원래 관찰','본문 처리','본문 묶음','선정 또는 통합 사유'],selection)
    frozen = book.create_sheet('_frozen_analysis')
    text = encoded(result).decode('utf-8')
    frozen.cell(1, 1, digest(result))
    for index, start in enumerate(range(0, len(text), 30000), 2):
        cell = frozen.cell(index, 1, text[start:start+30000]);cell.data_type = 's'
    frozen.sheet_state = 'veryHidden'
    book.save(destination);book.close()
    return {'schema': 'current-state-excel-freeze/1', 'analysis_sha256': digest(result),
            'xlsx_sha256': hashlib.sha256(destination.read_bytes()).hexdigest()}


def consume_current_state(path, receipt=None):
    """동결 해시와 전체 JSON을 검증하고 Excel에서 복원한다."""
    path = Path(path)
    if receipt and hashlib.sha256(path.read_bytes()).hexdigest() != receipt['xlsx_sha256']:
        raise ValueError('current_state_frozen_xlsx_changed')
    book = load_workbook(path, read_only=True, data_only=False)
    try:
        sheet = book['_frozen_analysis']
        expected = sheet.cell(1, 1).value
        text = ''.join(str(r[0] or '') for r in sheet.iter_rows(min_row=2, values_only=True))
        value = json.loads(text)
        if digest(value) != expected or (receipt and digest(value) != receipt['analysis_sha256']):
            raise ValueError('current_state_frozen_analysis_changed')
        check_current_state(value)
        return value
    finally:
        book.close()


def presentation_current_state(result, destination, *, _measure_only=False):
    """승인 원본의 파트·관계·머리말·푸터를 보존하고 현황 본문만 배치한다."""
    from asis_artifacts import text, sha, TEMPLATE_HASH, CONTENT_HASH, paragraphs_pages, wrapped_lines
    from asis_analysis import AREA_NAMES
    from asis_consolidated_deck import to_polite_korean
    from assessment_current_state_presentation import MAX_SLIDES,group_blocks,validate_projection
    check_current_state(result)
    projection=validate_projection(result) if result.get('presentation') is not None else None
    core_mode=projection is not None or _measure_only
    if sha(ASSET/'visual-v18.pptx') != TEMPLATE_HASH or sha(ASSET/'content-asis-e.pptx') != CONTENT_HASH:
        raise ValueError('approved_source_binding_failed')
    deck = Presentation(ASSET/'content-asis-e.pptx');donors = list(deck.slides)
    for sid in list(deck.slides._sldIdLst):
        deck.part.drop_rel(sid.rId);deck.slides._sldIdLst.remove(sid)
    data = result['input'];layout = []
    _is_synthetic = (data.get('context', {}).get('classification') in ('synthetic_unit_test', 'synthetic_unit_test_only', 'archived_test_snapshot')
                     or data.get('run', {}).get('analysis_revision') == 'unit'
                     or str(data.get('run', {}).get('case_id', '')).startswith('unit'))

    def note(slide, record):
        record = {**record, 'report_kind': 'current_state', 'input_sha256': digest(data)}
        frame = slide.notes_slide.notes_text_frame
        if frame is None:
            from xml.sax.saxutils import escape
            payload = escape(json.dumps(record, ensure_ascii=False))
            node = parse_xml('<p:sp xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main" xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"><p:nvSpPr><p:cNvPr id="2" name="Notes body"/><p:cNvSpPr/><p:nvPr><p:ph type="body" idx="1"/></p:nvPr></p:nvSpPr><p:spPr/><p:txBody><a:bodyPr/><a:lstStyle/><a:p><a:r><a:t>'+payload+'</a:t></a:r></a:p></p:txBody></p:sp>')
            slide.notes_slide.shapes._spTree.insert_element_before(node, 'p:extLst')
            frame = slide.notes_slide.notes_text_frame
        if frame is None:
            raise ValueError('current_state_notes_missing')
        frame.text = json.dumps(record, ensure_ascii=False)

    def clone(index, family, title, subtitle):
        if not _is_synthetic:
            title = to_polite_korean(title)
            subtitle = to_polite_korean(subtitle)
        if len(deck.slides)>=MAX_SLIDES:
            raise ValueError('current_state_slide_maximum_exceeded:'+str(MAX_SLIDES))
        donor = donors[index];slide = deck.slides.add_slide(donor.slide_layout)
        for shape in list(slide.shapes):slide.shapes._spTree.remove(shape._element)
        mapping = {}
        for rel in donor.part.rels.values():
            if rel.reltype.endswith(('/notesSlide', '/slideLayout')):continue
            mapping[rel.rId] = slide.part.relate_to(rel.target_ref if rel.is_external else rel.target_part,
                                                  rel.reltype, is_external=rel.is_external)
        for shape in donor.shapes:
            node = parse_xml(shape._element.xml)
            for child in node.iter():
                for key, value in list(child.attrib.items()):
                    if key.startswith('{http://schemas.openxmlformats.org/officeDocument/2006/relationships}') and value in mapping:
                        child.set(key, mapping[value])
            slide.shapes._spTree.insert_element_before(node, 'p:extLst')
        for shape in list(slide.shapes):
            if 2.15 <= shape.top/914400 < 6.99:
                slide.shapes._spTree.remove(shape._element)
        for shape in slide.shapes:
            if not shape.has_text_frame:continue
            top = shape.top/914400
            if top < .60:
                kickers = {
                    'cover': 'AI ASSESSMENT  /  현황 분석 보고서',
                    'executive_summary': 'EXECUTIVE SUMMARY',
                    'scope': 'Ⅱ  /  현황 분석 · 범위',
                    'architecture_overview': 'Ⅱ  /  현황 분석 · 2.2 내부 환경 분석 · 아키텍처',
                    'area_fact': 'Ⅱ  /  현황 분석 · 2.2 내부 환경 분석',
                    'sources': 'SOURCES',
                }
                value = kickers.get(family, 'AS-IS  /  현황분석')
                shape.name = 'approved_kicker:' + family
            else:
                value = (title if .80 <= top <= 1.20 else subtitle if 1.50 <= top <= 1.90 else
                         'AI Assessment · AS-IS 현황분석 보고서' if top > 7 and shape.left/914400 < 10 else
                         '' if top > 7 else None)
            if value is not None:
                runs = [r for p in shape.text_frame.paragraphs for r in p.runs]
                if runs:
                    runs[0].text = value
                    for run in runs[1:]:run.text = ''
                    if .80 <= top <= 1.20 and len(value) > 30:
                        from pptx.util import Pt as _Pt
                        runs[0].font.size = _Pt(18 if len(value) > 48 else 20 if len(value) > 40 else 22)
                else:shape.text_frame.paragraphs[0].add_run().text = value
        layout.append({'family': family, 'title': title, 'page': len(deck.slides), 'donor_page': index+1})
        note(slide, {'family': family, 'report_kind': 'current_state', 'input_sha256': digest(data)})
        return slide

    def blocks(title, values, *, family='area_fact', refs=(), subtitle='관측 사실 · 기술적 해석 · 근거 및 확인 범위'):
        limit = min(17, int(result.get('layout_policy', {}).get('body_max_lines', 17)))
        body_pt=13.5 if core_mode else 11.7
        polite_values = list(values) if _is_synthetic else [(to_polite_korean(k), to_polite_korean(v)) for k, v in values]
        for number, page in enumerate(paragraphs_pages(polite_values, width=11.96, pt=body_pt, max_lines=limit)):
            slide = clone(7, family, title+(' · 계속' if number else ''), subtitle)
            y = 2.30
            for label, value in page:
                text(slide, label, .72, y, 11.96, .28, 11.2, True, 'primary');y += .34
                lines = wrapped_lines(value, 11.96, body_pt);height = max(.22, len(lines)*.215*body_pt/11.7)
                text(slide, '\n'.join(lines), .72, y, 11.96, height, body_pt);y += height+.15
            if y > 6.90:raise ValueError('current_state_native_capacity')
            note(slide, {'family': family, 'report_kind': 'current_state', 'blocks': page,
                         'observation_refs': list(refs), 'body_font_size':body_pt,'input_sha256': digest(data)})
    slide = clone(0, 'cover', 'AS-IS 현황분석 보고서', '수집 근거에 기반한 현재 환경·구성·운영·비용 분석')
    text(slide, str(data['context']['customer']), .72, 2.50, 11.96, .65, 27, True, 'primary')
    text(slide, '현재 구성과 확인 범위의 분석', .72, 3.60, 11.96, .55, 22)
    text(slide, '인터뷰 보고서와 최종 종합·시사점·개선과제 보고서는 후속 단계에서 별도로 작성', .72, 4.40, 11.96, .90, 18)
    if core_mode:
        summary_areas=projection['areas'] if projection is not None else result['stages'][0]['areas']
        blocks('핵심 현황과 판단 범위를 함께 본다',
            [(AREA_NAMES.get(a['area_id'],a['area_id']),a['headline']) for a in summary_areas],family='executive_summary')
    scope_blocks=[('분석 대상', str(data['context'].get('title') or data['run']['case_id'])),
        ('판단 범위', '수집된 구성·관측·비용과 공식 기준을 대조한다. 실제 업무 사용·책임·성과는 인터뷰 전이므로 확정하지 않는다.'),
        ('수집 상태', '미수집은 없음이 아니며 설정의 존재만으로 실제 운영의 효과를 확정하지 않는다.')]
    if not core_mode:scope_blocks.append(('동일 개정',data['run']['source_bundle_sha256']))
    blocks('분석 범위와 근거',scope_blocks,family='scope')
    from asis_report_visuals import architecture_pages
    def connect(slide, start, end, key):
        from pptx.enum.shapes import MSO_CONNECTOR
        from pptx.util import Inches
        from pptx.dml.color import RGBColor
        shape = slide.shapes.add_connector(MSO_CONNECTOR.STRAIGHT, Inches(start[0]), Inches(start[1]), Inches(end[0]), Inches(end[1]))
        shape.name = 'approved_relation:'+key
        shape.line.color.rgb = RGBColor.from_string('326B91')
        node = shape._element;parent = node.getparent();parent.remove(node);parent.insert(2, node)
    architecture_pages(data, clone, note, connect)
    if projection is not None:
        for area in projection['areas']:
            area_label = AREA_NAMES.get(area['area_id'], area['area_id'])
            for idx_g, group in enumerate(area['groups'], 1):
                blocks(group['title'], group_blocks(group), refs=group['observation_refs'],
                       subtitle=f"{area_label} · 핵심 현황·기술적 의미 및 판단 범위 ({idx_g}/{len(area['groups'])})")
    elif not _measure_only:
        for area in result['stages'][0]['areas']:
            title = AREA_NAMES.get(area['area_id'], area['area_id'])+' · 현황분석'
            values = [('영역 현황', area['headline'])]
            values += [(row['id']+' · 확인 현황', row['text']) for row in area['observations']]
            values += [('기술적 해석', area['implication']['text'])]
            blocks(title, values, refs=[r['id'] for r in area['observations']])
    if core_mode and result['stages'][0]['unresolved']:
        blocks('판단 공백과 상세 자료를 구분한다',[
            ('본문의 판단 한계','핵심 현황의 해석에 직접 영향을 주는 미확인은 해당 장표에 함께 표시한다. 미확인 자체를 결함이나 구성 부재로 확정하지 않는다.'),
            ('전체 원장','모든 관찰·기술적 해석·미확인·공식 기준과 수집 원문은 동일 개정 Excel에 보존한다. 일반 설명과 중복 인벤토리 상세는 본문에서 통합하되 선정 사유를 원장에 남긴다.')],family='scope')
    elif result['stages'][0]['unresolved']:
        blocks('확인되지 않은 사항과 필요한 자료', [(r['topic'], r['reason']+'\n확인 자료: '+r['required_evidence'])
                                                    for r in result['stages'][0]['unresolved']], family='scope')
    cited = {cid for a in result['stages'][0]['areas'] for row in a['observations'] for cid in row['criterion_refs']}
    cited |= {cid for a in result['stages'][0]['areas'] for cid in a['implication']['criterion_refs']}
    if core_mode:
        by_source={}
        for criterion in data['criteria']:
            if criterion['id'] in cited:by_source.setdefault(criterion['source_url'],[]).append(criterion['id'])
        blocks('공식 기준과 현황 근거를 구분한다',
            [('Microsoft Learn · 공식 원문',url+'\n적용 기준: '+' · '.join(ids)+'\n원문·조회본 해시·현황 연결은 Excel 기준원장에 보존한다.') for url,ids in by_source.items()],family='sources')
    else:
        blocks('공식 지침과 분석 근거', [(c['id'], c['quote']+'\n'+c['source_url'])
                                      for c in data['criteria'] if c['id'] in cited], family='sources')
    if _measure_only:return len(deck.slides)
    from pptx.enum.text import PP_ALIGN
    for index, slide in enumerate(deck.slides, 1):
        text(slide, f'{index:02d} / {len(deck.slides):02d}', 11.75, 7.11, .96, .22, 9, False, 'muted', PP_ALIGN.RIGHT)
    deck.core_properties.subject = data['run']['source_bundle_sha256']
    deck.core_properties.title = 'AS-IS 현황분석 보고서'
    from pptx.opc.packuri import PackURI
    for index, part in enumerate([p for p in deck.part.package.iter_parts() if str(p.partname).startswith('/ppt/media/')], 1):
        part.partname = PackURI('/ppt/media/current-state-media-'+str(index)+Path(str(part.partname)).suffix)
    destination = Path(destination);destination.parent.mkdir(parents=True, exist_ok=True)
    deck.save(destination)
    destination.with_suffix('.layout.json').write_text(json.dumps({'content_template_sha256': CONTENT_HASH,
        'visual_template_sha256': TEMPLATE_HASH, 'pages': layout, 'actual_render_verified': False}, ensure_ascii=False), encoding='utf-8')
    return len(deck.slides)


def verify_current_state(result, pptx, xlsx):
    """동결 Excel 전체 분석과 PPT의 관찰·입력 근거가 동일한지 대조한다."""
    frozen = consume_current_state(xlsx)
    if frozen != result:raise ValueError('current_state_excel_candidate_mismatch')
    expected = {}
    for area in result['stages'][0]['areas']:
        expected[area['area_id']] = (area['area_id'], '제목', area['headline'])
        for observation in area['observations']:
            expected[observation['id']] = (area['area_id'], '확인 현황', observation['text'])
        expected[area['implication']['id']] = (area['area_id'], '기술적 해석', area['implication']['text'])
    book = load_workbook(xlsx, read_only=True, data_only=False)
    try:
        visible = {}
        for row in book['현황분석'].iter_rows(min_row=2, values_only=True):
            if row[2] not in expected:raise ValueError('current_state_visible_excel_unknown_row')
            area_id, kind, value = expected[row[2]]
            if row[0] != area_id or row[1] != kind:raise ValueError('current_state_visible_excel_classification_changed')
            visible.setdefault(row[2], []).append(str(row[3] or ''))
        if set(visible) != set(expected) or any(''.join(visible[key]) != row[2] for key,row in expected.items()):
            raise ValueError('current_state_visible_excel_content_changed')
    finally:
        book.close()
    deck = Presentation(pptx)
    from assessment_current_state_presentation import MAX_SLIDES,validate_projection,group_blocks
    if len(deck.slides)>MAX_SLIDES:raise ValueError('current_state_slide_maximum_exceeded:'+str(MAX_SLIDES))
    projection=validate_projection(result) if result.get('presentation') is not None else None
    records = [json.loads(s.notes_slide.notes_text_frame.text) for s in deck.slides]
    compact = lambda text: ''.join(str(text).split())
    displayed_blocks = []
    from asis_artifacts import wrapped_lines
    layout_text = lambda text: str(text).replace('\r\n', '\n').replace('\v', '\n')
    for slide, record in zip(deck.slides, records):
        visible_texts = {layout_text(shape.text) for shape in slide.shapes if shape.has_text_frame}
        for label, value in record.get('blocks', []):
            wrapped = '\n'.join(wrapped_lines(value, 11.96, record.get('body_font_size',11.7)))
            if layout_text(label) not in visible_texts or layout_text(wrapped) not in visible_texts:
                raise ValueError('current_state_visible_ppt_differs_from_notes')
            displayed_blocks.append(value)
    body = compact(''.join(displayed_blocks))
    from asis_consolidated_deck import to_polite_korean
    expected_texts=([row[2] for row in expected.values()] if projection is None else
        [a['headline'] for a in projection['areas']]+[value for a in projection['areas'] for g in a['groups'] for _,value in group_blocks(g)])
    if any(compact(value) not in body and compact(to_polite_korean(value)) not in body for value in expected_texts):
        raise ValueError('current_state_visible_ppt_approved_body_missing')
    present = {ref for r in records for ref in r.get('observation_refs', [])}
    all_observations={r['id'] for a in result['stages'][0]['areas'] for r in a['observations']}
    required = (all_observations if projection is None else
        {r for a in projection['areas'] for g in a['groups'] for r in g['observation_refs']})
    if present != required:raise ValueError('current_state_observation_coverage_mismatch')
    if any(r.get('report_kind', 'current_state') != 'current_state' for r in records):
        raise ValueError('current_state_report_kind_mismatch')
    return {'passed': True, 'same_frozen_analysis': True, 'observations': len(all_observations),
            'full_excel_observations':len(all_observations),'selected_observations':len(required),
            'pages': len(deck.slides), 'analysis_sha256': digest(result)}


def build_current_state(result, directory):
    """승인 분석→Excel 동결→PPT→실제 PDF·장표 이미지까지만 만든다. 시각 승인은 호출자가 수행한다."""
    from asis_artifacts import sha, TEMPLATE_HASH, CONTENT_HASH, convert
    from asis_tasks import active_task_manager
    directory = Path(directory);directory.mkdir(parents=True, exist_ok=True)
    manager = active_task_manager.get()
    def run(key, operation):
        return manager.perform_sync(key, operation) if manager is not None else operation()
    xlsx = directory/'ASIS-Current-State.xlsx';pptx = directory/'ASIS-Current-State.pptx'
    receipt = run('workbook-freeze', lambda: freeze_current_state(result, xlsx))
    frozen = consume_current_state(xlsx, receipt)
    (directory/'input.json').write_bytes(encoded(frozen['input']))
    (directory/'analysis.json').write_bytes(encoded(frozen))
    pages = run('native-presentation', lambda: presentation_current_state(frozen, pptx))
    pdf = convert(pptx, directory/'render', 'pdf', directory/'render-profile')
    import fitz
    previews = []
    with fitz.open(pdf) as rendered:
        if len(rendered) != pages:raise ValueError('current_state_render_page_count_mismatch')
        for index, page in enumerate(rendered):
            path = directory/'render'/f'slide-{index+1:03d}.png'
            page.get_pixmap(matrix=fitz.Matrix(1.1, 1.1), alpha=False).save(path)
            previews.append({'name': str(path.relative_to(directory)), 'sha256': sha(path), 'page': index+1})
    return {'schema': 'ai-assessment-asis-artifacts/r2', 'report_kind': 'current_state',
        'run': deepcopy(frozen['input']['run']), 'input_sha256': digest(frozen['input']), 'analysis_sha256': digest(frozen),
        'template_sha256': TEMPLATE_HASH, 'content_template_sha256': CONTENT_HASH,
        'artifacts': [{'role': role, 'name': str(path.relative_to(directory)), 'sha256': sha(path), 'bytes': path.stat().st_size}
                      for role, path in [('asis_report_pptx', pptx), ('asis_backdata_xlsx', xlsx), ('asis_render_pdf', pdf)]],
        'render': {'source_pptx_sha256': sha(pptx), 'pdf_sha256': sha(pdf), 'page_count': pages, 'previews': previews},
        'quality_status': result['quality_status'], 'model_call_performed': result['model_call_performed'],
        'classification': frozen['input']['context']['classification'], 'current_operations_verified': False,
        'generated_at': datetime.now(timezone.utc).isoformat()}
