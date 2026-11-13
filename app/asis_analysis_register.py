"""PPT에 사용할 검토 원고·시각 근거를 별도 Excel 분석란에 저장하고 재판독한다."""
from __future__ import annotations
import copy,hashlib,json
from pathlib import Path
import openpyxl
from openpyxl.styles import Font,PatternFill,Alignment
SHEET='분석_보고원고'
VERSION='ai-assessment-analysis-register/v1'
CHUNK=12000

def _json(v):
    return json.dumps(v,ensure_ascii=False,sort_keys=True,separators=(',',':'),allow_nan=False)

def project(candidate):
    """기초 원장은 제외하고 보고용으로 준비한 분석·시각 근거만 전달한다."""
    p=copy.deepcopy(candidate)
    original=p.get('input',{})
    data=copy.deepcopy(original)
    material={}
    if isinstance(original,dict):
        from asis_artifacts import native_diagram_rows,configuration_relationships
        areas=next((s.get('areas',[]) for s in p.get('stages',[]) if s.get('stage')=='area'),[])
        material['area_nodes']={a['area_id']:native_diagram_rows(a,original) for a in areas}
        material['relationships']={a['area_id']:configuration_relationships(original,a['area_id'])
            for a in areas if a['area_id'] in ('ai','network')}
        material['visual_facts']=[{'id':f['id'],'collection_status':f.get('collection_status'),
            'scope_complete':f.get('scope_complete'),'value':f.get('value')}
            for f in original.get('facts',[]) if isinstance(f,dict) and 'id' in f]
    topology=original.get('context',{}).get('source_documents',{}).get('topology',{})
    fields={'virtualNetworks':('id','name'),'subnets':('id','name','vnetId','addressPrefixes'),
        'networkInterfaces':('id','subnetId'),'networkInterfaceReliability':('id','virtualMachineId'),
        'privateEndpoints':('id','subnetId'),'applicationGatewayConfiguration':('id','gatewayIPConfigurations')}
    visual={'resources':[{k:v for k,v in row.items() if k in ('id','name','type','location')}
            for row in topology.get('resources',[]) if isinstance(row,dict)],'configurations':{}}
    for dataset,keys in fields.items():
        visual['configurations'][dataset]=[{k:v for k,v in row.items() if k in keys}
            for row in topology.get('configurations',{}).get(dataset,[]) if isinstance(row,dict)]
    material['architecture_topology']=visual
    ctx=data.setdefault('context',{})
    ctx.pop('source_documents',None)
    ctx.pop('collection_basis',None)
    ctx.pop('collection_basis_workbook',None)
    ctx['ppt_input_policy']='analysis_sheets_only'
    material['area_names']={a['id']:a['name'] for a in original.get('context',{}).get('collection_basis',{}).get('areas',[]) if isinstance(a,dict)}
    ctx['report_material']=material
    data['facts']=[]
    p['input']=data
    p['report_material']=material
    p['ppt_input_policy']='analysis_sheets_only'
    # Excel·JSON 재판독 뒤에도 tuple/list 차이로 원고 비교가 달라지지 않도록 정규화한다.
    return json.loads(_json(p))

HEADERS=['분석 ID','관련 영역','현황 요약','간단한 분석','권장 구성','시사점·권고 이유','권장 디자인 소개','권장 ID','근거 참조','검토자','검토 상태']


