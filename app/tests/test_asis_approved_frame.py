"""승인 보고서 전체 표현형태·보완 이미지 역할을 네이티브 생성 경로에서 확인한다."""
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from pptx import Presentation
from test_asis_connection import synthetic_input
import asis_analysis,asis_artifacts


def test_native_report_has_approved_families_and_authored_executive_headline(tmp_path):
    result=asis_analysis.analyze(synthetic_input(tmp_path))
    result['input']['context']['classification']='case_snapshot'
    output=tmp_path/'report.pptx'
    asis_artifacts.presentation(__import__('visual_spec_stub').with_visual_specs(result),output)
    deck=Presentation(output)
    families=[]
    for slide in deck.slides:
        families.extend(s.name for s in slide.shapes if s.name.startswith('approved_family:'))
    assert {'approved_family:executive','approved_family:area_fact','approved_family:fact_synthesis_overview','approved_family:area_diagnosis_insight','approved_family:diagnosis_action_overview','approved_family:action_overview'}<=set(families)
    second=''.join(s.text for s in deck.slides[1].shapes if s.has_text_frame)
    assert result['stages'][3]['summary']['headline'] in second
    raw=''.join(s.text for sl in deck.slides for s in sl.shapes if s.has_text_frame)
    assert '가상고객 E' not in raw
    assert 'ID만 연결' not in raw
