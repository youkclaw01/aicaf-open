"""Excel 선행 검증·동결과 동일 분석 개정의 PPT 소비 계약."""
from copy import deepcopy
import json
from pathlib import Path
from openpyxl import load_workbook
from asis_model import digest,encoded


def freeze(result,folder):
    from asis_artifacts import workbook,sha,literal
    from asis_view import atomic
    root=Path(folder);root.mkdir(parents=True,exist_ok=True)
    book=root/'ASIS-Backdata.xlsx'
    workbook(result,book)
    wb=load_workbook(book,data_only=True)
    if wb['실행 정보']['B7'].value!=len(result['input']['facts']):raise ValueError('excel_freeze_count_failed')
    if wb['기준 대조'].max_row-6!=len(result['input']['criteria']):raise ValueError('excel_freeze_criteria_failed')
    chunks={}
    for row in wb['원천 원문'].iter_rows(min_row=7,values_only=True):
        if row[0]:chunks.setdefault(row[0],[]).append((int(str(row[2])),str(row[4])))
    restored={name:json.loads(''.join(v for _,v in sorted(parts))) for name,parts in chunks.items()}
    if restored!=result['input']['context'].get('source_documents',{}):raise ValueError('excel_freeze_source_changed')
    for i,f in enumerate(result['input']['facts'],7):
        if literal(wb['사실원장'].cell(i,1).value)!=f['id']:raise ValueError('excel_freeze_fact_order_failed')
    from asis_analysis_register import read as read_analysis_register,project
    from hashlib import sha256
    registered=read_analysis_register(book)
    if registered!=project(result):raise ValueError('analysis_register_native_readback_changed')
    register_sha=sha256(encoded(registered)).hexdigest()
    atomic(root/'analysis-frozen.json',result)
    receipt={'schema':'asis-excel-first-freeze/1','verified':True,'xlsx_name':book.name,'xlsx_sha256':sha(book),
             'analysis_sha256':digest(result),'source_bundle_sha256':result['input']['run']['source_bundle_sha256'],
             'analysis_register_sha256':register_sha,'ppt_input_policy':'analysis_sheets_only',
             'source_documents_reconciled':True,'criterion_count':len(result['input']['criteria']),
             'fact_count':len(result['input']['facts']),'actual_formula_recalculation':True,
             'customer_visual_review_complete':False}
    atomic(root/'freeze-receipt.json',receipt)
    return receipt


def consume(result,folder):
    from asis_artifacts import sha
    root=Path(folder);receipt=json.loads((root/'freeze-receipt.json').read_text(encoding='utf-8'))
    frozen=json.loads((root/'analysis-frozen.json').read_text(encoding='utf-8'))
    if not receipt['verified'] or sha(root/receipt['xlsx_name'])!=receipt['xlsx_sha256']:raise ValueError('excel_freeze_hash_changed')
    if digest(result)!=receipt['analysis_sha256'] or digest(frozen)!=receipt['analysis_sha256']:raise ValueError('excel_freeze_analysis_changed')
    from asis_analysis_register import read as read_analysis_register,_json
    from hashlib import sha256
    registered=read_analysis_register(root/receipt['xlsx_name'])
    register_sha=sha256(_json(registered).encode('utf-8')).hexdigest()
    if receipt.get('ppt_input_policy')!='analysis_sheets_only' or register_sha!=receipt.get('analysis_register_sha256'):
        raise ValueError('analysis_register_binding')
    return registered