def surface_rows(packet):
    """기초 Excel의 지정 분석란에 동일 원고를 배치한다. 수집 원장과 판정은 수정하지 않는다."""
    from asis_artifacts import AREA_NAMES
    names={**AREA_NAMES,**packet.get('report_material',{}).get('area_names',{})}
    state='독립 검토 완료' if packet.get('quality_status')=='independently_reviewed' else '미검토'
    reviewer='독립 내용 검토' if state=='독립 검토 완료' else ''
    surfaces={}
    def add(name,row):
        values=[v if isinstance(v,str) else '\n'.join(str(x) for x in v) if isinstance(v,list) else '' if v is None else str(v) for v in row]
        pieces=max([1]+[(len(v)+CHUNK-1)//CHUNK for v in values])
        for i in range(pieces):
            surfaces.setdefault(name,[]).append([v[i*CHUNK:(i+1)*CHUNK] if len(v)>CHUNK else v if i==0 or j in (0,1,7,8,9,10) else '' for j,v in enumerate(values)])
    for stage in packet.get('stages',[]):
        if stage.get('stage')=='area':
            for area in stage.get('areas',[]):
                name='분석_'+names.get(area['area_id'],area['area_id'])
                for o in area.get('observations',[]):
                    add(name,[o['id'],area['area_id'],o['text'],'','','','',o.get('criterion_refs',[]),o.get('fact_refs',[]),reviewer,state])
                implication=area.get('implication')
                if implication:
                    add(name,[implication['id'],area['area_id'],'',implication['text'],implication.get('change_direction',''),implication.get('impact',''),'',implication.get('criterion_refs',[]),implication.get('fact_refs',[]),reviewer,state])
        if stage.get('stage')=='synthesis':
            for d in stage.get('directions',[]):
                add('분석_종합',[d['id'],d.get('area_implication_refs',[]),d.get('title',''),d['text'],d.get('change_direction',''),d.get('customer_impact',''),'',d.get('criterion_refs',[]),d.get('fact_refs',[]),reviewer,state])
        if stage.get('stage')=='actions':
            for d in stage.get('diagnoses',[]):
                add('분석_종합',[d['id'],d.get('direction_refs',[]),'',d['text'],'',d.get('impact',''),'',d.get('criterion_refs',[]),d.get('fact_refs',[]),reviewer,state])
            for a in stage.get('actions',[]):
                details='\n'.join(str(a.get(k,'')) for k in ('rationale','priority_basis','prerequisites','verification_condition','deliverable','improvement_points'))
                add('분석_종합',[a['id'],a.get('diagnosis_refs',[]),a.get('title',''),details,a.get('target',''),a.get('change_type',''),'',a.get('criterion_refs',[]),a.get('fact_refs',[]),reviewer,state])
    return surfaces


def write(path,candidate):
    path=Path(path);book=openpyxl.load_workbook(path)
    if SHEET in book:del book[SHEET]
    sheet=book.create_sheet(SHEET)
    projection=project(candidate);text=_json(projection);digest=hashlib.sha256(text.encode('utf-8')).hexdigest()
    source=candidate.get('input',{}).get('run',{}).get('source_bundle_sha256','')
    sheet.append(['형식','분석 원고 SHA-256','원천 SHA-256','조각 수','PPT 직접 입력'])
    count=(len(text)+CHUNK-1)//CHUNK
    sheet.append([VERSION,digest,source,count,'analysis_sheets_only'])
    for i in range(count):
        sheet.append(['report_projection',i+1,count,digest,text[i*CHUNK:(i+1)*CHUNK]])
        # 수식처럼 보이는 사용자 문장도 실행하지 않고 문자열로 보존한다.
        for cell in sheet[sheet.max_row]:
            if isinstance(cell.value,str):cell.data_type='s'
    for row in sheet:
        for cell in row:cell.font=Font(name='맑은 고딕',size=10);cell.alignment=Alignment(vertical='top',wrap_text=True)
    for cell in sheet[1]:cell.fill=PatternFill('solid',fgColor='E7E6E6');cell.font=Font(name='맑은 고딕',size=10,bold=True)
    for col,width in [('A',28),('B',22),('C',22),('D',24),('E',110)]:sheet.column_dimensions[col].width=width
    sheet.freeze_panes='A3';sheet.auto_filter.ref=sheet.dimensions
    for name,values in surface_rows(projection).items():
        human=book[name] if name in book else book.create_sheet(name)
        header_styles=[copy.copy(c._style) for c in human[1]] if [c.value for c in human[1]]==HEADERS else None
        human.delete_rows(1,human.max_row);human.append(HEADERS)
        for row in values:human.append(row)
        for row in human:
            for cell in row:
                if isinstance(cell.value,str):cell.data_type='s'
                cell.font=Font(name='맑은 고딕',size=11,bold=cell.row==1)
                cell.alignment=Alignment(vertical='top',wrap_text=True)
        if header_styles:
            for cell,style in zip(human[1],header_styles):cell._style=style
        human.freeze_panes='A2';human.auto_filter.ref=human.dimensions
    book.save(path);book.close()
    actual=read(path)
    if actual!=projection:raise ValueError('analysis_register_write_readback_mismatch')
    return {'schema':VERSION,'sha256':digest,'source_bundle_sha256':source,'chunks':count,'sheet':SHEET}

def read(path):
    book=openpyxl.load_workbook(path,data_only=False,read_only=True)
    try:
        if SHEET not in book:raise ValueError('analysis_register_missing')
        rows=iter(book[SHEET].iter_rows(values_only=True));next(rows);header=next(rows)
        version,digest,source,count,policy=header[:5]
        if version!=VERSION or policy!='analysis_sheets_only' or type(count) is not int or count<1:raise ValueError('analysis_register_header')
        pieces=[]
        for i,row in enumerate(rows,1):
            if len(row)<5 or row[0]!='report_projection' or row[1]!=i or row[2]!=count or row[3]!=digest or not isinstance(row[4],str):raise ValueError('analysis_register_fragment')
            pieces.append(row[4])
        if len(pieces)!=count:raise ValueError('analysis_register_chunk_count')
        text=''.join(pieces)
        if hashlib.sha256(text.encode('utf-8')).hexdigest()!=digest:raise ValueError('analysis_register_sha256')
        packet=json.loads(text)
        if packet.get('input',{}).get('run',{}).get('source_bundle_sha256','')!=source:raise ValueError('analysis_register_source_binding')
        if packet.get('ppt_input_policy')!='analysis_sheets_only':raise ValueError('analysis_register_policy')
        for name,expected in surface_rows(packet).items():
            if name not in book:raise ValueError('analysis_surface_missing:'+name)
            values=list(book[name].iter_rows(values_only=True,max_col=len(HEADERS)))
            normalized=[[v if v is not None else '' for v in row] for row in values]
            if normalized!=[HEADERS,*expected]:raise ValueError('analysis_surface_mismatch:'+name)
        return packet
    finally:book.close()
