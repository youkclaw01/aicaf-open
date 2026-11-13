"""확인된 인터뷰 인용과 운영 확인을 혼동하지 않고 두 native 산출물에 보존한다."""
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from openpyxl import load_workbook
from pptx import Presentation
import asis_analysis
import asis_artifacts
from asis_pipeline import bind_intents
from test_asis_connection import synthetic_input


def test_goal_quote_is_visible_in_native_pair_without_promoting_operating_state(tmp_path):
    original=synthetic_input(tmp_path)
    quote=next(f['value'] for f in original['facts'] if f['id']=='F_IV_Q1_transcript')
    bound=bind_intents(original,{'goals':[{'source_fact_id':'F_IV_Q1_transcript','quote':quote,
        'interpretation':'현재 접근 권한을 유지하는 내부 문서 검색','area_refs':['ai','data','secgov'],'ambiguity':''}], 'requirements':[]})
    result=asis_analysis.analyze(bound)
    asis_artifacts.presentation(__import__('visual_spec_stub').with_visual_specs(result),tmp_path/'goal.pptx')
    asis_artifacts.workbook(result,tmp_path/'goal.xlsx')
    prs=Presentation(tmp_path/'goal.pptx');text=''.join(''.join(shape.text.split()) for slide in prs.slides for shape in slide.shapes if shape.has_text_frame)
    book=load_workbook(tmp_path/'goal.xlsx',data_only=False)
    cells=[str(cell.value or '') for sheet in book for row in sheet for cell in row]
    assert ''.join(quote.split()) in text
    assert any(quote in cell for cell in cells)
    assert any('운영 확인과 분리' in cell for cell in cells)
