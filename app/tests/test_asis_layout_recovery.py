"""시각 보완 정책이 실제 PPT 배치에 소비되는지 확인한다. 수치·원장은 변경하지 않는다."""
from copy import deepcopy
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from pptx import Presentation
from test_asis_configuration_delivery import configuration_case
import asis_artifacts


def test_layout_repair_policy_changes_native_pages_without_losing_body_or_edges(tmp_path):
    result,_=configuration_case(tmp_path,extra_vnets=3)
    sentence='합성 시험 입력의 구성 참조와 실제 동작 검증 공백은 구분하여 확인합니다. '
    result['stages'][0]['areas'][0]['implication']['text']=sentence*45
    original=tmp_path/'original.pptx';fixed=tmp_path/'fixed.pptx'
    asis_artifacts.presentation(__import__('visual_spec_stub').with_visual_specs(result),original)
    revised=deepcopy(result);revised['layout_policy']={'body_max_lines':10,'observation_max_lines':7,
        'configuration_force_rows':True,'trace_edge_rows':True}
    asis_artifacts.presentation(__import__('visual_spec_stub').with_visual_specs(revised),fixed)
    old=Presentation(original);new=Presentation(fixed)
    assert len(new.slides)>len(old.slides),'layout limits must change the actual native document'
    from pptx.util import Pt
    joined=''.join(''.join(shape.text.split()) for slide in new.slides for shape in slide.shapes
        if shape.has_text_frame and any(run.font.size==Pt(11.7) for paragraph in shape.text_frame.paragraphs for run in paragraph.runs))
    assert ''.join((sentence*45).split()) in joined
    before=[];after=[]
    for deck,target in ((old,before),(new,after)):
        for slide in deck.slides:
            notes=slide.notes_slide.notes_text_frame.text
            if 'asis-configuration-relationships/1' in notes:
                import json
                target.extend(e['id'] for e in json.loads(notes)['edges'])
    assert set(before)==set(after)
    assert revised['stages']==result['stages']
