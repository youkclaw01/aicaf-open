"""동일 r2 원장의 편집 가능한 PPT·현황 XLSX·실제 렌더 생성. 템플릿 입력과 산출물 해시를 결합한다."""
from __future__ import annotations

from copy import deepcopy
from datetime import datetime, timezone
import hashlib
import io
import json
import math
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import zipfile

from lxml import etree
from openpyxl import Workbook, load_workbook
from openpyxl.styles import Alignment, Font, PatternFill
from pptx import Presentation
from pptx.dml.color import RGBColor
from pptx.enum.shapes import MSO_SHAPE, MSO_CONNECTOR
from pptx.enum.text import MSO_ANCHOR, PP_ALIGN
from pptx.enum.dml import MSO_LINE_DASH_STYLE
from pptx.util import Inches, Pt

from asis_model import ASSET, encoded
from asis_analysis import AREA_NAMES

COLOR = {'primary':'326B91','ink':'232D2B','muted':'596660','light':'EDF3F8','panel':'F5F7F6','white':'FFFFFF'}
FONT = 'Malgun Gothic'
TEMPLATE_HASH = '802f9b80e0dfa3e9b8f51363d129b791d50793513d7fb7dd6ffd3a21a798a539'
CONTENT_HASH = 'af1316a8e490ecd04bcd68c33d2b9b67cf579214731e2a91a3fb85ec8fae8829'


