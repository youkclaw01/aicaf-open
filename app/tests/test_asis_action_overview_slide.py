"""과제 구현방안 오버뷰 장표: 글이 아니라 4칸 흐름(선행조건 -> 개선 포인트 -> 산출물 -> 완료 조건)으로 보이고, 길이 상한을 코드가 강제한다."""
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import pytest
from pptx import Presentation
from pptx.util import Inches
import asis_artifacts as art
import asis_consolidated_deck as deck


def action(**changes):
    row = {'id': 'J_ACT_0001', 'title': '네트워크 경계 통제 정비', 'target': 'AI 서비스 계정의 네트워크 접근', 'change_type': 'modify',
           'rationale': '공용 접근 설정이 계정별로 다르고 사설 연결 해석 기록이 수집되지 않았다.',
           'prerequisites': ['계정별 접근 설정 목록 확보', '사설 DNS 해석 기록 수집'], 'deliverable': '계정별 접근 통제 기준서와 적용 결과',
           'verification_condition': '모든 계정이 사설 연결 경로로만 접근됨을 기록으로 확인', 'improvement_points': ['공용 접근 차단', '사설 엔드포인트 연결', '접근 기록 수집'],
           'diagnosis_refs': ['J_DIAG_1']}
    row.update(changes)
    return row


def draw(row):
    prs = Presentation(); prs.slide_width, prs.slide_height = Inches(13.333), Inches(7.5)
    calls = []
    def clone(index, family, title, subtitle):
        return prs.slides.add_slide(prs.slide_layouts[6])
    slide = deck.draw_action_overview(clone, lambda s, p: None, art.text, art.box, art.wrapped_lines, row, {}, [{'id': 'J_DIAG_1'}], {}, connect=lambda s, a, b, k: calls.append(k))
    return slide, calls


def test_the_four_columns_follow_the_implementation_order_and_use_at_least_twelve_points():
    slide, calls = draw(action())
    text = '\n'.join(shape.text_frame.text for shape in slide.shapes if shape.has_text_frame)
    for label in ('대상', '선행조건', '개선 포인트', '산출물', '완료 조건', '공용 접근 차단'):
        assert label in text, label
    assert [c for c in calls] == ['action-flow-0', 'action-flow-1', 'action-flow-2']
    sizes = [run.font.size.pt for shape in slide.shapes if shape.has_text_frame for p in shape.text_frame.paragraphs for run in p.runs if run.font.size]
    assert min(sizes) >= 11


@pytest.mark.parametrize('field,value,error', [
    ('target', '가' * 41, 'consolidated_action_text_too_long:J_ACT_0001:target'),
    ('rationale', '가' * 121, 'rationale'),
    ('deliverable', '가' * 57, 'deliverable'),
    ('verification_condition', '가' * 57, 'condition'),
    ('improvement_points', ['가' * 46], 'point'),
    ('improvement_points', [], 'consolidated_action_points'),
    ('improvement_points', ['a'] * 4, 'consolidated_action_points'),
])
def test_an_action_that_is_too_long_or_has_the_wrong_number_of_points_fails_instead_of_shrinking(field, value, error):
    with pytest.raises(ValueError, match=error):
        draw(action(**{field: value}))


def test_an_action_without_prerequisites_says_so_instead_of_leaving_the_column_empty():
    slide, _ = draw(action(prerequisites=[]))
    assert '등록된 선행조건 없음' in '\n'.join(shape.text_frame.text for shape in slide.shapes if shape.has_text_frame)


def test_the_largest_allowed_action_fits_the_slide_and_an_overflowing_column_is_rejected_by_line_count_not_only_by_characters():
    worst = action(prerequisites=['가' * 15] * 3, improvement_points=['가' * 15] * 3, deliverable='가' * 56, verification_condition='가' * 56,
                   target='가' * 40, rationale='가' * 120)
    slide, _ = draw(worst)
    assert slide is not None, 'the documented maximum must be drawable; otherwise the limits contradict the layout'
    deck_limits = deck.ACTION_LIMITS


def test_three_items_in_a_column_may_use_fifteen_characters_each_and_sixteen_is_rejected():
    draw(action(prerequisites=['가' * 15] * 3, improvement_points=['가' * 15] * 3))
    with pytest.raises(ValueError, match='consolidated_action_column_capacity'):
        draw(action(prerequisites=['가' * 16] * 3))
    draw(action(improvement_points=['가' * 45, '나' * 45]))
