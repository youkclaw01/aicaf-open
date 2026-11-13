"""기초 Excel의 전송 SHA·원천 바인딩과 보고서 병합 시 원본 보존을 확인한다."""
import sys,json,base64,hashlib
from pathlib import Path
from io import BytesIO
import openpyxl,pytest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))


def document():
    topology=b'{"resources":[]}'
    book=openpyxl.Workbook();book.active.title='전체현황';book.active['A1']='실측 기초 원장';book.active['A2']=99
    output=BytesIO();book.save(output);raw=output.getvalue();sha=hashlib.sha256(raw).hexdigest()
    evidence={'assessmentBasis':{'schema':'ai-assessment-basis/v1','source_artifacts':{
        'topology.json':{'sha256':hashlib.sha256(topology).hexdigest()}},'resourceInventory':[],'criterionMatches':[]},
        'assessmentBasisWorkbook':{'schema':'ai-assessment-basis-workbook/v1','base64':base64.b64encode(raw).decode(),'sha256':sha},
        'assessmentBasisArtifact':{'workbook_sha256':sha}}
    return topology,evidence


def test_collector_workbook_sha_and_source_are_required():
    from asis_collection_basis import extract
    topology,evidence=document();actual=extract(evidence,topology)
    assert actual['collection_basis']['schema']=='ai-assessment-basis/v1'
    assert actual['collection_basis_workbook']['sha256']==evidence['assessmentBasisWorkbook']['sha256']
    evidence['assessmentBasisWorkbook']['sha256']='0'*64
    with pytest.raises(ValueError,match='collection_basis_workbook_hash'):extract(evidence,topology)


def test_collector_workbook_cannot_be_bound_to_other_topology():
    from asis_collection_basis import extract
    topology,evidence=document()
    with pytest.raises(ValueError,match='collection_basis_source_binding'):extract(evidence,b'{}')


def test_original_basic_workbook_remains_when_analysis_is_appended(tmp_path,monkeypatch):
    from asis_collection_basis import extract
    from asis_artifacts import workbook
    from test_asis_connection import synthetic_input
    import asis_analysis,asis_artifacts
    source=synthetic_input(tmp_path);topology,evidence=document();source['context'].update(extract(evidence,topology))
    result=asis_analysis.analyze(source)
    path=tmp_path/'analysis.xlsx';workbook(result,path)
    actual=openpyxl.load_workbook(path)
    assert actual['전체현황']['A1'].value=='실측 기초 원장'
    assert actual['전체현황']['A2'].value==99
    assert '분석_보고원고' in actual.sheetnames
