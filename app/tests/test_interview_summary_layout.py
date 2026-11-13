"""합성 점수로 요약 가족의 좁은 단위칸·이중영역명·기준선 교차를 검증한다. 점수 산정 증명은 아니다."""
from collections import Counter
from copy import deepcopy
from typing import Any, Callable, cast
import pytest
from pptx import Presentation
from pptx.dml.color import RGBColor
import assessment_flow_interview as native
import interview_report as report
from questions import questions_for,TRACK_META
from scoring import compute


def summary_case(areas=None):
    questions=deepcopy(questions_for('existing'));answers={q['id']:{'score':'2','summary':'합성 배치 시험','transcript':'합성 시험 응답'} for q in questions}
    result=compute(questions,answers)
    result['areas']={'A':65.6,'B':18.8,'C':sum((25,50,50))/3,'D':66.7}
    if areas is not None:result['areas']=areas
    deck=Presentation(str(report.TEMPLATE));report.summary(deck,questions,answers,result,2,TRACK_META['existing'])
    return deck,cast(dict[str,Any],TRACK_META['existing'])


def texts(slide):return Counter(s.text for s in slide.shapes if s.has_text_frame)


def layout_function():
    fn=getattr(native,'fit_summary_layout',None)
    assert callable(fn),'실제 요약 형상에 적용되는 공통 레이아웃 보정기가 없다'
    return cast(Callable[...,dict[str,Any]],fn)


def test_summary_fits_narrow_units_and_multiline_domain_without_value_edits():
    deck,meta=summary_case();slide=deck.slides[-1];before=texts(slide)
    result=layout_function()(deck,meta)
    assert texts(slide)==before
    units=[s for s in cast(list[Any],list(slide.shapes)) if s.has_text_frame and (s.text.startswith('/') or s.text=='개')]
    assert {'/100','/4'}<={s.text for s in units}
    for s in units:
        assert s.text_frame.word_wrap is False
        assert s.text_frame.margin_left==s.text_frame.margin_right==s.text_frame.margin_top==s.text_frame.margin_bottom==0
    for key,label in meta['short'].items():
        shape=next(s for s in cast(list[Any],list(slide.shapes)) if s.has_text_frame and s.text==f'{key} {label}')
        assert shape.text_frame.word_wrap is False
        assert shape.text_frame.margin_left==shape.text_frame.margin_right==0
    assert result['values_unchanged'] is True
    assert not report.verify(deck)


def test_chart_score_labels_are_opaque_and_in_front_of_reference_line():
    deck,meta=summary_case();slide=deck.slides[-1];layout_function()(deck,meta)
    values=[s for s in cast(list[Any],list(slide.shapes)) if s.has_text_frame and s.text=='41.7']
    assert len(values)==1
    score=values[0];assert score.fill.fore_color.rgb==RGBColor(255,255,255)
    assert list(slide.shapes).index(score)>max(i for i,s in enumerate(slide.shapes) if getattr(s,'shape_type',None)==9)


def test_summary_layout_is_idempotent_and_does_not_edit_other_slides():
    deck,meta=summary_case();other=report.new(deck);report.text(other,100000,100000,1000000,300000,'독립 장표',9,report.TXT)
    untouched=other._element.xml;fn=layout_function();fn(deck,meta)
    first=[(s.text,s.left,s.top,s.width,s.height,s._element.xml) for s in cast(list[Any],list(deck.slides[0].shapes)) if s.has_text_frame]
    fn(deck,meta)
    assert first==[(s.text,s.left,s.top,s.width,s.height,s._element.xml) for s in cast(list[Any],list(deck.slides[0].shapes)) if s.has_text_frame]
    assert other._element.xml==untouched


def test_unknown_summary_family_fails_closed():
    deck=Presentation();deck.slides.add_slide(deck.slide_layouts[6])
    with pytest.raises(ValueError,match='summary'):layout_function()(deck,TRACK_META['existing'])


def test_full_score_labels_do_not_collide_with_chart_heading():
    deck,meta=summary_case({k:100.0 for k in 'ABCD'});slide=deck.slides[-1]
    layout_function()(deck,meta)
    shapes=cast(list[Any],list(slide.shapes))
    heading=next(s for s in shapes if s.has_text_frame and s.text.startswith('영역별 점수 ·'))
    values=[s for s in shapes if s.has_text_frame and s.text=='100.0']
    assert len(values)==4
    assert all(s.top>=heading.top+heading.height for s in values)


def test_unscored_domains_keep_missing_values_and_have_no_false_score_labels():
    deck,meta=summary_case({k:None for k in 'ABCD'});slide=deck.slides[-1];before=texts(slide)
    result=layout_function()(deck,meta)
    assert result['scores']==0 and texts(slide)==before


def test_summary_fault_cannot_be_routed_to_unrelated_body_pagination():
    fn=getattr(native,'summary_layout_failures',None);assert callable(fn)
    fault={'where':'/pages/1/texts/12','quote':'/100','why':'분모 줄바꿈','fix':'슬롯 경계 보정'}
    rows=[{'pages':[1,2,3,4],'review':{'verdict':'revise','defects':[fault]}}]
    assert fn(rows,2)==[dict(fault,page=2)]
    assert fn(rows,8)==[]
    assert fn([{'pages':[1,2],'review':{'verdict':'revise','defects':[dict(fault,where='/source/items/0/summary')]}}],2)==[]


def test_operating_build_and_retry_really_call_the_layout_boundaries():
    import ast,inspect
    checks=[(native.build_pair,'fit_summary_layout'),(native.execute,'summary_layout_failures'),(native.execute,'next_native_round')]
    for owner,name in checks:
        tree=ast.parse(inspect.getsource(owner))
        assert sum(isinstance(n,ast.Call) and isinstance(n.func,ast.Name) and n.func.id==name for n in ast.walk(tree))==1
