"""장표 글자 크기 규칙: 본문·도식 라벨은 10pt 미만으로 줄이지 않는다(영석님 지시). 꼬리말(쪽 번호·다음 상세)만 템플릿 규격 9pt를 유지한다.

생성 코드의 텍스트 호출 인수에서 10pt 미만 글자 크기가 다시 생기면 실패한다. 이 시험은 소스를 읽어 확인하므로 실제 렌더와 별개로 회귀를 막는다.
"""
from pathlib import Path
import re
import sys

APP = Path(__file__).resolve().parents[1]
FILES = ('asis_consolidated_deck.py', 'asis_approved_presentation.py', 'asis_report_visuals.py', 'asis_artifacts.py', 'asis_overview_architecture.py')
FOOTER_MARKERS = ('다음 상세', "{i:02d} / ", "{i+1:02d} / ", 'AI Assessment · 현황 분석 보고서')
SIZE_ARGUMENT = re.compile(r",\s*(\d(?:\.\d+)?)\s*,\s*(?:True|False)")
WRAPPED_ARGUMENT = re.compile(r"wrapped_lines\([^)]*?,\s*(\d(?:\.\d+)?)\s*\)")


def small_sizes(name):
    found = []
    for number, line in enumerate((APP / name).read_text(encoding='utf-8').splitlines(), 1):
        if any(marker in line for marker in FOOTER_MARKERS):
            continue
        for pattern in (SIZE_ARGUMENT, WRAPPED_ARGUMENT):
            for match in pattern.finditer(line):
                if float(match.group(1)) < 10:
                    found.append((name, number, match.group(1)))
    return found


def test_no_generated_body_or_diagram_text_is_below_ten_points():
    findings = [item for name in FILES if (APP / name).exists() for item in small_sizes(name)]
    assert not findings, 'text below 10pt: %s' % findings[:10]


def test_the_body_minimum_is_declared_once_and_the_footer_keeps_the_template_size():
    sys.path.insert(0, str(APP))
    import asis_consolidated_deck as deck
    assert deck.BODY_MIN_PT == 10.0 and deck.FOOTER_PT == 9.0
