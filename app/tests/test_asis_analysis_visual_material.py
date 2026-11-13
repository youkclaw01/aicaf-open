"""보고 도식의 표시용 근거가 분석란에 저장되고 원장 재접근 없이 소비되는지 확인한다."""
import sys
from pathlib import Path
import pytest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import asis_analysis_register as r
from asis_artifacts import native_diagram_rows,configuration_relationships


def test_architecture_register_only_carries_named_visual_fields():
    original={'input':{'facts':[],'context':{'source_documents':{'topology':{
        'resources':[{'id':'/r1','name':'service','type':'microsoft.storage/storageaccounts','location':'koreacentral','properties':{'raw_config':'RAW_ONLY'}}],
        'configurations':{'subnets':[{'id':'/s1','name':'s1','vnetId':'/v1','addressPrefixes':['10.0.0.0/24'],'other':'RAW_ONLY'}]},
        'raw_unused_dataset':'RAW_ONLY'}}}},'stages':[{'stage':'area','areas':[]}]}
    actual=r.project(original)
    topology=actual['report_material']['architecture_topology']
    assert topology['resources'][0]=={'id':'/r1','name':'service','type':'microsoft.storage/storageaccounts','location':'koreacentral'}
    assert 'other' not in topology['configurations']['subnets'][0]
    assert 'raw_unused_dataset' not in topology
    assert 'source_documents' not in actual['input']['context']


def test_native_area_diagram_consumes_saved_analysis_rows():
    data={'facts':[],'context':{'ppt_input_policy':'analysis_sheets_only','report_material':{
        'area_nodes':{'ai':[['관측 서비스','검토된 설명',False]]},
        'relationships':{'ai':{'nodes':[],'edges':[],'unknowns':[]}}}}}
    assert native_diagram_rows({'area_id':'ai'},data)==[['관측 서비스','검토된 설명',False]]
    assert configuration_relationships(data,'ai')=={'nodes':[],'edges':[],'unknowns':[]}
    with pytest.raises(ValueError,match='analysis_visual_material_missing'):native_diagram_rows({'area_id':'data'},data)
    with pytest.raises(ValueError,match='analysis_visual_material_missing'):configuration_relationships(data,'data')
