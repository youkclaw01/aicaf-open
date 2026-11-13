"""고객용 한국어 본문에 섞인 일본어·한자·비정상 문자를 모델 검토 전에 결정적으로 찾는다.

공식 서비스 명칭·원문 인용·식별자는 예외다. 후보의 어느 위치(where)와 원문(quote)이 문제인지 반환하며,
반환 형식은 독립 검토와 같아 작성자에게 위치·원문·사유·수정 방향으로 그대로 전달된다.
"""
from __future__ import annotations
import re

# 히라가나·가타카나·반각 가타카나와 CJK 한자(호환 한자 포함)만 대상으로 한다. 한글·영문·숫자·일반 문장부호는 허용한다.
_KANA = re.compile(r'[\u3040-\u309f\u30a0-\u30ff\u31f0-\u31ff\uff66-\uff9f]')
_HAN = re.compile(r'[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff]')
_JP_PUNCT = re.compile(r'[\u3001\u3002\u300c-\u300f\u30fb]')
# 원문·고유 이름이 담기는 필드: 모델 문장이 아니라 인용/식별자이므로 검사하지 않는다.
_SKIP_FIELDS = frozenset({'quote', 'id', 'source_id', 'field_path', 'source_url', 'value', 'raw', 'evidence_refs'})
_REF_SUFFIX = ('_refs', '_ids')


def _walk(value, where, out):
    if isinstance(value, str):
        out.append((where, value))
    elif isinstance(value, dict):
        for key, child in value.items():
            if key in _SKIP_FIELDS or key.endswith(_REF_SUFFIX):
                continue
            _walk(child, where + '/' + str(key), out)
    elif isinstance(value, list):
        for index, child in enumerate(value):
            _walk(child, where + '/' + str(index), out)


def _allowed_literals(allowed_text):
    """같은 입력에 실제로 존재하는 한자·가나 표현(예: 인용된 고객 원문, 공식 명칭)은 허용 목록으로 만든다."""
    found = set()
    for text in allowed_text:
        for match in re.finditer(r'[\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\uff66-\uff9f]+', text or ''):
            found.add(match.group(0))
    return found


def find_language_defects(candidate, *, allowed_text=()):
    """후보의 문장 필드에서 일본어·한자 혼입을 찾아 검토 결함 형식으로 반환한다."""
    allowed = _allowed_literals(allowed_text)
    rows = []
    _walk(candidate, '', rows)
    defects = []
    for where, text in rows:
        hits = []
        for pattern in (_KANA, _HAN, _JP_PUNCT):
            for match in pattern.finditer(text):
                hits.append(match)
        if not hits:
            continue
        # 허용 목록에 있는 연속 문자열 안의 문자는 정상 원문 인용으로 본다.
        bad = []
        for match in sorted(hits, key=lambda m: m.start()):
            start = match.start()
            run = re.search(r'[\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\uff66-\uff9f]+', text[start:])
            literal = run.group(0) if run else match.group(0)
            if any(literal in item or item in literal for item in allowed):
                continue
            bad.append(match.group(0))
        if not bad:
            continue
        sample = ''.join(dict.fromkeys(bad))[:12]
        defects.append({'where': where or '/', 'quote': text[:400], 'evidence_refs': [],
                        'why': '고객용 한국어 본문에 일본어·한자 문자(' + sample + ')가 혼입되었다. 공식 서비스 명칭이나 원문 인용이 아니다.',
                        'fix': '해당 표현을 자연스러운 한국어로 바꾸고 나머지 내용·근거 참조는 그대로 유지한다.'})
    return defects


def allowed_text_from_input(data):
    """입력에서 원문 인용으로 쓸 수 있는 문자열(고객 목표 인용·사실 값·기준 원문)을 모은다."""
    texts = []
    for goal in data.get('goals', []) or []:
        texts.append(goal.get('quote', ''))
    for family in ('facts', 'criteria'):
        for row in data.get(family, []) or []:
            for key in ('quote', 'title', 'name', 'source_url'):
                if isinstance(row.get(key), str):
                    texts.append(row[key])
            value = row.get('value')
            if isinstance(value, str):
                texts.append(value)
    for row in (data.get('context', {}).get('goal_interpretations') or []):
        for key in ('quote', 'goal_text'):
            if isinstance(row.get(key), str):
                texts.append(row[key])
    return texts
