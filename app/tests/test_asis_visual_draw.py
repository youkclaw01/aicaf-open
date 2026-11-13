"""시각 형식 그리기 시험: 실제 python-pptx 슬라이드에 그려 글자 크기·영역·수치 계산을 확인한다(렌더 이미지 검토는 별도)."""
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import pytest
from pptx import Presentation
from pptx.util import Inches
import asis_artifacts as art
import asis_visual_draw as D
import asis_overview_architecture as A


def new_slide():
    deck = Presentation()
    deck.slide_width, deck.slide_height = Inches(13.333), Inches(7.5)
    return deck, deck.slides.add_slide(deck.slide_layouts[6])


def connector_log():
    calls = []
    return calls, (lambda slide, start, end, key: calls.append((start, end, key)))


def draw(form, data, **extra):
    deck, slide = new_slide(); calls, connect = connector_log()
    D.draw_form(form, slide, art.text, art.box, art.wrapped_lines, {'form': form, 'data': data}, connect, **extra)
    return slide, calls


def sizes(slide):
    return [run.font.size.pt for shape in slide.shapes if shape.has_text_frame for p in shape.text_frame.paragraphs for run in p.runs if run.font.size]


def inside_body(slide):
    for shape in slide.shapes:
        assert shape.left / 914400 >= D.X0 - .01 and (shape.left + shape.width) / 914400 <= D.X1 + .01, shape.shape_type
        assert shape.top / 914400 >= D.Y0 - .01 and (shape.top + shape.height) / 914400 <= D.Y1 + .75, 'inside the body area'


NODES = [{'layer': k, 'label': A.layer_label(k), 'count': 3, 'types': ['accounts']} for k in ('network', 'compute', 'ai', 'data', 'observability')]
EDGES = [{'from': 'ai', 'to': 'network', 'count': 3}, {'from': 'data', 'to': 'network', 'count': 2}]


def test_every_form_draws_only_text_of_ten_points_or_more():
    forms = {
        'architecture': ({'focus_layers': ['ai', 'network'], 'gap_layers': ['network']}, {'nodes': NODES, 'edges': EDGES}),
        'table': ({'columns': ['대상', '현재', '갭'], 'rows': [['계정', '3개', '미확인'], ['배포', '5개', '부분']]}, {}),
        'matrix': ({'columns': ['부여', '검토', '회수'], 'rows': [{'label': 'AI 관리', 'states': ['충족', '부분', '미확인']}]}, {}),
        'flow': ({'steps': [{'label': '요청', 'owner': '운영', 'broken': False}, {'label': '승인', 'owner': '관리', 'broken': True}, {'label': '반영', 'owner': '운영', 'broken': False}]}, {}),
        'chart': ({'metrics': [{'fact_id': 'F1', 'label': '월 비용', 'aggregate': 'sum'}, {'fact_id': 'F2', 'label': '배포', 'aggregate': 'count'}]},
                  {'facts': [{'id': 'F1', 'value': [{'Cost': 10.5}, {'Cost': 4.5}]}, {'id': 'F2', 'value': [1, 2, 3]}]}),
        'scorecard': ({'items': [{'fact_id': 'F2', 'label': '배포 수', 'aggregate': 'count'}] * 3}, {'facts': [{'id': 'F2', 'value': [1, 2, 3]}]}),
    }
    for form, (data, extra) in forms.items():
        slide, calls = draw(form, data, **extra)
        assert sizes(slide) and min(sizes(slide)) >= 10, (form, min(sizes(slide)))


def test_the_architecture_draws_connectors_only_between_layers_with_a_declared_relationship_and_marks_gaps():
    slide, calls = draw('architecture', {'focus_layers': ['ai', 'network'], 'gap_layers': ['network']}, nodes=NODES, edges=EDGES)
    assert sorted({key for _, _, key in calls}) == ['arch-ai-network', 'arch-data-network'], 'one relationship, one connector key (several orthogonal segments)'
    for start, end, key in calls:
        assert abs(start[0] - end[0]) < .001 or abs(start[1] - end[1]) < .001, 'every segment is horizontal or vertical'
    text = '\n'.join(shape.text_frame.text for shape in slide.shapes if shape.has_text_frame)
    assert text.count('확인 필요') == 1 and '3개' in text and 'accounts' in text
    inside_body(slide)


