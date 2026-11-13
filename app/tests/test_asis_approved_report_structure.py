"""승인 보고서의 실제 페이지 역할과 판단 패널을 검증한다. 입력은 합성 단위 자료다."""
from pathlib import Path
import sys
from pptx import Presentation
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import asis_artifacts


def test_current_report_keeps_overall_native_architecture_and_six_action_columns(tmp_path):
    """승인 보고서의 전체구성 및 추가 이미지의 여섯 열 역할을 실제 PPT에서 확인한다."""
    from test_asis_configuration_delivery import configuration_case
    result,_=configuration_case(tmp_path)
    path=tmp_path/'native-form.pptx'
    asis_artifacts.presentation(__import__('visual_spec_stub').with_visual_specs(result),path)
    deck=Presentation(path)
    arch=[s for s in deck.slides if any(sh.name=='approved_family:architecture_overview' for sh in s.shapes)]
    assert arch, '승인 전체 구성 장표 누락'
    assert any(sh.name.startswith('architecture_native_scope:') for s in arch for sh in s.shapes)
    assert any(sh.shape_type==13 for s in arch for sh in s.shapes), '공식 아이콘 누락'
    from zipfile import ZipFile
    with ZipFile(path) as archive:
        assert len(archive.namelist()) == len(set(archive.namelist())), 'PPT 미디어 파트명 중복'
    action=[s for s in deck.slides if any(sh.name=='approved_family:diagnosis_action_overview' for sh in s.shapes)]
    assert action, '추가 이미지의 전체 관계 장표 누락'
    text='\n'.join(sh.text for s in action for sh in s.shapes if sh.has_text_frame)
    for label in ('과제','연결 영역','연결 진단 주제'):
        assert label in text, label
from test_asis_configuration_delivery import configuration_case


def test_approved_report_roles_and_current_state_chain_are_visible(tmp_path):
    result,_=configuration_case(tmp_path)
    path=tmp_path/'approved.pptx'
    asis_artifacts.presentation(__import__('visual_spec_stub').with_visual_specs(result),path)
    deck=Presentation(path)
    def family(slide):
        return next((s.name.split(':',1)[1] for s in slide.shapes if s.name.startswith('approved_family:')),None)
    families=[family(slide) for slide in deck.slides]
    required={'cover','executive','structure','framework_waf','framework_landing_zone','inventory','area_fact','fact_synthesis_overview','area_diagnosis_insight','diagnosis_action_overview','action_overview','sources'}
    assert required.issubset(families), sorted(required-set(families))
    assert families.index('structure')<families.index('framework_waf')<families.index('inventory')<families.index('area_fact')
    source_pages=[slide for slide in deck.slides if family(slide)=='area_fact']
    assert source_pages
    labels={'기준','현재','부족한 점','권고'}
    for slide in source_pages:
        # 계속 장표도 실제 분석 문법을 공유한다. 별첨의 일반 본문은 다른 family다.
        texts={s.text for s in slide.shapes if s.has_text_frame}
        assert labels.issubset(texts), labels-texts
        assert any(s.name.startswith('approved_area_diagram:') for s in slide.shapes)
        assert any(s.name.startswith('approved_fact_panel:') for s in slide.shapes)
        assert any(s.name.startswith('approved_judgment_cell:') for s in slide.shapes)
    for slide in deck.slides:
        for shape in slide.shapes:
            if shape.has_text_frame and shape.name.startswith('approved_kicker:'):
                assert '네트워크' not in shape.text or family(slide) not in {'framework_waf','framework_landing_zone','inventory','sources'}
