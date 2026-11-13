"""독립 네이티브 조각 준비와 공유 패키지의 단일 병합."""
from copy import copy
from pathlib import Path
import hashlib
from concurrent.futures import ThreadPoolExecutor
from openpyxl import Workbook
from openpyxl.styles import Alignment,Font,PatternFill


def style_sheet(sheet):
    from asis_artifacts import FONT,COLOR
    sheet.sheet_view.showGridLines=False
    for row in sheet:
        for cell in row:cell.font=Font(name=FONT,size=10,color=COLOR['ink'])
    header=1 if sheet.title=='실행 정보' else 6
    for cell in sheet[header]:
        cell.fill=PatternFill('solid',fgColor=COLOR['primary'])
        cell.font=Font(name=FONT,size=10,color='FFFFFF',bold=True)
    sheet.column_dimensions['A'].width=max(sheet.column_dimensions['A'].width or 10,30)
    if sheet.title=='실행 정보':sheet.column_dimensions['B'].width=100


def prepare_workbook_sheets(specs,manager,destination):
    """셀·스타일 준비는 별도 Workbook 객체에서 수행하며 공유 ZIP에는 쓰지 않는다."""
    from asis_artifacts import literal
    from asis_tasks import task
    def prepare(spec):
        book=Workbook();sheet=book.active;sheet.title=spec['name']
        sheet['A1']=spec['name']
        sheet['A2']='동일 입력·동일 개정 원장. 미수집/실패는 0·없음/정상으로 변환하지 않습니다.'
        sheet['A3']='확인된 사실·해석·제안·미확인을 구분합니다. 운영 현재성과 독립 품질 승인은 별도입니다.'
        for j,label in enumerate(spec['headers'],1):sheet.cell(6,j,label)
        numeric={'비용 월별':{4},'사용량 집계':{4,5}}.get(spec['name'],set())
        for i,row in enumerate(spec['rows'],7):
            for j,value in enumerate(row,1):
                cell=sheet.cell(i,j,value if j in numeric else literal(value))
                cell.data_type='n' if j in numeric else 's'
                cell.alignment=Alignment(vertical='top',wrap_text=True)
        sheet.freeze_panes='B7'
        sheet.auto_filter.ref=f"A6:{sheet.cell(6,len(spec['headers'])).column_letter}{max(7,len(spec['rows'])+6)}"
        for j,width in enumerate(spec['widths'],1):sheet.column_dimensions[sheet.cell(6,j).column_letter].width=width
        style_sheet(sheet)
        return sheet
    operations={'excel-sheet-'+str(i+1).zfill(3):(lambda spec=spec:prepare(spec)) for i,spec in enumerate(specs)}
    if manager is not None:
        rows=[task(key,spec['name']+' 시트 준비',group='native') for key,spec in zip(operations,specs) if key not in manager.ledger['tasks']]
        if 'excel-merge' not in manager.ledger['tasks']:
            rows.append(task('excel-merge','Excel 시트 병합·저장',depends_on=list(operations),scheduling='exclusive',resource='excel-package',group='native'))
            rows.append(task('excel-recalculate','Excel 실제 재계산·수식 검증',depends_on=['excel-merge'],scheduling='exclusive',resource='office-recalculate',group='native'))
        if rows:manager.register(rows)
        return list(manager.parallel_sync(operations).values())
    with ThreadPoolExecutor() as pool:return list(pool.map(prepare,specs))


def merge_workbook_sheets(book,sheets):
    """서로 다른 Workbook의 셀·차원 스타일을 복사하고 같은 출력 파일은 한 번만 저장한다."""
    style_sheet(book.active)
    for source in sheets:
        target=book.create_sheet(source.title)
        for row in source:
            for cell in row:
                value=target.cell(cell.row,cell.column,cell.value)
                value.data_type=cell.data_type
                value.font=copy(cell.font);value.fill=copy(cell.fill)
                value.border=copy(cell.border);value.alignment=copy(cell.alignment)
                value.number_format=cell.number_format;value.protection=copy(cell.protection)
        for key,dimension in source.column_dimensions.items():
            target.column_dimensions[key].width=dimension.width
        target.freeze_panes=source.freeze_panes
        target.auto_filter.ref=source.auto_filter.ref
        target.sheet_view.showGridLines=source.sheet_view.showGridLines