def test_a_chart_value_is_computed_by_code_from_the_cited_fact_and_a_missing_fact_is_shown_as_not_collected():
    slide, _ = draw('chart', {'metrics': [{'fact_id': 'F1', 'label': '월 비용', 'aggregate': 'sum'}, {'fact_id': 'F9', 'label': '없는 사실', 'aggregate': 'count'}]},
                    facts=[{'id': 'F1', 'value': [{'Cost': 10.5}, {'Cost': 4.5}, {'Cost': True}]}])
    text = '\n'.join(shape.text_frame.text for shape in slide.shapes if shape.has_text_frame)
    assert '15.0' in text and '미수집' in text, 'a bool is never summed and a missing fact is never invented'


def test_aggregate_rules_and_the_scorecard_never_show_a_number_the_model_wrote():
    assert D.aggregate({'value': [1, 2, 3]}, 'count') == 3 and D.aggregate({'value': {'a': 1}}, 'count') is None
    assert D.aggregate({'value': [{'Cost': 1}, {'cost': 2.5}]}, 'sum') == 3.5 and D.aggregate({'value': []}, 'sum') is None
    slide, _ = draw('scorecard', {'items': [{'fact_id': 'F2', 'label': '배포 수', 'aggregate': 'count'}] * 3}, facts=[{'id': 'F2', 'value': [1, 2, 3, 4]}])
    text = '\n'.join(shape.text_frame.text for shape in slide.shapes if shape.has_text_frame)
    assert text.count('4') >= 3


def test_a_label_that_does_not_fit_fails_instead_of_shrinking_below_ten_points():
    with pytest.raises(ValueError, match='visual_label_capacity'):
        draw('table', {'columns': ['가' * 25, 'b', 'c'], 'rows': [['x', 'y', 'z']] * 8})


def test_a_broken_flow_step_is_dashed_and_labelled_and_steps_are_linked_in_order():
    slide, calls = draw('flow', {'steps': [{'label': '요청', 'owner': '운영', 'broken': False}, {'label': '승인', 'owner': '관리', 'broken': True}, {'label': '반영', 'owner': '운영', 'broken': False}]})
    assert [key for _, _, key in calls] == ['flow-0', 'flow-1']
    assert '끊김·확인 필요' in '\n'.join(shape.text_frame.text for shape in slide.shapes if shape.has_text_frame)


def test_an_architecture_without_collected_nodes_shows_a_gap_note_instead_of_failing():
    slide, calls = draw('architecture', {'focus_layers': ['network'], 'gap_layers': ['network']}, nodes=[], edges=[])
    text = '\n'.join(shape.text_frame.text for shape in slide.shapes if shape.has_text_frame)
    assert '수집된 구성 정보 없음' in text and '확인 필요' in text and calls == []


def test_long_type_lists_are_shortened_not_shrunk_so_the_card_text_stays_at_ten_points_or_more():
    nodes = [{'layer': 'edge', 'label': '진입·보호', 'count': 14, 'types': ['publicipaddresses', 'certificateorders', 'domains']},
             {'layer': 'network', 'label': '네트워크 경계', 'count': 40, 'types': ['networkinterfaces', 'networksecuritygroups', 'privateendpoints']}]
    slide, _ = draw('architecture', {'focus_layers': ['edge'], 'gap_layers': []}, nodes=nodes, edges=[])
    assert min(sizes(slide)) >= 10
    inside_body(slide)


def test_a_connector_never_crosses_a_layer_block():
    layout = D.architecture_layout(NODES)
    for edge in EDGES + [{'from': 'compute', 'to': 'observability', 'count': 1}, {'from': 'network', 'to': 'observability', 'count': 1}]:
        points = D.route(layout, edge['from'], edge['to'])
        for start, end in zip(points, points[1:]):
            for layer, (bx, by, bw, bh) in layout.items():
                if layer in (edge['from'], edge['to']):
                    continue
                lo_x, hi_x = sorted((start[0], end[0])); lo_y, hi_y = sorted((start[1], end[1]))
                overlaps = lo_x < bx + bw - .001 and hi_x > bx + .001 and lo_y < by + bh - .001 and hi_y > by + .001
                assert not overlaps, (edge, layer)


def test_a_long_layer_label_takes_two_lines_instead_of_a_smaller_font():
    nodes = [{'layer': 'identity', 'label': '아이덴티티·거버넌스', 'count': 1, 'types': ['관리 ID']}, {'layer': 'network', 'label': '네트워크 경계', 'count': 40, 'types': ['NSG']}]
    slide, _ = draw('architecture', {'focus_layers': ['identity'], 'gap_layers': ['identity']}, nodes=nodes, edges=[], icon=lambda *a: True)
    assert min(sizes(slide)) >= 10
    text = '\n'.join(shape.text_frame.text for shape in slide.shapes if shape.has_text_frame)
    assert '아이덴티티' in text and '확인 필요' in text