def sha(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def soffice():
    """현재 설치된 렌더러만 사용한다. 없으면 설치·임의 렌더 대체 없이 실패한다."""
    found = shutil.which('soffice')
    if found:
        return found
    for key in ('ProgramFiles','ProgramFiles(x86)'):
        base = os.environ.get(key)
        if base:
            path = Path(base)/'LibreOffice/program/soffice.exe'
            if path.is_file():
                return str(path)
    raise RuntimeError('actual_office_renderer_unavailable')


def convert(path, output_dir, extension, profile):
    import platform
    if platform.system()=='Windows':
        from asis_office import convert as native_convert
        return native_convert(path,output_dir,extension)
    Path(output_dir).mkdir(parents=True, exist_ok=True)
    export_filter = 'pdf:impress_pdf_Export:{"UseTaggedPDF":{"type":"boolean","value":"false"}}' if extension == 'pdf' else extension
    proc = subprocess.run([soffice(), '-env:UserInstallation='+Path(profile).resolve().as_uri(), '--headless',
                           '--convert-to', export_filter, '--outdir', str(output_dir), str(path)],
                          capture_output=True, text=True, encoding='utf-8', errors='replace', timeout=180)
    output = Path(output_dir)/(Path(path).stem+'.'+extension)
    if proc.returncode or not output.is_file() or not output.stat().st_size:
        raise RuntimeError('office_conversion_failed:'+proc.stdout[-500:]+proc.stderr[-500:])
    return output


def literal(value):
    """원문을 변경하지 않는다. 수식 비승격은 원장 셀 문자열형으로 보장한다."""
    text = json.dumps(value, ensure_ascii=False, allow_nan=False) if isinstance(value,(dict,list)) else '' if value is None else str(value)
    return text


def workbook(result, destination):
    data=result['input']; run=data['run']; stages=result['stages']
    basic=data.get('context',{}).get('collection_basis_workbook')
    if basic:
        from io import BytesIO
        from asis_collection_basis import workbook_bytes
        wb=load_workbook(BytesIO(workbook_bytes(basic)))
        if '실행 정보' in wb.sheetnames:raise ValueError('collection_basis_sheet_collision')
        ws=wb.create_sheet('실행 정보',0)
    else:
        wb=Workbook(); ws=wb.active; ws.title='실행 정보'
    ws.append(['구분','값'])
    ws.append(['Assessment',run['assessment_id']]); ws.append(['Case',run['case_id']]); ws.append(['유형',run['track']])
    ws.append(['입력 SHA-256',run['source_bundle_sha256']]); ws.append(['분석 개정',run['analysis_revision']])
    ws.append(['사실원장 행 수',f"=COUNTA('사실원장'!A7:A{len(data['facts'])+6})"])
    action_rows=stages[2]['actions']
    ws.append(['개선/확인 과제 수',f"=COUNTA('개선과제'!A7:A{len(action_rows)+6})"])
    ws.append(['품질 검토 상태',result['quality_status']]); ws.append(['자료 분류',data['context']['classification']])
    ws.append(['운영 현재성','미확인'])
    goals=data.get('goals',[])
    goal_text='고객 인용(운영 확인과 분리):\n'+'\n'.join(row['quote'] for row in goals) if goals else '검증된 목표 근거 없음'
    ws.append(['고객 목표',goal_text])
    ws.append(['수집 시점(topology)',data['context']['topology_collected_at']]); ws.append(['수집 시점(evidence)',data['context']['evidence_collected_at']])

    sheet_specs=[]
    def sheet(name,headers,rows,widths):
        # 공유 Workbook은 병합 시점에만 변경한다. 준비 작업은 별도 Workbook을 소유한다.
        sheet_specs.append({'name':name,'headers':headers,'rows':[list(row) for row in rows],'widths':widths})

    facts=data['facts']
    sheet('사실원장',['Fact ID','Source','필드 경로','수집 상태','범위 완전성','원문/값'],
          [(f['id'],f['source_id'],f['field_path'],f['collection_status'],f['scope_complete'],f['value']) for f in facts],[36,25,66,22,15,95])
    # 큰 원문은 별도 원장 행으로 분해하여 Excel 셀 길이 한계에서 조용히 사라지는 값을 없앤다.
    rawrows=sheet_specs[-1]['rows']
    expanded=[]
    for idx,fact in enumerate(facts):
        text=literal(fact['value'])
        if len(text)>30000:
            rawrows[idx][5]='전체 값은 원문 상세의 동일 Fact ID 행들에 보존됩니다.'
            chunks=[text[i:i+28000] for i in range(0,len(text),28000)]
            expanded.extend((fact['id'],i+1,len(chunks),part) for i,part in enumerate(chunks))
    sheet('원문 상세',['Fact ID','분할 순번','전체 분할 수','원문 조각'],expanded,[36,15,18,110])
    sheet('측정원장',['Measurement ID','값','단위','Fact 참조'],[(m['id'],m['value'],m['unit'],m['fact_refs']) for m in data['measurements']],[46,20,25,80])
    sheet('영역 분석',['영역','관찰/시사점 ID','내용','영향','변경/추가 방향','Fact 참조','원칙 참조'],
          [(AREA_NAMES[a['area_id']],o['id'],o['text'],'','',o['fact_refs'],o['criterion_refs']) for a in stages[0]['areas'] for o in a['observations']]+
          [(AREA_NAMES[a['area_id']],a['implication']['id'],a['implication']['text'],a['implication']['impact'],a['implication']['change_direction'],a['implication']['fact_refs'],a['implication']['criterion_refs']) for a in stages[0]['areas']],
          [20,30,100,75,85,95,35])
    sheet('종합 시사점',['Direction ID','제목','내용','고객 영향','변경 방향','영역 참조','Fact 참조','원칙 참조'],
          [(d['id'],d['title'],d['text'],d['customer_impact'],d['change_direction'],d['area_implication_refs'],d['fact_refs'],d['criterion_refs']) for d in stages[1]['directions']],[22,45,100,75,80,35,95,35])
    sheet('진단',['Diagnosis ID','진단','영향','Direction 참조','Fact 참조','원칙 참조'],
          [(d['id'],d['text'],d['impact'],d['direction_refs'],d['fact_refs'],d['criterion_refs']) for d in stages[2]['diagnoses']],[22,110,85,30,95,35])
    sheet('개선과제',['Action ID','제목','대상','변경 유형','근거/방향','우선순위','우선순위 근거','선행조건','확인 완료 조건','진단 참조','Fact 참조','원칙 참조','산출물','개선·확인 사항','Direction 참조','검증된 목표 참조'],
          [(a['id'],a['title'],a['target'],a['change_type'],a['rationale'],a['priority_level'],a['priority_basis'],a['prerequisites'],a['verification_condition'],a['diagnosis_refs'],a['fact_refs'],a['criterion_refs'],a['deliverable'],a['improvement_points'],a['direction_refs'],a['goal_refs']) for a in action_rows],[22,45,65,15,95,18,95,45,110,30,95,35,85,95,40,35])
    # 통합 진단·과제가 합친 통합 전 후보(영역별 계획 단계의 진단·과제). PPT는 통합 항목만 담고 상세·원본은 이 시트들에 있다.
    trace = data['context'].get('action_item_plan', {}).get('consolidation_trace') or {'diagnoses': [], 'actions': []}
    final_diagnosis_ids = [row['id'] for row in stages[2]['diagnoses']]
    final_action_ids = [row['id'] for row in stages[2]['actions']]
    sheet('통합 진단 원본',['최종 Diagnosis ID','통합 주제','영역','통합 전 진단 범위','통합 전 진단 이유','이슈 참조'],
          [(final_diagnosis_ids[n] if n < len(final_diagnosis_ids) else '', topic['title'], AREA_NAMES.get(topic['area_id'], topic['area_id']), row['scope'], row['reason'], row['issue_refs'])
           for n, topic in enumerate(trace['diagnoses']) for row in topic['origin']],[24,45,20,70,90,45])
    sheet('통합 과제 원본',['최종 Action ID','통합 과제','통합 전 과제 범위','통합 전 과제 이유','이슈 참조'],
          [(final_action_ids[n] if n < len(final_action_ids) else '', action['title'], row['scope'], row['reason'], row['issue_refs'])
           for n, action in enumerate(trace['actions']) for row in action['origin']],[24,45,70,90,45])
    sheet('근거 연결',['From ID','To ID','관계'],[(e['from_id'],e['to_id'],e['relation']) for s in stages for e in s.get('edges',[])],[45,30,22])
    configuration_graphs=[configuration_relationships(data,area) for area in ('network','ai')]
    sheet('구성 연결',['Edge ID','영역','관계','From Resource ID','To Resource ID','Fact 참조','원문 경로','대상 구성 수집','실제 동작 검증'],
          [(edge['id'],AREA_NAMES[graph['area_id']],edge['relation'],edge['from_id'],edge['to_id'],edge['fact_refs'],edge['provenance'],edge['target_collected'],'미검증')
           for graph in configuration_graphs for edge in graph['edges']],[30,20,30,95,95,45,110,20,25])
    sheet('미확인',['범위','보류 이유','추가 확인 근거','Fact 참조'],[(u['topic'],u['reason'],u['required_evidence'],u['fact_refs']) for u in result['unresolved']],[30,90,110,95])
    sheet('적용 원칙',['원칙 ID','공식 원문','출처','출처 SHA-256','확인 시점','권고 강도'],[(c['id'],c['quote'],c['source_url'],c['source_snapshot_sha256'],c['checked_at'],c['modality']) for c in data['criteria']],[28,95,85,72,36,18])
    sheet('입력 바인딩',['파일','SHA-256','Bytes'],[(n,v['sha256'],v['bytes']) for n,v in data['context']['input_files'].items()],[35,75,20])
    requirement_items=result.get('requirements_application',{}).get('items',[])
    sheet('추가 요구 반영',['요구 ID','요구 원문','영역 참조','반영 상태','해석·한계','과제 참조'],
          [(item['id'],item['quote'],item['area_refs'],item['status'],item['interpretation'],item['action_refs']) for item in requirement_items],[24,110,35,38,100,45])
    register=data['context'].get('dataset_register',[])
    sheet('분류 인덱스',['Dataset ID','분석 영역','원천','원문 경로','관측 행수','원천 상태','정규화 상태','범위 완전성','Fact ID','값 SHA-256','수집 메타데이터'],
          [(r['id'],r['area_refs'],r['source_id'],r['pointer'],r['observed_records'],r['source_status'],r['collection_status'],r['scope_complete'],r['fact_id'],r['value_sha256'],r['metadata']) for r in register],[28,30,30,75,16,22,22,18,40,70,100])
    def resolve(doc,pointer):
        for key in pointer.lstrip('/').split('/'):
            doc=doc[int(key)] if isinstance(doc,list) else doc[key]
        return doc
    documents=data['context'].get('source_documents',{})
    classified=[]
    for r in register:
        source_key='topology' if r['source_id']=='topology.json' else 'evidence'
        value=resolve(documents[source_key],r['pointer'])
        for ordinal,record in enumerate(value if isinstance(value,list) else [],1):
            rendered=literal(record);chunks=[rendered[i:i+28000] for i in range(0,len(rendered),28000)] or ['']
            classified.extend((r['id'],r['area_refs'],ordinal,i+1,len(chunks),r['pointer']+'/'+str(ordinal-1),chunk) for i,chunk in enumerate(chunks))
    sheet('현황 분류',['Dataset ID','분석 영역','원문 행순번','분할 순번','분할 수','원문 위치','구성·관측 원문'],classified,[28,30,16,16,16,80,110])
    sources=[]
    for name,doc in documents.items():
        raw=literal(doc);parts=[raw[i:i+28000] for i in range(0,len(raw),28000)]
        sources.extend((name,hashlib.sha256(encoded(doc)).hexdigest(),i+1,len(parts),part) for i,part in enumerate(parts))
    sheet('원천 원문',['원천','정규 JSON SHA-256','분할 순번','분할 수','원문 조각'],sources,[35,72,16,16,110])
    assessments=data['context'].get('criterion_assessments',[])
    sheet('기준 대조',['판단 ID','원칙 ID','원문 ID','분석 영역','적용 조건','판정','적용성 확인','판단·한계','필수 근거','Fact 참조','근거 상태','공식 원문','URL','원문 SHA-256','권고 강도'],
          [(r['id'],r['criterion_id'],r['original_id'],r['area_refs'],r['application_condition'],r['status'],r['applicability_confirmed'],r['conclusion'],r['required_evidence'],r['fact_refs'],r['evidence_states'],r['quote'],r['source_url'],r['source_snapshot_sha256'],r['modality']) for r in assessments],[28,28,22,25,75,20,18,110,100,90,100,100,90,70,18])
    cost_rows=data['context'].get('cost_summary',[])
    sheet('비용 월별',['기간','비용 기준','통화','금액','원천 행수','범위','분류','시작 UTC','종료 UTC'],
          [(r['period'],r['basis'],r['currency'],r['amount'],r['source_rows'],r['scope'],r['amount_class'],r['from_utc'],r['to_utc']) for r in cost_rows],[24,25,15,25,16,20,35,30,30])
    metrics=data['context'].get('metric_summary',[])
    sheet('사용량 집계',['계정 Resource ID','메트릭','차원','관측점 수','관측 합계','첫 관측 UTC','마지막 관측 UTC','원천 섹션','집계 기준','업무 성공 검증'],
          [(r['resource_id'],r['metric'],r['dimensions'],r['points'],r['total'],r['observed_from'],r['observed_to'],r['source_section'],r['aggregation'],r['business_success_verified']) for r in metrics],[100,40,80,18,24,30,30,35,60,20])
    from asis_tasks import active_task_manager,task
    manager=active_task_manager.get()
    from asis_native import prepare_workbook_sheets,merge_workbook_sheets
    prepared=prepare_workbook_sheets(sheet_specs,manager,destination)
    def merge():
        merge_workbook_sheets(wb,prepared)
        wb.save(destination)
        from asis_analysis_register import write as write_analysis_register
        write_analysis_register(destination,result)
        return sha(destination)
    if manager is not None:
        manager.perform_sync('excel-merge',merge)
    else:merge()
    def recalculate():
        with tempfile.TemporaryDirectory(prefix='asis-recalc-') as tmp:
            folder=Path(tmp); recalculated=convert(destination,folder/'out','xlsx',folder/'profile')
            calculated=load_workbook(recalculated,data_only=True)
            if calculated['실행 정보']['B7'].value!=len(facts) or calculated['실행 정보']['B8'].value!=len(action_rows):
                raise ValueError('backdata_count_cache_mismatch')
            for w in calculated:
                for row in w:
                    for cell in row:
                        if cell.data_type=='e': raise ValueError('backdata_formula_error:'+w.title+':'+cell.coordinate)
            shutil.copy2(recalculated,destination)
        return sha(destination)
    if manager is not None:
        manager.perform_sync('excel-recalculate',recalculate)
    else:recalculate()


def text(slide,value,x,y,w,h,pt=11.2,bold=False,color='ink',align=PP_ALIGN.LEFT):
    shape=slide.shapes.add_textbox(Inches(x),Inches(y),Inches(w),Inches(h)); tf=shape.text_frame
    tf.clear(); tf.word_wrap=True; tf.margin_left=0;tf.margin_right=0;tf.margin_top=0;tf.margin_bottom=0
    tf.vertical_anchor=MSO_ANCHOR.TOP
    for i,line in enumerate(str(value).split('\n')):
        paragraph=tf.paragraphs[0] if i==0 else tf.add_paragraph(); paragraph.alignment=align
        paragraph.space_after=Pt(3); run=paragraph.add_run();run.text=line
        run.font.name=FONT;run.font.size=Pt(pt);run.font.bold=bold;run.font.color.rgb=RGBColor.from_string(COLOR[color])
        # 한글·복합문자도 같은 지정 글꼴로 바인딩한다. latin만 지정하면 LO가 CJK를 별도 대체한다.
        properties=run._r.get_or_add_rPr()
        namespace='http://schemas.openxmlformats.org/drawingml/2006/main'
        for script in ('ea','cs'):
            node=properties.find('{'+namespace+'}'+script)
            if node is None:
                node=etree.SubElement(properties,'{'+namespace+'}'+script)
            node.set('typeface',FONT)
    return shape


def box(slide,x,y,w,h,fill='white',dashed=False):
    shape=slide.shapes.add_shape(MSO_SHAPE.RECTANGLE,Inches(x),Inches(y),Inches(w),Inches(h))
    shape.fill.solid();shape.fill.fore_color.rgb=RGBColor.from_string(COLOR[fill]);shape.line.color.rgb=RGBColor.from_string(COLOR['primary' if not dashed else 'muted']);shape.line.width=Pt(.75)
    if dashed:
        from pptx.enum.dml import MSO_LINE_DASH_STYLE
        shape.line.dash_style=MSO_LINE_DASH_STYLE.DASH
    return shape


def wrapped_lines(value,width,pt):
    """한글 전각/라틴 혼합 폭을 이용한 보수적 사전 배치. 실제 렌더 검증과 분리한다."""
    import re
    budget=width*72/pt
    lines=[]
    for paragraph in str(value).split('\n'):
        current='';used=0
        for word in paragraph.split(' '):
            advance=sum(1 if ord(c)>0x2E80 else .62 for c in word)+.4
            if advance>budget:
                parts=re.findall(r'[^·]+(?:·|$)',word)
                if len(parts)>1 and all(sum(1 if ord(c)>0x2E80 else .62 for c in p)<=budget for p in parts):
                    for idx,p in enumerate(parts):
                        adv=sum(1 if ord(c)>0x2E80 else .62 for c in p)+(.4 if idx==len(parts)-1 else 0)
                        if current and used+adv>budget:lines.append(current);current=p;used=adv
                        elif current:current+=(' ' if current=='·' else '')+p;used+=adv
                        else:current=p;used=adv
                    continue
                if current:lines.append(current);current='';used=0
                for character in word:
                    step=1 if ord(character)>0x2E80 else .62
                    if current and used+step>budget:lines.append(current);current='';used=0
                    current+=character;used+=step
                continue
            if current and used+advance>budget:
                lines.append(current);current=word;used=advance
            else:
                current=('' if not current else current+' ')+word;used+=advance
        lines.append(current)
    return lines


def paragraphs_pages(items,width=11.96,pt=11.7,max_lines=21):
    pages=[];current=[];count=0
    for label,body in items:
        lines=wrapped_lines(body,width,pt); need=len(lines)+math.ceil(.53/.215)
        if need>max_lines:
            if current:pages.append(current);current=[];count=0
            capacity=max_lines-math.ceil(.53/.215)
            for offset in range(0,len(lines),capacity):pages.append([(label+' · 계속' if offset else label,'\n'.join(lines[offset:offset+capacity]))])
        elif count+need>max_lines:
            pages.append(current);current=[(label,body)];count=need
        else: current.append((label,body));count+=need
    if current:pages.append(current)
    return pages


def observed_region(record, data):
    """리소스 ID가 정확히 같은 수집 행에서만 리전을 보완한다."""
    if record.get('location'):
        return str(record['location'])
    identifier = str(record.get('id') or '').casefold()
    fact = next((item for item in data['facts'] if item['id'] == 'F_T_resources'), None)
    if not identifier or not fact or fact['collection_status'] != 'collected' or not isinstance(fact['value'], list):
        return '리전 미확인'
    locations = {str(row['location']) for row in fact['value'] if isinstance(row, dict)
                 and str(row.get('id') or '').casefold() == identifier and row.get('location')}
    return next(iter(locations)) if len(locations) == 1 else '리전 미확인'


def deployment_detail(record):
    """관측 원문의 구조화 모델·SKU와 기존 평탄화 형식을 함께 보존한다."""
    model = record.get('model') if isinstance(record.get('model'), dict) else {}
    sku = record.get('sku') if isinstance(record.get('sku'), dict) else {}
    name = model.get('name') or record.get('modelName') or '미확인'
    version = model.get('version') or '미확인'
    tier = sku.get('name') or record.get('skuName') or '미확인'
    return '모델 ' + str(name) + ' · 버전 ' + str(version) + ' · SKU ' + str(tier)


def native_diagram_rows(area,data):
    if data.get('context',{}).get('ppt_input_policy')=='analysis_sheets_only':
        recorded=data['context'].get('report_material',{}).get('area_nodes',{})
        if area['area_id'] not in recorded:raise ValueError('analysis_visual_material_missing')
        return recorded[area['area_id']]
    facts={f['id']:f for f in data['facts']}
    def rows(fid):
        f=facts.get(fid);return f['value'] if f and isinstance(f['value'],list) else None
    out=[];key=area['area_id']
    if key=='network':
        values=rows('F_T_subnets')
        for r in values or []:
            out.append((str(r.get('name') or r.get('id') or '이름 미확인'),
                        'NSG '+('참조 있음' if r.get('nsgId') else '참조 값 없음')+' · UDR '+('참조 있음' if r.get('routeTableId') else '참조 값 없음'),False))
    elif key=='compute':
        for r in rows('F_E_virtualMachineOs') or []:
            out.append((str(r.get('id') or '').split('/')[-1],str(r.get('osType') or 'OS 미확인')+' · '+str(r.get('vmSize') or '크기 미확인'),False))
    elif key=='data':
        for r in rows('F_T_dataServiceConfiguration') or []:
            if r.get('type') in ('microsoft.storage/storageaccounts','microsoft.keyvault/vaults'):
                out.append((str(r.get('id') or '').split('/')[-1],str(r.get('publicNetworkAccess') or '공용 접근 필드 미확인'),False))
    elif key=='ai':
        deployments=rows('F_E_aiDeployments') or []
        for r in rows('F_T_foundryAccountConfiguration') or []:
            matching=[d for d in deployments if str(d.get('accountId') or '').lower()==str(r.get('id') or '').lower()]
            out.append((str(r.get('id') or '').split('/')[-1],observed_region(r,data)+' · 연결된 관측 배포 '+str(len(matching)),False))
        for r in deployments:
            out.append((str(r.get('name') or '배포명 미확인'),deployment_detail(r),False))
    elif key=='secgov':
        for fid,label in [('F_T_rbacRoleAssignments','역할 할당'),('F_T_policyAssignments','조회 직접 정책'),('F_E_managementGroupChain','상위 체인')]:
            r=rows(fid);out.append((label,'관측 행 '+str(len(r)) if r is not None else '미확인',r is None))
    elif key=='telemetry':
        for fid,label in [('F_E_aiMetricsTokensDaily','플랫폼 메트릭'),('F_E_diagnosticSettingsExtended','진단 설정'),('F_E_alertRules','경고 규칙')]:
            r=rows(fid);out.append((label,'관측 행 '+str(len(r)) if r is not None else '미수집·미확인',r is None))
    elif key=='quality': out=[('업무 성공·허용 실패','고객 기준 미확인',True),('모델/검색 버전별 비교','평가 근거 미수집',True),('업무별 안전 시험','결과·승인 미확인',True)]
    else: out=[('업무 목표·책임','고객 인용 확인 · 운영 이행 미확인' if data.get('goals') else '검증된 고객 인용 미확인',True),('변경·롤백 조건','승인·시험 미확인',True),('운영 검토·대응','실제 수행 기록 미확인',True)]
    return out or [('대상 구성','미수집 또는 적용성 확인 필요',True)]


def configuration_relationships(data, area_id):
    """수집 ID·명시 참조 필드에서만 구성 연결을 만든다. 이름 유사도와 실제 트래픽은 근거로 사용하지 않는다."""
    if data.get('context',{}).get('ppt_input_policy')=='analysis_sheets_only':
        recorded=data['context'].get('report_material',{}).get('relationships',{})
        if area_id not in recorded:raise ValueError('analysis_visual_material_missing')
        return recorded[area_id]
    facts={f['id']:f for f in data['facts']};nodes={};edges={};unknowns=[]

    def rows(fid):
        fact=facts.get(fid)
        if not fact or fact['collection_status'] not in ('collected','not_present') or not isinstance(fact['value'],list):
            unknowns.append({'fact_ref':fid,'reason':fact['collection_status'] if fact else 'not_collected'})
            return []
        if not fact['scope_complete']:unknowns.append({'fact_ref':fid,'reason':'partial_collection'})
        return [(i,row) for i,row in enumerate(fact['value']) if isinstance(row,dict)]

    def node(identifier,kind,fid,row=None):
        if not isinstance(identifier,str) or not identifier.strip():return None
        key=identifier.casefold();record=row or {}
        if key not in nodes:
            detail=observed_region(record,data) if kind in ('account','service') else str(record.get('location') or '')
            if kind=='deployment':detail=deployment_detail(record)
            elif kind in ('subnet','vnet'):detail=', '.join(str(value) for value in record.get('addressPrefixes',[]) if value)
            elif kind=='endpoint':
                statuses=[str(item['status']) for item in record.get('privateLinkServiceConnections',[])
                          if isinstance(item,dict) and item.get('status')]
                detail='Private Link 승인 상태: '+(', '.join(dict.fromkeys(statuses)) if statuses
                    else str(record.get('connectionState') or '미확인'))
            nodes[key]={'id':identifier,'kind':kind,'label':str(record.get('name') or identifier.rstrip('/').rsplit('/',1)[-1]),'detail':detail,'collected':row is not None,'fact_refs':[fid]}
        else:
            if row is not None:nodes[key]['collected']=True
            if fid not in nodes[key]['fact_refs']:nodes[key]['fact_refs'].append(fid)
        return key

    def edge(source,target,relation,fid,index,field,source_kind,target_kind):
        if not source or not target:
            unknowns.append({'fact_ref':fid,'reason':'explicit_reference_missing','record_index':index,'field':field});return
        sk=node(source,source_kind,fid);tk=node(target,target_kind,fid)
        if sk is None or tk is None:return
        key=(sk,tk,relation)
        provenance={'fact_ref':fid,'source_id':facts[fid]['source_id'],'field_path':facts[fid]['field_path']+'/'+str(index)+'/'+field,'record_index':index,'field':field}
        if key not in edges:
            edges[key]={'id':'E_CFG_'+hashlib.sha256(encoded(key)).hexdigest()[:16],'from_id':nodes[sk]['id'],'to_id':nodes[tk]['id'],'relation':relation,'fact_refs':[fid],'provenance':[provenance],'target_collected':nodes[tk]['collected']}
        elif provenance not in edges[key]['provenance']:edges[key]['provenance'].append(provenance)

    if area_id=='ai':
        for _,row in rows('F_T_foundryAccountConfiguration'):node(row.get('id'),'account','F_T_foundryAccountConfiguration',row)
        deployments=rows('F_E_aiDeployments')
        for _,row in deployments:node(row.get('id'),'deployment','F_E_aiDeployments',row)
        for i,row in deployments:edge(row.get('accountId'),row.get('id'),'account_deployment','F_E_aiDeployments',i,'accountId','account','deployment')
    elif area_id=='network':
        vnets=rows('F_T_virtualNetworks');subnets=rows('F_T_subnets');endpoints=rows('F_T_privateEndpoints')
        for _,row in vnets:node(row.get('id'),'vnet','F_T_virtualNetworks',row)
        for _,row in subnets:node(row.get('id'),'subnet','F_T_subnets',row)
        for _,row in endpoints:node(row.get('id'),'endpoint','F_T_privateEndpoints',row)
        service_refs={str(connection.get('privateLinkServiceId') or '').casefold()
                      for _,row in endpoints for connection in row.get('privateLinkServiceConnections',[])
                      if isinstance(connection,dict) and connection.get('privateLinkServiceId')}
        if service_refs:
            for fid,kind in [('F_T_foundryAccountConfiguration','account'),('F_T_resources','service')]:
                for _,row in rows(fid):
                    if str(row.get('id') or '').casefold() in service_refs:node(row.get('id'),kind,fid,row)
        for _,row in rows('F_T_dnsConfiguration'):
            if str(row.get('type') or '').casefold()=='microsoft.network/privatednszones':node(row.get('id'),'dns_zone','F_T_dnsConfiguration',row)
        referenced_subnets={str(row.get('subnetId') or '').casefold() for _,row in endpoints}
        for i,row in endpoints:edge(row.get('id'),row.get('subnetId'),'endpoint_subnet','F_T_privateEndpoints',i,'subnetId','endpoint','subnet')
        for i,row in endpoints:
            for j,connection in enumerate(row.get('privateLinkServiceConnections',[])):
                if isinstance(connection,dict):
                    edge(row.get('id'),connection.get('privateLinkServiceId'),'endpoint_service','F_T_privateEndpoints',i,
                         'privateLinkServiceConnections/'+str(j)+'/privateLinkServiceId','endpoint','service')
        for i,row in subnets:
            if str(row.get('id') or '').casefold() in referenced_subnets:edge(row.get('id'),row.get('vnetId'),'subnet_vnet','F_T_subnets',i,'vnetId','subnet','vnet')
        for i,row in rows('F_E_privateEndpointDnsZoneGroups'):
            for j,zone in enumerate(row.get('zones') or []):
                if isinstance(zone,dict):edge(row.get('privateEndpointId'),zone.get('privateDnsZoneId'),'endpoint_dns_zone','F_E_privateEndpointDnsZoneGroups',i,'zones/'+str(j)+'/privateDnsZoneId','endpoint','dns_zone')
        for i,row in rows('F_T_privateDnsLinks'):edge(row.get('zoneId'),row.get('vnetId'),'dns_zone_vnet','F_T_privateDnsLinks',i,'vnetId','dns_zone','vnet')
    else:raise ValueError('unsupported_configuration_area')
    return {'schema':'asis-configuration-relationships/1','area_id':area_id,'runtime_verified':False,'nodes':list(nodes.values()),'edges':list(edges.values()),'unknowns':unknowns}


def presentation(result,destination):
    if result['input']['context'].get('classification') not in ('synthetic_unit_test',):
        from asis_native import parallel_presentation
        return parallel_presentation(result,destination)
    template=ASSET/'visual-v18.pptx'; content=ASSET/'content-asis-e.pptx'
    if sha(template)!=TEMPLATE_HASH or sha(content)!=CONTENT_HASH: raise ValueError('asis_template_hash_mismatch')
    deck=Presentation(template)
    for slide_id in list(deck.slides._sldIdLst)[1:]:
        deck.part.drop_rel(slide_id.rId);deck.slides._sldIdLst.remove(slide_id)
    records=[]; data=result['input']; run=data['run']; ctx=data['context']
    policy=result.get('layout_policy',{})
    body_lines=max(6,min(21,int(policy.get('body_max_lines',21))))
    observation_lines=max(6,min(15,int(policy.get('observation_max_lines',15))))
    frozen=f"#{run['assessment_id']} · {run['track']} · {run['analysis_revision']} · 입력 {run['source_bundle_sha256'][:12]}"

    def slide(title,kicker='Ⅱ  /  AS-IS 현황 분석',subtitle='구성 사실 · 해석/제안 · 미확인 구분 / 독립 품질 검토 전 후보'):
        s=deck.slides.add_slide(deck.slide_layouts[0]);s.background.fill.solid();s.background.fill.fore_color.rgb=RGBColor(255,255,255)
        text(s,kicker,.62,.30,10,.25,10,True,'primary')
        text(s,title,.62,.88,12.08,.64,27,True)
        text(s,subtitle,.64,1.64,12,.48,15.3,False,'muted')
        text(s,'AI Assessment · 현황 분석 보고서',.64,7.13,5.1,.18,8.1,False,'muted')
        records.append({'title':title,'slide':s,'texts':[]})
        return s

    def body_pages(title,items,kicker='Ⅱ  /  AS-IS 현황 분석',item_refs=()):
        pages=paragraphs_pages(items,max_lines=body_lines)
        for index,page in enumerate(pages):
            s=slide(title+(' · 계속' if index else ''),kicker); y=2.30
            for label,body in page:
                text(s,label,.72,y,11.96,.30,13.5,True,'primary');y+=.35
                lines=wrapped_lines(body,11.96,11.7); height=max(.24,len(lines)*.215)
                text(s,'\n'.join(lines),.72,y,11.96,height,11.7);y+=height+.18
                records[-1]['texts'].append(body)
            if y>6.90: raise ValueError('asis_content_layout_budget_exceeded')
            records[-1]['item_refs'] = list(item_refs)

    def configuration_pages(area_id):
        """편집 가능한 실제 구성 연결과 ID·필드 근거를 별도 상세 도식으로 보존한다."""
        graph=configuration_relationships(data,area_id)
        if not graph['edges']:return
        nodes={n['id'].casefold():n for n in graph['nodes']};drawn=set()

        def card(s,identifier,x,y,w,h,role):
            row=nodes[identifier.casefold()];lines=wrapped_lines(row['label'],w-.24,10.4)
            required=.22*len(lines)+.27
            if required>h:raise ValueError('configuration_node_layout_budget_exceeded')
            background=box(s,x,y,w,h,'panel' if row['collected'] else 'white',not row['collected'])
            background.name='configuration_node:'+row['id']
            label=text(s,'\n'.join(lines),x+.12,y+.05,w-.24,.22*len(lines),10.4,True,'primary')
            detail=role+' · '+row['detail'] if row['detail'] else role
            if not row['collected']:detail+=' · 참조 대상 구성 미수집'
            text(s,detail,x+.12,y+.09+.22*len(lines),w-.24,.18,10,False,'muted')
            return x,y,w,h,label

        def connect(s,edge,points):
            for index,(start,end) in enumerate(zip(points,points[1:])):
                line=s.shapes.add_connector(MSO_CONNECTOR.STRAIGHT,Inches(start[0]),Inches(start[1]),Inches(end[0]),Inches(end[1]))
                line.name='configuration_edge:'+edge['id']+':'+str(index)
                line.line.color.rgb=RGBColor.from_string(COLOR['primary']);line.line.width=Pt(.75)
                if not edge['target_collected']:line.line.dash_style=MSO_LINE_DASH_STYLE.DASH
                if index==len(points)-2:
                    ns='http://schemas.openxmlformats.org/drawingml/2006/main'
                    arrow=etree.SubElement(next(line._element.iter('{'+ns+'}ln')),'{'+ns+'}tailEnd');arrow.set('type','triangle');arrow.set('w','sm');arrow.set('len','sm')
                element=line._element;parent=element.getparent();parent.remove(element);parent.insert(2,element)
            drawn.add(edge['id'])

        def finish(s,visible):
            unique={e['id']:e for e in visible};used={e[k].casefold() for e in unique.values() for k in ('from_id','to_id')}
            note={**graph,'nodes':[n for n in graph['nodes'] if n['id'].casefold() in used],'edges':list(unique.values())}
            note['display_rule']='동일 구성 ID는 대상별 관계 추적을 위해 반복 표시하며 별도 리소스로 집계하지 않습니다.'
            s.notes_slide.notes_text_frame.text=json.dumps(note,ensure_ascii=False)
            refs=list(dict.fromkeys(fid for e in unique.values() for fid in e['fact_refs']))
            text(s,'구성 참조만 표시 · 실제 DNS 응답/통신 성공 미검증 · 원문 경로: Notes / 현황 Excel 구성 연결',.72,6.50,11.96,.23,10,False,'muted')
            text(s,'Fact: '+', '.join(refs),.72,6.78,11.96,.23,10,False,'muted')

        if policy.get('configuration_force_rows') or any(len(wrapped_lines(row['label'],2.36,10.4))>1 for row in graph['nodes']):
            # 장문은 고정 높이의 다열 도식을 강제하지 않고 같은 관계를 넓은 행으로 분할한다.
            s=None;y=2.75;visible=[]
            for edge in graph['edges']:
                source=nodes[edge['from_id'].casefold()];target=nodes[edge['to_id'].casefold()]
                height=max(.55,.22*max(len(wrapped_lines(source['label'],5.0,10.4)),len(wrapped_lines(target['label'],5.60,10.4)))+.31)
                if height>3.55:raise ValueError('configuration_single_long_label_budget_exceeded')
                if s is None or y+height>6.36:
                    if s is not None:finish(s,visible)
                    s=slide(AREA_NAMES[area_id]+' · 장문 구성관계 상세','Ⅱ / AS-IS 수집 구성관계','명시 참조를 동일 ID로 분할 표시 · 원문 이름은 보존 · 실제 통신 미검증')
                    y=2.75;visible=[]
                left=card(s,edge['from_id'],.72,y,5.24,height,'구성 주체')
                right=card(s,edge['to_id'],6.84,y,5.84,height,'참조 대상')
                connect(s,edge,[(left[0]+left[2],left[1]+left[3]/2),(right[0],right[1]+right[3]/2)])
                visible.append(edge);y+=height+.14
            if s is not None:finish(s,visible)
            return

        if area_id=='ai':
            groups=[]
            for account in (n for n in graph['nodes'] if n['kind']=='account'):
                related=[e for e in graph['edges'] if e['from_id'].casefold()==account['id'].casefold()]
                for start in range(0,len(related),6):groups.append((account,related[start:start+6]))
            s=None;y=2.72;visible=[]
            for account,related in groups:
                height=max(.62,len(related)*.55+(len(related)-1)*.08)
                if s is None or y+height>6.36:
                    if s is not None:finish(s,visible)
                    s=slide(AREA_NAMES[area_id]+' · 계정→배포 구성관계','Ⅱ / AS-IS 수집 구성관계','동일 accountId의 명시 구성 연결 · 실제 업무 사용/모델 호출 성공은 미검증')
                    text(s,'AI 계정',.72,2.26,3.20,.25,11.2,True,'primary');text(s,'관측된 배포',4.68,2.26,8.00,.25,11.2,True,'primary');y=2.72;visible=[]
                left=card(s,account['id'],.72,y+(height-.62)/2,3.20,.62,'계정')
                for index,edge in enumerate(related):
                    target=card(s,edge['to_id'],4.68,y+index*.63,8.00,.55,'배포')
                    connect(s,edge,[(left[0]+left[2],left[1]+left[3]/2),(target[0],target[1]+target[3]/2)]);visible.append(edge)
                y+=height+.16
            if s is not None:finish(s,visible)
        else:
            groups=[]
            for endpoint in (n for n in graph['nodes'] if n['kind']=='endpoint'):
                dns=[e for e in graph['edges'] if e['relation']=='endpoint_dns_zone' and e['from_id'].casefold()==endpoint['id'].casefold()]
                subnet=[e for e in graph['edges'] if e['relation']=='endpoint_subnet' and e['from_id'].casefold()==endpoint['id'].casefold()]
                if not dns and not subnet:continue
                for start in range(0,max(1,len(dns)),3):groups.append((endpoint,dns[start:start+3],subnet))
            s=None;y=2.75;visible=[]
            for endpoint,dns,subnets in groups:
                height=max(1.34,len(dns)*.52+max(0,len(dns)-1)*.10)
                if s is None or y+height>6.36:
                    if s is not None:finish(s,visible)
                    s=slide(AREA_NAMES[area_id]+' · Endpoint→DNS・VNet 구성관계','Ⅱ / AS-IS 수집 구성관계','수집 ID로 확인한 구성 참조 · 동일 DNS/VNet은 대상별 추적을 위해 반복 표시')
                    for caption,x,w in [('Private Endpoint',.72,2.60),('Private DNS zone',3.77,4.60),('서브넷 / VNet',8.93,3.75)]:text(s,caption,x,2.26,w,.25,11.2,True,'primary')
                    y=2.75;visible=[]
                left=card(s,endpoint['id'],.72,y+(height-.62)/2,2.60,.62,'Endpoint')
                dns_positions=[]
                for index,edge in enumerate(dns):
                    middle=card(s,edge['to_id'],3.77,y+index*.62,4.60,.52,'DNS zone')
                    connect(s,edge,[(left[0]+left[2],left[1]+left[3]/2),(middle[0],middle[1]+middle[3]/2)]);visible.append(edge);dns_positions.append((edge,middle))
                if not dns:text(s,'Endpoint→DNS zone 참조 미확인',3.77,y+.50,4.60,.40,11.2,False,'muted')
                subnet_positions={};vnet_positions={}
                for edge in subnets:
                    right=card(s,edge['to_id'],8.93,y,3.75,.62,'서브넷');subnet_positions[edge['to_id'].casefold()]=right
                    connect(s,edge,[(left[0]+left[2],left[1]+left[3]/2),(3.53,left[1]+left[3]/2),(3.53,y-.10),(8.73,y-.10),(8.73,y+.31),(8.93,y+.31)]);visible.append(edge)
                    for parent in graph['edges']:
                        if parent['relation']=='subnet_vnet' and parent['from_id'].casefold()==edge['to_id'].casefold():
                            target=card(s,parent['to_id'],8.93,y+.78,3.75,.55,'VNet');vnet_positions[parent['to_id'].casefold()]=target
                            connect(s,parent,[(right[0]+right[2]/2,right[1]+right[3]),(target[0]+target[2]/2,target[1])]);visible.append(parent)
                for edge,middle in dns_positions:
                    for link in graph['edges']:
                        if link['relation']=='dns_zone_vnet' and link['from_id'].casefold()==edge['to_id'].casefold():
                            target=vnet_positions.get(link['to_id'].casefold())
                            if target is None:
                                # 같은 위치에 다른 VNet을 덮지 않고 아래의 미표시 관계 페이지로 전달한다.
                                continue
                            connect(s,link,[(middle[0]+middle[2],middle[1]+middle[3]/2),(target[0],target[1]+target[3]/2)]);visible.append(link)
                y+=height+.24
            if s is not None:finish(s,visible)
        # 대상별 페이지에서 분리한 명시 참조도 동일 ID·근거의 독립 행으로 보존한다.
        remaining=[e for e in graph['edges'] if e['id'] not in drawn]
        for start in range(0,len(remaining),5):
            visible=remaining[start:start+5];s=slide(AREA_NAMES[area_id]+' · 수집 구성관계 상세','Ⅱ / AS-IS 수집 구성관계','수집된 명시 참조 · 범위 밖/미수집 대상은 파선으로 구분 · 실제 트래픽 미검증')
            for index,edge in enumerate(visible):
                y=2.75+index*.70;left=card(s,edge['from_id'],.72,y,4.60,.55,edge['relation']);right=card(s,edge['to_id'],6.20,y,6.48,.55,'참조 대상')
                connect(s,edge,[(left[0]+left[2],left[1]+left[3]/2),(right[0],right[1]+right[3]/2)])
            finish(s,visible)

    # 원본 표지의 독립 위계·장식·run 스타일을 그대로 보존하고 예시 문구만 교체한다.
    s=deck.slides[0]
    cover_text={0:'AI ASSESSMENT / 현황 분석 보고서',1:'AS-IS 현황 분석',
                2:'관찰 · 적용 판단 · 개선/확인 과제',3:'수집 구성과 업무 검증 공백을 구분한 검토 후보',
                4:'1',5:'관찰',6:'2',7:'판단',8:'3',9:'검증',
                10:str(ctx['customer'])+' / '+str(ctx['topology_collected_at']),
                11:'AI Assessment · 현황 분석 보고서',12:''}
    for index,shape in enumerate(s.shapes):
        if not shape.has_text_frame:continue
        value=cover_text.get(index,'')
        for pi,paragraph in enumerate(shape.text_frame.paragraphs):
            for ri,cover_run in enumerate(paragraph.runs):cover_run.text=value if pi==0 and ri==0 else ''
    records.append({'title':'AS-IS 현황 분석','slide':s,'texts':[]})
    summary=result['stages'][3]['summary']
    body_pages('Executive Summary', [('현황 판단의 범위',summary['message'])]+[(k['id'],k['text']) for k in summary['key_points']],'EXECUTIVE SUMMARY')
    disclosure=('기존 시험프로젝트23/24의 보관 인프라 수집사본이며 최신 실제 고객 운영 자료가 아닙니다. 현재 운영은 검증하지 않았습니다.' if ctx['classification'].startswith('archived_test') else '수집본과 고객 업무의 현재 운영 적용 여부는 별도 확인이 필요합니다.')
    # 동일 범위의 현재성·목표 제한을 묶고 제한 목록은 손실 없이 연속 배치한다. 빈 목표 계속 페이지를 만들지 않는다.
    scope_binding=frozen+'\n수집 시점(topology): '+str(ctx['topology_collected_at'])+'\n수집 시점(evidence): '+str(ctx['evidence_collected_at'])
    scope_limits=' · '.join(ctx['known_limitations']) or '등록된 추가 제한이 없으며 전 범위 완전성을 의미하지 않습니다.'
    body_pages('분석 범위·시점·수집 제한', [('입력과 수집 시점',scope_binding),('자료 현재성·고객 목표',disclosure+' 입력 원문 해시와 수집 시점을 기준으로 해석하며 수집 이후 변경은 확인이 필요합니다.\n검증된 고객 목표와 업무 우선순위는 자동 보충하지 않습니다. 질문 설명·추가 생성 요구·샘플 수치를 고객 답변으로 치환하지 않습니다.'),('알려진 수집 제한',scope_limits)])
    if data.get('goals'):
        interpreted={row['goal_id']:row for row in ctx.get('goal_interpretations',[])}
        goal_blocks=[]
        for goal in data['goals']:
            row=interpreted.get(goal['id'],{})
            explanation='고객 원문: '+goal['quote']+'\n의미 해석: '+row.get('interpretation','의미 해석 근거 확인 필요')+'\n인용 근거: '+goal['source_id']+'\n고객 목표는 운영 확인과 분리하며, 이행·성과·충족을 확정하지 않습니다.'
            if row.get('ambiguity'):explanation+='\n추가 확인: '+row['ambiguity']
            goal_blocks.append((goal['id'],explanation))
        body_pages('고객 목표·제약과 운영 확인 범위',goal_blocks)
    requirement_items=result.get('requirements_application',{}).get('items',[])
    if requirement_items:
        body_pages('추가 요구의 분석 반영과 판단 한계',[(item['id']+' · '+('검토 제약 반영' if item['area_refs'] else '해석·범위 확인 필요'),
                   item['quote']+'\n'+item['interpretation']+'\n대상: '+(', '.join(AREA_NAMES[key] for key in item['area_refs']) or '미확인')) for item in requirement_items])
    for area in result['stages'][0]['areas']:
        key=area['area_id']; nodes=native_diagram_rows(area,data)
        network_graph=configuration_relationships(data,'network') if key=='network' else None
        network_edges=network_graph['edges'] if network_graph else []
        gap=.40 if network_edges else .20
        columns=2 if network_edges or (key=='compute' and len(nodes)>6) else 1
        card_width=(5.60-gap*(columns-1))/columns
        prepared=[]
        if network_edges and network_graph is not None:
            graph_nodes={node['id'].casefold():node for node in network_graph['nodes']}
            kinds={'vnet':'Virtual Network','subnet':'서브넷','endpoint':'Private Endpoint',
                   'account':'AI 계정','service':'Azure 서비스','dns_zone':'Private DNS Zone'}
            for edge in network_edges:
                pair=[graph_nodes[edge[name].casefold()] for name in ('from_id','to_id')]
                row=[(kinds.get(node['kind'],'구성 대상')+' · '+node['label'],node['detail'],not node['collected']) for node in pair]
                prepared.append((row,edge))
        else:
            prepared=[(nodes[start:start+columns],None) for start in range(0,len(nodes),columns)]
        node_chunks=[];current=[];used=0
        for row,edge in prepared:
            height=max(max(.48,.22*len(wrapped_lines(label,card_width-.40,10.4))
                +.17*len(wrapped_lines(detail,card_width-.32,10))+.16) for label,detail,_ in row)
            if height>3.75:raise ValueError('asis_diagram_single_row_layout_budget_exceeded')
            if current and used+height+.09>3.75:node_chunks.append(current);current=[];used=0
            current.append((row,height,edge));used+=height+.09
        if current:node_chunks.append(current)
        obs_pages=paragraphs_pages([(o['id'],o['text']) for o in area['observations']],width=6.0,pt=11.2,max_lines=observation_lines)
        page_count=max(len(node_chunks),len(obs_pages))
        for index in range(page_count):
            s=slide(AREA_NAMES[key]+' · 구성도와 관찰'+(' · 계속' if index else ''))
            text(s,'수집 구성/검증 대상 · 실제 트래픽 도식이 아님',.72,2.30,5.60,.30,11.2,True,'primary')
            y=2.75
            for row,h,edge in node_chunks[min(index,len(node_chunks)-1)]:
                for column,(label,detail,dashed) in enumerate(row):
                    x=.72+column*(card_width+gap)
                    label_lines=wrapped_lines(label,card_width-.40,10.4)
                    detail_lines=wrapped_lines(detail,card_width-.32,10)
                    box(s,x,y,card_width,h,'panel' if not dashed else 'white',dashed)
                    text(s,'\n'.join(label_lines),x+.14,y+.05,card_width-.32,.22*len(label_lines),10.4,True,'primary')
                    text(s,'\n'.join(detail_lines),x+.14,y+.08+.22*len(label_lines),card_width-.32,.17*len(detail_lines),10,False,'muted')
                if edge:
                    connector=s.shapes.add_connector(MSO_CONNECTOR.STRAIGHT,Inches(.72+card_width),Inches(y+h/2),
                        Inches(.72+card_width+gap),Inches(y+h/2))
                    connector.name='area_configuration_edge:'+edge['id']
                    connector.line.color.rgb=RGBColor.from_string(COLOR['primary']);connector.line.width=Pt(.75)
                    ns='http://schemas.openxmlformats.org/drawingml/2006/main'
                    arrow=etree.SubElement(next(connector._element.iter('{'+ns+'}ln')),'{'+ns+'}tailEnd')
                    arrow.set('type','triangle');arrow.set('w','sm');arrow.set('len','sm')
                y+=h+.09
            if y>6.50: raise ValueError('asis_diagram_layout_budget_exceeded')
            y=2.30
            text(s,'Fact Finding',6.65,y,6.0,.30,13.5,True,'primary');y+=.42
            if index<len(obs_pages):
                for label,body in obs_pages[index]:
                    lines=wrapped_lines(body,6.0,11.2);h=len(lines)*.205
                    text(s,label,6.65,y,6.0,.22,10,True,'primary');y+=.25
                    text(s,'\n'.join(lines),6.65,y,6.0,h,11.2);y+=h+.13
                    records[-1]['texts'].append(body)
            refs=list(area['implication']['fact_refs'])
            if network_graph:
                refs=list(dict.fromkeys(refs+[ref for node in network_graph['nodes'] for ref in node['fact_refs']]))
            compact=', '.join(refs[:3])
            text(s,'원장 참조 '+str(len(refs))+'건: '+compact+(' · 전체 목록은 Notes/현황 Excel' if len(refs)>3 else ''),.72,6.67,11.96,.24,10,False,'muted')
            note_frame=s.notes_slide.notes_text_frame
            if note_frame is None:raise ValueError('asis_native_notes_frame_missing')
            note_frame.text='전체 원장 참조: '+', '.join(refs)
            if network_graph:
                note_frame.text=json.dumps({'schema':'asis-area-configuration-overview/1','fact_refs':refs,
                    'configuration_overview':{key:value for key,value in network_graph.items() if key!='schema'}},ensure_ascii=False)
        if key in ('network','ai'):configuration_pages(key)
        implication=area['implication']
        blocks=[('시사점 '+implication['id'],implication['text']),('고객 영향(조건부)',implication['impact']),
                ('변경·추가 방향',implication['change_direction']),
                ('분석상 적용 해설','구성 조회와 실제 동작 효과는 구분하며, 아래 공식 원문의 적용 범위와 권고 수준에 따라 판단합니다.')]
        criteria_by_id={criterion['id']:criterion for criterion in data['criteria']}
        for identifier in implication['criterion_refs']:
            criterion=criteria_by_id[identifier]
            blocks.append(('Microsoft 공식 원문 · '+identifier,criterion['quote']+'\n원문 규범 강도: '
                +criterion['modality']+'\n출처: '+criterion['source_url']))
        body_pages(AREA_NAMES[key]+' · 시사점과 적용 판단',blocks)
    # 종합·진단은 실제 작성 문장을 각각 소비한다. 원장 ID를 제목의 연결 anchor로 유지한다.
    directions=result['stages'][1]['directions']; action_rows=result['stages'][2]['actions']
    diagnoses=result['stages'][2]['diagnoses']
    for direction in directions:
        body_pages(direction['id']+' · 시사점 종합',[(direction['title'],direction['text']),('고객 영향(조건부)',direction['customer_impact']),('변경·대안과 선행조건',direction['change_direction']),('교차영역 판단 근거',', '.join(direction['area_implication_refs']))])
    for diagnosis in diagnoses:
        body_pages(diagnosis['id']+' · 최종 진단',[('구성·갭·미확인 판정',diagnosis['text']),('영향과 판단 한계',diagnosis['impact']),('선행 종합방향',', '.join(diagnosis['direction_refs']))],item_refs=[diagnosis['id']])
    implications={area['implication']['id']:area['implication'] for area in result['stages'][0]['areas']}
    observations={observation['id']:observation for area in result['stages'][0]['areas'] for observation in area['observations']}
    direction_map={row['id']:row for row in directions};diagnosis_map={row['id']:row for row in diagnoses}
    for action in action_rows:
        related_j=[diagnosis_map[identifier] for identifier in action['diagnosis_refs']]
        d_ids=list(dict.fromkeys(identifier for row in related_j for identifier in row['direction_refs']))
        related_d=[direction_map[identifier] for identifier in d_ids]
        i_ids=list(dict.fromkeys(identifier for row in related_d for identifier in row['area_implication_refs']))
        f_ids=list(dict.fromkeys(action['fact_refs']+[identifier for row in related_d for identifier in row['fact_refs']]))
        chunks=[];current=[]
        for identifier in f_ids:
            proposed=current+[identifier]
            linked_o=[row for row in observations.values() if set(row['fact_refs'])&set(proposed)]
            if current and (len(proposed)>(4 if policy.get('trace_edge_rows') else 8) or len(linked_o)>(6 if policy.get('trace_edge_rows') else 14)):chunks.append(current);current=[identifier]
            else:current=proposed
        if current:chunks.append(current)
        for chunk_index,chunk in enumerate(chunks):
            o_ids=[identifier for identifier,row in observations.items() if set(row['fact_refs'])&set(chunk)]
            observation_chunk=6 if policy.get('trace_edge_rows') else 14
            for o_start in range(0,max(1,len(o_ids)),observation_chunk):
                o_chunk=o_ids[o_start:o_start+observation_chunk]
                graph=slide(action['id']+' · 사실→관찰/판단→종합→진단→과제','Ⅱ / 근거 관계 원장',
                            '동일 원장의 실제 참조 · 사실 분할 '+str(chunk_index+1)+'/'+str(len(chunks))+' · 직접 선언되지 않은 O→I는 공통 Fact 참조만 파선')
                columns=[('사실 F',chunk,2.80),('관찰 O',o_chunk,1.65),('적용판단 I',i_ids,1.65),('종합방향 D',d_ids,1.45),('최종진단 J',[row['id'] for row in related_j],1.45),('과제 A',[action['id']],1.30)]
                positions={};x=.66
                for role,identifiers,width in columns:
                    text(graph,role,x,2.20,width,.24,11.2,True,'primary')
                    count=max(1,len(identifiers));height=(3.50-(count-1)*.05)/count
                    for ordinal,identifier in enumerate(identifiers):
                        y=2.61+ordinal*(height+.05)
                        box(graph,x,y,width,height,'panel' if identifier.startswith(('I_','D_')) else 'white')
                        lines=wrapped_lines(identifier,width-.12,10)
                        if len(lines)*.16>height-.02:raise ValueError('asis_trace_node_layout_budget_exceeded')
                        node=text(graph,'\n'.join(lines),x+.06,y+.01,width-.12,height-.02,10,False,'primary')
                        positions[identifier]=(x,y,width,height,node)
                    x+=width+.17
                edges=[]
                for identifier in o_chunk:
                    edges += [(source,identifier,False) for source in observations[identifier]['fact_refs'] if source in chunk]
                for identifier in i_ids:
                    edges += [(source,identifier,False) for source in implications[identifier]['fact_refs'] if source in chunk]
                    # 교집합은 수집된 직접 관계가 아니므로 파선과 명시 라벨만 사용한다.
                    edges += [(source,identifier,True) for source in o_chunk if set(observations[source]['fact_refs'])&set(implications[identifier]['fact_refs'])&set(chunk)]
                for row in related_d:
                    edges += [(source,row['id'],False) for source in row['fact_refs'] if source in chunk]
                    edges += [(source,row['id'],False) for source in row['area_implication_refs']]
                for row in related_j:edges += [(source,row['id'],False) for source in row['direction_refs']]
                edges += [(source,action['id'],False) for source in action['diagnosis_refs']]
                # 여러 열을 건너는 직접 관계는 상단 여백으로 우회하여 다른 근거 노드를 가리지 않는다.
                for source,target,derived in edges:
                    if source not in positions or target not in positions:continue
                    sx,sy,sw,sh,_=positions[source];tx,ty,tw,th,_=positions[target]
                    points=[(sx+sw,sy+sh/2),(tx,ty+th/2)]
                    if tx-(sx+sw)>.20:
                        lane=chunk.index(source) if source in chunk else 0
                        source_x=sx+sw+.055+.008*lane;target_x=tx-.055-.008*lane
                        top_y=2.465+.017*lane
                        points=[points[0],(source_x,points[0][1]),(source_x,top_y),
                                (target_x,top_y),(target_x,points[-1][1]),points[-1]]
                    edge_id=hashlib.sha256((source+'|'+target+'|'+str(derived)).encode('utf-8')).hexdigest()[:16]
                    for segment,(start,end) in enumerate(zip(points,points[1:])):
                        connector=graph.shapes.add_connector(MSO_CONNECTOR.STRAIGHT,Inches(start[0]),Inches(start[1]),Inches(end[0]),Inches(end[1]))
                        connector.name='trace_edge:'+edge_id+':'+str(segment)
                        connector.line.color.rgb=RGBColor.from_string(COLOR['muted' if derived else 'primary']);connector.line.width=Pt(.5 if derived else .75)
                        if derived:connector.line.dash_style=MSO_LINE_DASH_STYLE.DASH
                        if segment==len(points)-2:
                            drawing_namespace='http://schemas.openxmlformats.org/drawingml/2006/main'
                            line_element=next(connector._element.iter('{'+drawing_namespace+'}ln'))
                            arrow=etree.SubElement(line_element,'{'+drawing_namespace+'}tailEnd')
                            arrow.set('type','triangle');arrow.set('w','sm');arrow.set('len','sm')
                        element=connector._element;tree=element.getparent();tree.remove(element);tree.insert(2,element)
                graph.notes_slide.notes_text_frame.text='원장 참조(직접 선언 및 공통 Fact 교집합 파선 구분)\n'+json.dumps({'action_id':action['id'],'edges':edges},ensure_ascii=False)
                records[-1]['item_refs'] = [action['id']] + [row['id'] for row in related_j]
                text(graph,'전체 Fact/관찰/판단 및 다대다 연결은 동일 개정 현황 Excel의 사실원장·영역 분석·근거 연결에서 대조합니다.',.70,6.25,11.96,.32,10,False,'muted')
    for a in action_rows:
        change_labels={'verify':'확인·검증','add':'추가','change':'변경','retain':'유지','remove':'제거'}
        priority_labels={'first':'선행 확인','next':'전제 확인 후','pending':'우선순위 미확인'}
        body_pages(a['id']+' · 개선·확인 과제',[(a['title'],a['target']+' · '+change_labels.get(a['change_type'],a['change_type'])),('판단 근거',a['rationale']),('우선순위와 전제',priority_labels.get(a['priority_level'],a['priority_level'])+' · '+a['priority_basis']),('선행조건', '\n'.join(a['prerequisites'])),('산출물',a['deliverable']),('확인 완료 조건',a['verification_condition']),('구체적 개선·확인 사항','\n'.join(a['improvement_points']))],'Ⅲ / 개선·확인 과제',item_refs=[a['id']])
    body_pages('미확인과 다음 확인 근거',[(u['topic'],u['required_evidence']) for u in result['unresolved']],'별첨 / 판단 한계')
    body_pages('Microsoft 기준과 데이터 근거',[(c['id'],c['quote']+'\n'+c['source_url']) for c in data['criteria']],'별첨 / 공식 원칙과 출처')
    anchors={}
    known_ids={row['id'] for stage in result['stages'] for key in ('directions','diagnoses','actions') for row in stage.get(key,[])}|set(implications)|set(observations)
    for page in records:
        if '근거 관계 원장' in ''.join(shape.text for shape in page['slide'].shapes if shape.has_text_frame):continue
        raw=''.join(shape.text for shape in page['slide'].shapes if shape.has_text_frame)
        for identifier in known_ids:
            if identifier in raw:anchors.setdefault(identifier,page['slide'])
    official_urls={criterion['source_url'] for criterion in data['criteria']}
    for page in records:
        for shape in page['slide'].shapes:
            if not shape.has_text_frame:continue
            compact=''.join(shape.text.split())
            matched_url=next((url for url in official_urls if ''.join(url.split()) in compact),None)
            if matched_url:shape.click_action.hyperlink.address=matched_url
            elif compact in anchors:shape.click_action.target_slide=anchors[compact]
    total=len(deck.slides)
    for i,s in enumerate(deck.slides,1): text(s,f'{i:02d} / {total:02d}',11.75,7.11,.96,.22,9,False,'muted',PP_ALIGN.RIGHT)
    deck.core_properties.title='AI Assessment · AS-IS 현황 검토';deck.core_properties.subject=run['source_bundle_sha256']
    deck.save(destination)
    # 기본 불변식: 샘플 본문·리소스는 사양으로부터 새로 작성되었고 템플릿은 읽기 전용이다.
    restored=Presentation(destination)
    if len(restored.slides)!=total:raise ValueError('native_slide_manifest_mismatch')
    with zipfile.ZipFile(destination) as archive:
        if archive.testzip():raise ValueError('pptx_zip_integrity_failed')
        ns={'a':'http://schemas.openxmlformats.org/drawingml/2006/main'}
        for name in archive.namelist():
            if name.startswith('ppt/slides/slide') and name.endswith('.xml'):
                root=etree.fromstring(archive.read(name))
                for paragraph in root.xpath('//a:p',namespaces=ns):
                    tags=[node.tag.rsplit('}',1)[-1] for node in paragraph]
                    if 'endParaRPr' in tags and any(tag=='r' for tag in tags[tags.index('endParaRPr')+1:]):raise ValueError('pptx_run_after_end_properties')
    Path(destination).with_suffix('.layout.json').write_bytes(encoded({'pages':[
        {'page':i+1,'title':row['title'],'item_refs':row.get('item_refs',[])} for i,row in enumerate(records)],
        'actual_render_verified':False}))
    return total


def verify_pair(result, ppt, xlsx):
    """실제 PPT·Excel을 다시 열어 동일 입력·수치·원문·관계·항목을 원장과 대조한다."""
    data=result['input'];run=data['run'];stages=result['stages']
    if any(stage['run']!=run for stage in stages):raise ValueError('analysis_run_binding_mismatch')
    deck=Presentation(ppt);book=load_workbook(xlsx,data_only=True)
    info=book['실행 정보']
    for cell,value in {'B2':run['assessment_id'],'B3':run['case_id'],'B4':run['track'],
                       'B5':run['source_bundle_sha256'],'B6':run['analysis_revision']}.items():
        if literal(info[cell].value)!=literal(value):raise ValueError('backdata_run_binding_mismatch:'+cell)
    if deck.core_properties.subject!=run['source_bundle_sha256']:raise ValueError('ppt_input_binding_mismatch')
    if info['B7'].value!=len(data['facts']) or info['B8'].value!=len(stages[2]['actions']):
        raise ValueError('backdata_count_cache_mismatch')
    if book['사실원장'].max_row!=max(6,len(data['facts'])+6):raise ValueError('backdata_fact_count_mismatch')
    fragments={}
    for row in book['원문 상세'].iter_rows(min_row=7,values_only=True):
        if row[0] is not None:fragments.setdefault(row[0],[]).append((int(str(row[1])),int(str(row[2])),literal(row[3])))
    for index,fact in enumerate(data['facts'],7):
        values=[book['사실원장'].cell(index,c).value for c in range(1,7)]
        expected=[fact['id'],fact['source_id'],fact['field_path'],fact['collection_status'],fact['scope_complete']]
        if [literal(v) for v in values[:5]]!=[literal(v) for v in expected]:raise ValueError('backdata_fact_binding_mismatch:'+fact['id'])
        expected_value=literal(fact['value'])
        if len(expected_value)>30000:
            parts=sorted(fragments.get(fact['id'],[]))
            if [p[0] for p in parts]!=list(range(1,len(parts)+1)) or any(p[1]!=len(parts) for p in parts):
                raise ValueError('backdata_fact_chunk_binding_mismatch:'+fact['id'])
            actual=''.join(p[2] for p in parts)
        else:actual=literal(values[5])
        if actual!=expected_value:raise ValueError('backdata_fact_value_mismatch:'+fact['id'])
    categories={'종합 시사점':(1,[r['id'] for r in stages[1]['directions']]),
                '진단':(1,[r['id'] for r in stages[2]['diagnoses']]),
                '개선과제':(1,[r['id'] for r in stages[2]['actions']]),
                '영역 분석':(2,[r['id'] for a in stages[0]['areas'] for r in a['observations']]+[a['implication']['id'] for a in stages[0]['areas']])}
    trace=data['context'].get('action_item_plan',{}).get('consolidation_trace')
    if trace:
        # 통합 추적표는 최종 진단·과제와 같은 순서·같은 개수여야 한다(어긋나면 원본 연결이 틀린 것이다).
        if len(trace['diagnoses'])!=len(stages[2]['diagnoses']) or len(trace['actions'])!=len(stages[2]['actions']):raise ValueError('consolidation_trace_count_mismatch')
        for sheet_name,column,expected_ids in (('통합 진단 원본',1,[r['id'] for r in stages[2]['diagnoses']]),('통합 과제 원본',1,[r['id'] for r in stages[2]['actions']])):
            seen=sorted({literal(book[sheet_name].cell(i,column).value) for i in range(7,book[sheet_name].max_row+1) if book[sheet_name].cell(i,column).value})
            if seen!=sorted(expected_ids):raise ValueError('backdata_consolidation_trace_mismatch:'+sheet_name)
    all_ids=[]
    for name,(column,expected_ids) in categories.items():
        actual_ids=[literal(book[name].cell(i,column).value) for i in range(7,book[name].max_row+1) if book[name].cell(i,column).value is not None]
        if sorted(actual_ids)!=sorted(expected_ids):raise ValueError('backdata_item_binding_mismatch:'+name)
        all_ids.extend(expected_ids)
    expected_edges=sorted((e['from_id'],e['to_id'],e['relation']) for stage in stages for e in stage.get('edges',[]))
    actual_edges=sorted(tuple(literal(v) for v in row[:3]) for row in book['근거 연결'].iter_rows(min_row=7,values_only=True) if row[0] is not None)
    if actual_edges!=expected_edges:raise ValueError('backdata_edge_binding_mismatch')
    native_text=[]
    for slide in deck.slides:
        for shape in slide.shapes:
            frame=getattr(shape,'text_frame',None);table=getattr(shape,'table',None)
            if frame is not None:native_text.append(frame.text)
            if table is not None:native_text.extend(cell.text for row in table.rows for cell in row.cells)
        if slide.has_notes_slide and slide.notes_slide.notes_text_frame is not None:
            native_text.append(slide.notes_slide.notes_text_frame.text)
    compact=''.join(''.join(native_text).split())
    for item_id in all_ids:
        if ''.join(item_id.split()) not in compact:raise ValueError('native_item_missing:'+item_id)
    return {'schema':'ai-assessment-native-pair-check/v1','fact_count':len(data['facts']),
            'action_count':len(stages[2]['actions']),'item_count':len(all_ids),'edge_count':len(expected_edges),
            'source_bundle_sha256':run['source_bundle_sha256'],'pptx_sha256':sha(ppt),'xlsx_sha256':sha(xlsx)}


def preserve_pdf_navigation(ppt, pdf):
    """원본 native 내부 목적지를 PDF의 같은 페이지로 명시 보존한다. 클릭 실앱 승인은 별도다."""
    import os
    import time
    import fitz
    from pptx.enum.action import PP_ACTION
    deck=Presentation(ppt); document=fitz.open(pdf)
    if len(deck.slides)!=len(document):raise ValueError('pdf_navigation_page_mismatch')
    targets={s.part:i for i,s in enumerate(deck.slides)};bindings=[]
    for page_index,slide in enumerate(deck.slides):
        page=document[page_index]
        sx=page.rect.width/deck.slide_width;sy=page.rect.height/deck.slide_height
        for shape in slide.shapes:
            if not shape.has_text_frame or shape.click_action.action!=PP_ACTION.NAMED_SLIDE:continue
            target=shape.click_action.target_slide
            if target is None or target.part not in targets:raise ValueError('native_navigation_target_unresolved')
            target_index=targets[target.part]
            rectangle=fitz.Rect(float(shape.left)*sx,float(shape.top)*sy,float(shape.left+shape.width)*sx,float(shape.top+shape.height)*sy)
            page.insert_link({'kind':fitz.LINK_GOTO,'from':rectangle,'page':target_index,'to':fitz.Point(0,0)})
            bindings.append({'source_page':page_index+1,'target_page':target_index+1,'label':getattr(shape,'text',''),'rectangle':list(rectangle)})
    temporary=Path(pdf).with_name(Path(pdf).stem+f'.{os.getpid()}.navigation.tmp.pdf')
    document.save(temporary,garbage=3,deflate=True);document.close()
    # 파일을 읽는 Windows 뷰어와 경합하면 이전 후보를 남기고 명시적으로 실패한다.
    for attempt in range(4):
        try:os.replace(temporary,pdf);break
        except PermissionError:
            if attempt==3:raise
            time.sleep(.2*(attempt+1))
    with fitz.open(pdf) as verified:
        for binding in bindings:
            links=verified[binding['source_page']-1].get_links()
            if not any(l['kind']==fitz.LINK_GOTO and l['page']==binding['target_page']-1 and abs(l['from'].x0-binding['rectangle'][0])<.02 and abs(l['from'].y0-binding['rectangle'][1])<.02 for l in links):raise ValueError('pdf_navigation_not_preserved')
    return {'native_internal_links':len(bindings),'pdf_destination_annotations_verified':True,'native_app_click_verified':False,'pdf_viewer_click_verified':False,'bindings':bindings}


def build(result,directory,*,task_manager=None):
    directory=Path(directory);directory.mkdir(parents=True,exist_ok=True)
    if task_manager is None:
        from asis_tasks import active_task_manager
        task_manager = active_task_manager.get()
    def run_native(key,operation):
        return task_manager.perform_sync(key,operation) if task_manager is not None else operation()
    def record_native(key,paths):
        if task_manager is not None:
            row=task_manager.ledger['tasks'][key]
            row['completion']['evidence']=[{'path':str(Path(path).resolve()),'sha256':sha(path)} for path in paths]
            with task_manager._mutex:task_manager._persist()
    input_hash=hashlib.sha256(encoded(result['input'])).hexdigest(); analysis_hash=hashlib.sha256(encoded(result)).hexdigest()
    (directory/'analysis.json').write_bytes(encoded(result))
    (directory/'input.json').write_bytes(encoded(result['input']))
    integrated = result['input']['context'].get('report_kind') == 'integrated'
    ppt=directory/('Integrated-Report.pptx' if integrated else 'ASIS-Report.pptx')
    xlsx=directory/('Integrated-Backdata.xlsx' if integrated else 'ASIS-Backdata.xlsx')
    from asis_excel_freeze import freeze,consume
    frozen_folder=directory/'excel-first'
    freeze_receipt=run_native('workbook-freeze',lambda:freeze(result,frozen_folder))
    record_native('workbook-freeze',[frozen_folder/'freeze-receipt.json',frozen_folder/freeze_receipt['xlsx_name']])
    frozen_result=consume(result,frozen_folder)
    shutil.copy2(frozen_folder/freeze_receipt['xlsx_name'],xlsx)
    pages=run_native('native-presentation',lambda:presentation(frozen_result,ppt))
    record_native('native-presentation',[ppt,ppt.with_suffix('.layout.json')])
    # 네이티브 원본은 보존한다. macOS는 동일 설치 글꼴의 검증된 한글 family alias만 QA 사본에 사용한다.
    render_input=ppt; alias_binding=None
    import platform
    if platform.system()=='Darwin':
        from fontTools.ttLib import TTFont
        font_file=Path.home()/'Library/Fonts/malgun.ttf'
        if not font_file.is_file():raise RuntimeError('requested_font_identity_unavailable')
        font=TTFont(font_file);families={n.toUnicode() for n in font['name'].names if n.nameID in (1,16)}
        if not {'Malgun Gothic','맑은 고딕'}.issubset(families):raise ValueError('localized_alias_is_not_same_installed_font')
        before_hash=sha(ppt);render_input=directory/'render-input'/'ASIS-Report.pptx';render_input.parent.mkdir(parents=True,exist_ok=True)
        with zipfile.ZipFile(ppt) as source,zipfile.ZipFile(render_input,'w',zipfile.ZIP_DEFLATED) as target:
            for info in source.infolist():
                content=source.read(info.filename)
                if info.filename.startswith('ppt/') and info.filename.endswith('.xml'):
                    content=content.replace(b'typeface="Malgun Gothic"','typeface="맑은 고딕"'.encode('utf-8'))
                target.writestr(info,content)
        if sha(ppt)!=before_hash:raise ValueError('native_ppt_changed_during_alias_render')
        alias_binding={'native_font':'Malgun Gothic','render_alias':'맑은 고딕','same_font_file_sha256':sha(font_file),'native_unchanged':True}
    def render_actual():
        pdf=convert(render_input,directory/'render','pdf',directory/'render-profile')
        navigation=preserve_pdf_navigation(ppt,pdf)
        import fitz
        with fitz.open(pdf) as rendered:
            if len(rendered)!=pages:raise ValueError('actual_render_page_count_mismatch')
            previews=[]
            for index,page in enumerate(rendered):
                target=directory/'render'/f'slide-{index+1:03d}.png'
                page.get_pixmap(matrix=fitz.Matrix(1.1,1.1),alpha=False).save(target)
                previews.append({'name':str(target.relative_to(directory)),'sha256':sha(target),'page':index+1})
        return pdf,navigation,previews
    pdf,navigation,previews=run_native('actual_render',render_actual)
    record_native('actual_render',[pdf,*[directory/p['name'] for p in previews]])
    roles=[('asis_report_pptx',ppt),('asis_backdata_xlsx',xlsx),('asis_render_pdf',pdf)]
    artifacts=[{'role':role,'name':str(path.relative_to(directory)),'sha256':sha(path),'bytes':path.stat().st_size} for role,path in roles]
    manifest={'schema':'ai-assessment-asis-artifacts/r2','run':deepcopy(result['input']['run']),
              'input_sha256':input_hash,'analysis_sha256':analysis_hash,'code_sha256':sha(Path(__file__)),
              'template_sha256':TEMPLATE_HASH,'content_template_sha256':CONTENT_HASH,
              'prompt_files':{p.name:sha(p) for p in (ASSET/'contract/prompts').glob('*.txt')},
              'artifacts':artifacts,'render':{'source_pptx_sha256':sha(ppt),'pdf_sha256':sha(pdf),'page_count':pages,'previews':previews,'rendered_pptx_sha256':sha(render_input),'same_face_alias':alias_binding,'navigation':navigation,'tagged_pdf':False,'accessibility_approved':False},
              'quality_status':result['quality_status'],'model_call_performed':result['model_call_performed'],
              'classification':result['input']['context']['classification'],'current_operations_verified':False,
              'generated_at':datetime.now(timezone.utc).isoformat()}
    if result['input']['context'].get('report_kind') == 'integrated':
        manifest['report_kind'] = 'integrated'
        manifest['workflow'] = deepcopy(result['input']['context']['assessment_workflow'])
    (directory/'artifact-manifest.json').write_bytes(encoded(manifest))
    return manifest