def parallel_presentation(result,destination):
    """독립 승인 조각을 생성하고 Part·관계·Notes를 보존하여 한 패키지로 병합한다."""
    import json,re
    from pptx import Presentation
    from pptx.opc.package import Part
    from pptx.opc.packuri import PackURI
    from pptx.parts.slide import SlidePart
    from pptx.oxml import parse_xml
    from pptx.opc.constants import RELATIONSHIP_TYPE as RT
    from pptx.enum.text import PP_ALIGN
    from asis_model import ASSET
    from asis_tasks import active_task_manager,task
    from asis_approved_presentation import presentation
    from asis_artifacts import text,CONTENT_HASH,TEMPLATE_HASH
    manager=active_task_manager.get()
    destination=Path(destination);root=destination.parent/'ppt-fragments';root.mkdir(parents=True,exist_ok=True)
    sections=['front']+['area-'+a['area_id'] for a in result['stages'][0]['areas']]+['synthesis','actions','appendix']
    operations={ 'ppt-fragment-'+section:(lambda section=section:presentation(result,root/(section+'.pptx'),section=section)) for section in sections}
    if manager is not None:
        specs=[task(key,section+' 승인 PPT 조각',group='native') for key,section in zip(operations,sections) if key not in manager.ledger['tasks']]
        if specs:manager.register(specs)
        manager.parallel_sync(operations)
    else:
        with ThreadPoolExecutor() as pool:list(pool.map(lambda op:op(),operations.values()))
    base=Presentation(ASSET/'content-asis-e.pptx')
    for sid in list(base.slides._sldIdLst):base.part.drop_rel(sid.rId);base.slides._sldIdLst.remove(sid)
    package=base.part.package;sequence=0;pages=[]
    def clone(part,memo):
        nonlocal sequence
        if id(part) in memo:return memo[id(part)]
        sequence+=1
        from pathlib import PurePosixPath
        old=PurePosixPath(str(part.partname));name=PackURI(str(old.parent)+'/merged-'+str(sequence)+old.suffix)
        node=SlidePart.load(name,part.content_type,package,part.blob) if part.content_type.endswith('slide+xml') else Part(name,part.content_type,package,part.blob)
        memo[id(part)]=node
        for rel in part.rels.values():
            target=rel.target_ref if rel.is_external else clone(rel.target_part,memo)
            from pptx.opc.package import _Relationship
            from pptx.opc.constants import RELATIONSHIP_TARGET_MODE as RTM
            node.rels._rels[rel.rId]=_Relationship(node.partname.baseURI,rel.rId,rel.reltype,
                target_mode=RTM.EXTERNAL if rel.is_external else RTM.INTERNAL,target=target)
        return node
    # 승인 조각은 같은 틀의 마스터·레이아웃을 재사용한다. 미등록 마스터 복제는 Office에서 손상으로 판정된다.
    template_types={
        'application/vnd.openxmlformats-officedocument.presentationml.slideMaster+xml',
        'application/vnd.openxmlformats-officedocument.presentationml.slideLayout+xml',
        'application/vnd.openxmlformats-officedocument.presentationml.notesMaster+xml',
        'application/vnd.openxmlformats-officedocument.theme+xml'}
    template_parts={str(part.partname):part for part in package.iter_parts() if part.content_type in template_types}
    for section in sections:
        source=root/(section+'.pptx');deck=Presentation(source);memo={}
        for part in deck.part.package.iter_parts():
            if part.content_type not in template_types:continue
            target=template_parts.get(str(part.partname))
            if target is None or target.content_type!=part.content_type or target.blob!=part.blob:
                raise ValueError('approved_fragment_template_part_mismatch')
            memo[id(part)]=target
        for slide in deck.slides:
            part=clone(slide.part,memo)
            rid=base.part.relate_to(part,RT.SLIDE);base.slides._sldIdLst.add_sldId(rid)
        pages.extend(json.loads(source.with_suffix('.layout.json').read_text(encoding='utf-8'))['pages'])
    for i,slide in enumerate(base.slides):
        for shape in list(slide.shapes):
            value=getattr(shape,'text','')
            if value=='다음 상세' or re.fullmatch(r'\d+ / \d+',value):slide.shapes._spTree.remove(shape._element)
        if i+1<len(base.slides):text(slide,'다음 상세',10.1,6.78,1.6,.22,9,False,'muted').click_action.target_slide=base.slides[i+1]
        text(slide,f'{i+1:02d} / {len(base.slides):02d}',11.75,7.11,.96,.22,9,False,'muted',PP_ALIGN.RIGHT)
    base.core_properties.subject=result['input']['run']['source_bundle_sha256']
    base.core_properties.title='AI Assessment · AS-IS 현황 분석'
    base.save(destination)
    destination.with_suffix('.layout.json').write_text(json.dumps({'content_template_sha256':CONTENT_HASH,'visual_template_sha256':TEMPLATE_HASH,'pages':pages,'actual_render_verified':False},ensure_ascii=False,indent=2),encoding='utf-8')
    return len(base.slides)
