"""진단·과제 산출물 품질 채점기: 입력의 실제 값과 코드 검사로 항목별 통과/실패를 낸다(모델 호출 없음).

원칙:
- 정답은 입력(facts·criteria·goals·directions)에서 계산한다. 모델 판단·승인 결과를 정답으로 쓰지 않는다.
- 합산 점수·순위를 만들지 않는다. 항목별 통과/실패와 사유만 낸다.
- 대조군(승인 산출물)과는 '인용 보존율'로만 비교한다. 문장 유사도로 품질을 재지 않는다.
- 같은 입력에는 같은 결과가 나온다. 입력은 변경하지 않는다.
"""
from __future__ import annotations

import json
import re

# 설명 문장의 숫자만 대상으로 한다. 식별자(D_P0001, F_E_x)·날짜·버전 번호 안의 숫자는 제외한다.
_NUMBER = re.compile(r'(?<![A-Za-z0-9_.\-/])(\d{1,3}(?:,\d{3})+|\d+(?:\.\d+)?)(?![A-Za-z0-9_.\-/])')
_NAME = re.compile(r'(?<![A-Za-z0-9_])[A-Z][A-Za-z0-9]*(?:-[A-Za-z0-9]+)+(?![A-Za-z0-9_])')
_ASSERT = re.compile(r'(전수|모든|전부|빠짐없이|전체)\s*(?:구독|리소스|자원|VM|서버|대)?[^.。]{0,18}(확인|점검|검증|조사)(?:되었|됐|했|완료)')
_NEGATE = re.compile(r'(완전하지|확정하지|판단을? 보류|확인되지|수집되지|미수집|불완전|제한적|단정하지|알 수 없)')
_LIST_KEYS = ('fact_refs', 'criterion_refs', 'goal_refs', 'direction_refs')
_PROSE_KEYS = ('text', 'impact', 'rationale', 'title', 'target', 'deliverable', 'priority_basis', 'verification_condition', 'improvement_points', 'prerequisites')


def _strings(value):
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for v in value.values():
            yield from _strings(v)
    elif isinstance(value, list):
        for v in value:
            yield from _strings(v)


def _prose(candidate):
    parts = []
    for key in _PROSE_KEYS:
        v = candidate.get(key)
        if isinstance(v, str):
            parts.append(v)
        elif isinstance(v, list):
            parts.extend(s for s in _strings(v))
    return '\n'.join(parts)


def _ids(rows):
    return {row['id'] for row in rows or [] if isinstance(row, dict) and 'id' in row}


def _fact_text(facts):
    return '\n'.join(json.dumps(f.get('value'), ensure_ascii=False) for f in facts)


def _row(name, passed, reason='', detail=None, skipped=False):
    row = {'check': name, 'passed': bool(passed), 'reason': reason}
    if detail is not None:
        row['detail'] = detail
    if skipped:
        row['skipped'] = True
    return row


def grade(candidate, data, reference=None):
    """candidate: 진단·과제 1건(dict). data: 모델이 받은 입력(facts·criteria·goals·directions). reference: 대조군 산출물 또는 None."""
    cand = candidate if isinstance(candidate, dict) else {}
    checks = []
    prose = _prose(cand)
    known = {'fact_refs': _ids(data.get('facts')), 'criterion_refs': _ids(data.get('criteria')),
             'goal_refs': _ids(data.get('goals')), 'direction_refs': _ids(data.get('directions'))}

    structure_problems = []
    if not isinstance(cand.get('id'), str) or not cand['id']:
        structure_problems.append('id')
    if not (isinstance(cand.get('text'), str) and cand['text'].strip()) and not (isinstance(cand.get('rationale'), str) and cand['rationale'].strip()):
        structure_problems.append('text')
    checks.append(_row('structure', not structure_problems, ','.join(structure_problems) + ' 비어 있음' if structure_problems else ''))

    ghosts = []
    for key in _LIST_KEYS:
        for ref in cand.get(key, []) or []:
            if ref not in known[key]:
                ghosts.append(ref)
    checks.append(_row('citation_validity', not ghosts, '입력에 없는 참조: ' + ', '.join(sorted(set(ghosts))) if ghosts else ''))

    if reference is None:
        checks.append(_row('citation_retention', True, '대조군 없음', skipped=True))
    else:
        ref_set = set(reference.get('fact_refs', []) or [])
        cand_set = set(cand.get('fact_refs', []) or [])
        missing = sorted(ref_set - cand_set)
        retained = len(ref_set) - len(missing)
        checks.append(_row('citation_retention', not missing, '대조군이 인용한 사실이 빠짐: ' + ', '.join(missing) if missing else '',
                           detail={'retained': retained, 'reference': len(ref_set)}))

    cited_facts = [f for f in data.get('facts', []) if f['id'] in set(cand.get('fact_refs', []) or [])]
    all_text = _fact_text(data.get('facts', []))
    cited_text = _fact_text(cited_facts) or all_text
    id_blob = ' '.join(f['id'] for f in data.get('facts', []))
    stray = []
    for m in _NUMBER.finditer(prose):
        token = m.group(1).replace(',', '')
        if token in all_text.replace(',', '') or token in cited_text.replace(',', ''):
            continue
        # 한 자리 서수·항목 번호는 근거 주장이 아니다
        if token in {'1', '2', '3'} and len(token) == 1:
            continue
        stray.append(m.group(1))
    checks.append(_row('number_grounding', not stray, '입력 사실에 없는 숫자: ' + ', '.join(sorted(set(stray))) if stray else ''))

    names = []
    # 기준(criteria)의 제목·원문·ID에 나오는 설계 원칙 코드(WAF-AI-SE1 등)와 고객 목표 원문도 입력에 있는 이름이다.
    criteria_text = '\n'.join(' '.join(_strings(c)) for c in data.get('criteria', []) or [])
    goals_text = '\n'.join(' '.join(_strings(g)) for g in data.get('goals', []) or [])
    directions_text = '\n'.join(' '.join(_strings(d)) for d in data.get('directions', []) or [])
    for m in _NAME.finditer(prose):
        token = m.group(0)
        if token in all_text or token in id_blob or token in criteria_text or token in goals_text or token in directions_text:
            continue
        names.append(token)
    checks.append(_row('name_grounding', not names, '입력에 없는 이름: ' + ', '.join(sorted(set(names))) if names else ''))

    incomplete = [f['id'] for f in cited_facts if f.get('scope_complete') is False]
    flagged = []
    if incomplete and prose:
        for sentence in re.split(r'(?<=[.。!?])\s+|\n', prose):
            if _ASSERT.search(sentence) and not _NEGATE.search(sentence):
                flagged.append(sentence.strip()[:80])
    checks.append(_row('incomplete_scope_assertion', not flagged,
                       '수집 범위가 완전하지 않은 사실(' + ', '.join(incomplete) + ')을 확정 서술: ' + ' / '.join(flagged[:2]) if flagged else ''))

    try:
        from asis_language_guard import find_language_defects
        defects = find_language_defects(cand, allowed_text=[prose_text for prose_text in _strings(data.get('goals', []))])
    except Exception as exc:  # 채점기는 검사 실패를 통과로 바꾸지 않는다
        defects = [{'why': 'language_check_error:' + str(exc)[:60]}]
    checks.append(_row('language', not defects, '일본어·한자 혼입 %d건' % len(defects) if defects else ''))
    return {'checks': checks}
