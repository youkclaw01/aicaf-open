"""분류 Excel의 전수 원문·기준·비용 재계산과 바인딩을 실제 파일로 검증한다."""
from pathlib import Path
import sys,json
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from openpyxl import load_workbook
from test_asis_connection import synthetic_input
import asis_analysis,asis_artifacts


def test_classified_workbook_reconstructs_all_sources_and_preserves_formulas(tmp_path):
    data=synthetic_input(tmp_path)
    data['context']['source_documents']['sentinel-extra']={'unicode':'합성 정본','large':'a'*40000}
    result=asis_analysis.analyze(data)
    book=tmp_path/'classified.xlsx'
    asis_artifacts.workbook(result,book)
    wb=load_workbook(book,data_only=True)
    assert {'분류 인덱스','현황 분류','기준 대조','원천 원문','비용 월별','사용량 집계'}<=set(wb.sheetnames)
    chunks={}
    for row in wb['원천 원문'].iter_rows(min_row=7,values_only=True):
        if row[0]:chunks.setdefault(row[0],[]).append((int(row[2]),row[4]))
    restored={name:json.loads(''.join(v for _,v in sorted(values))) for name,values in chunks.items()}
    assert restored==result['input']['context']['source_documents']
    assert wb['기준 대조'].max_row-6==len(result['input']['criteria'])
    formulas=load_workbook(book,data_only=False)
    assert formulas['실행 정보']['B7'].data_type=='f'
    assert wb['실행 정보']['B7'].value==len(result['input']['facts'])
