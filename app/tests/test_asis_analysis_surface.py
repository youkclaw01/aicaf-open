"""기초 Excel의 지정 분석란이 채워지고 PPT 원고와 읽기 복원이 일치하는지 검증한다."""
from pathlib import Path
import sys,openpyxl,pytest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import asis_analysis_register as r


def packet():
    return {'input':{'run':{'analysis_revision':'fixture','source_bundle_sha256':'a'*64},'facts':[],'context':{}},'stages':[
        {'stage':'area','areas':[{'area_id':'network','observations':[{'id':'O1','text':'검증용 현재 구성','fact_refs':['F1'],'criterion_refs':['C1']}],
         'implication':{'id':'I1','text':'검증용 분석','impact':'검증용 영향','change_direction':'검증용 권장 구성','fact_refs':['F1'],'criterion_refs':['C1']}}]},
        {'stage':'synthesis','directions':[{'id':'D1','title':'검증용 종합','text':'검증용 종합 의견','customer_impact':'검증용 영향','change_direction':'검증용 권장','fact_refs':['F1'],'criterion_refs':['C1']}]}],
        'quality_status':'not_independently_reviewed'}


def test_reserved_analysis_sheets_contain_same_reviewable_text(tmp_path):
    p=tmp_path/'basic.xlsx';b=openpyxl.Workbook();b.active.title='전체현황';b.active['A1']='변경 금지 원장';b.create_sheet('분석_네트워크');b.save(p)
    r.write(p,packet());b=openpyxl.load_workbook(p)
    values=[c.value for row in b['분석_네트워크'] for c in row]
    assert '검증용 현재 구성' in values and '검증용 분석' in values and '검증용 권장 구성' in values
    assert b['전체현황']['A1'].value=='변경 금지 원장'
    assert '미검토' in values
    assert '검증용 종합 의견' in [c.value for row in b['분석_종합'] for c in row]
    assert r.read(p)==r.project(packet())


def test_human_analysis_tampering_blocks_packet_readback(tmp_path):
    p=tmp_path/'report.xlsx';openpyxl.Workbook().save(p);r.write(p,packet());b=openpyxl.load_workbook(p);b['분석_네트워크']['D3']='반드시 거부해야 하는 변경';b.save(p)
    with pytest.raises(ValueError,match='analysis_surface'):r.read(p)
