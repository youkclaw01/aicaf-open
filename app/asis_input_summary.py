"""입력이 모델 한도에 닿아 길이로 반복 실패한 항목만 코드가 결정적으로 요약해 보내는 규칙(모델 호출 없음, 환각 없음).

운영 2026-10-18(프로젝트 25, 10단계 과제 A_ITEM_0015): 연결 방향 15개·진단 18개를 묶은 과제의 작성 입력이 29.9만 토큰으로 한도(27.2만)를 넘었다.
무손실로 뺄 곳이 없었다(사실 175/182 인용, 영역 9/9 필요, 미확인 겹침 0). 영석님 지시: 과제 수는 늘리지 않고, 이름·숫자 등 변하면 안 되는 내용을 제외하고
길이로 2번 이상 실패한 경우는 요약해서 보낸다.

원칙:
- 요약은 코드가 한다(결정적). 모델에게 요약을 맡기지 않는다.
- ID·참조·이름·숫자·수치·한정 표현이 든 문장은 어떤 단계에서도 지우지 않는다. 영문·숫자 토큰 집합이 요약 전후 같아야 하며 하나라도 빠지면 그 요약은 취소한다.
- 슬롯이 직접 인용한 사실의 값과 고객 진술 원문은 요약하지 않는다.
- 단계는 올라가기만 한다: 1단계(승인 서술문 축약) → 2단계(미확인 이유 축약 + 직접 인용하지 않은 목록형 사실의 대표 행).
"""
import copy
import json
import re

_TOKEN = re.compile(r'[A-Za-z][A-Za-z0-9_./:-]*|\d[\d,._:/-]*')
_SENTENCE = re.compile(r'(?<=[.!?。])\s+|\n+')
_KEEP_MARK = re.compile(r'[A-Za-z0-9]|수집된 범위|부분 수집|검증되지|확정할 수 없|확인되지|판단할 수 없|한정|아니다|않는다|없다')

NARRATIVE_KEYS = ('text', 'change_direction', 'customer_impact', 'impact')


def tokens(value):
    """값 안의 모든 문자열에서 영문·숫자 토큰 집합을 뽑는다(이름·수치 보존 검사용)."""
    found = set()

    def walk(item):
        if isinstance(item, str):
            found.update(_TOKEN.findall(item))
        elif isinstance(item, dict):
            for key, child in item.items():
                walk(key)
                walk(child)
        elif isinstance(item, list):
            for child in item:
                walk(child)
        elif isinstance(item, (int, float)) and not isinstance(item, bool):
            found.add(str(item))
    walk(value)
    return found


def condense_text(text):
    """문장 단위로 쪼개 이름·숫자·한정 표현이 든 문장은 모두 남기고, 그 밖의 문장은 첫 문장만 남긴다."""
    if not isinstance(text, str) or len(text) < 120:
        return text
    sentences = [part.strip() for part in _SENTENCE.split(text) if part.strip()]
    if len(sentences) <= 1:
        return text
    kept = [s for index, s in enumerate(sentences) if index == 0 or _KEEP_MARK.search(s)]
    return ' '.join(kept) if len(kept) < len(sentences) else text


def safe_condense(text, report):
    """요약하되 이 문자열의 영문·숫자 토큰이 하나라도 사라지면 원문으로 되돌린다(필드 단위 검사).

    전체 집합으로 비교하면 같은 숫자가 다른 필드에 남아 있을 때 한 필드의 손실이 가려진다. 위치마다 확인해야 이름·숫자가 어디서도 사라지지 않는다.
    """
    shorter = condense_text(text)
    if shorter == text:
        return text
    lost = _TOKEN_SET(text) - _TOKEN_SET(shorter)
    if lost:
        report['reverted_fields'] += 1
        report.setdefault('lost_tokens', []).extend(sorted(lost)[:5])
        return text
    return shorter


def _TOKEN_SET(text):
    return set(_TOKEN.findall(text))


def _size(value):
    return len(json.dumps(value, ensure_ascii=False, separators=(',', ':')).encode('utf-8'))


def _condense_narratives(value, report, path=''):
    if isinstance(value, dict):
        for key in list(value):
            child = value[key]
            if key in NARRATIVE_KEYS and isinstance(child, str):
                shorter = safe_condense(child, report)
                if shorter != child:
                    report['narratives'] += 1
                    report['narrative_bytes_saved'] += len(child.encode('utf-8')) - len(shorter.encode('utf-8'))
                    value[key] = shorter
            else:
                _condense_narratives(child, report, path + '/' + str(key))
    elif isinstance(value, list):
        for child in value:
            _condense_narratives(child, report, path)


def _condense_unresolved(rows, report):
    for row in rows if isinstance(rows, list) else []:
        if isinstance(row, dict) and isinstance(row.get('reason'), str):
            shorter = safe_condense(row['reason'], report)
            if shorter != row['reason']:
                report['unresolved_reasons'] += 1
                report['unresolved_bytes_saved'] += len(row['reason'].encode('utf-8')) - len(shorter.encode('utf-8'))
                row['reason'] = shorter


def _represent_list_fact(value, sample=3):
    """직접 인용하지 않은 목록형 사실: 행 수·필드 목록·대표 행만 남긴다(전체 원장은 정본에 그대로 있다)."""
    keys = sorted({key for row in value for key in row}) if all(isinstance(row, dict) for row in value) else []
    return {'summarized_list': True, 'row_count': len(value), 'fields': keys, 'sample_rows': copy.deepcopy(value[:sample]),
            'note': '전체 원장은 정본에 있다. 이 사실은 슬롯이 직접 인용하지 않아 행 수·필드·대표 행만 싣는다.'}


def summarize_item_input(view, level, direct_fact_refs=()):
    """전송 뷰 사본을 요약한다. 반환: (요약 뷰, 보고). level 1: 승인 서술문 축약. level 2: 1 + 미확인 이유 축약 + 비직접 목록형 사실 대표 행.
    필드마다 영문·숫자 토큰이 하나라도 사라지면 그 필드는 원문으로 되돌린다(safe_condense)."""
    if level not in (1, 2):
        raise ValueError('invalid_summary_level')
    out = copy.deepcopy(view)
    report = {'level': level, 'narratives': 0, 'narrative_bytes_saved': 0, 'unresolved_reasons': 0, 'unresolved_bytes_saved': 0,
              'list_facts': 0, 'list_fact_bytes_saved': 0, 'reverted_fields': 0}
    stage = out.get('stage_data', {})
    for name in ('approved_areas', 'approved_directions', 'approved_diagnoses'):
        if name in stage:
            _condense_narratives(stage[name], report)
    if level >= 2:
        for name in ('approved_areas', 'approved_directions', 'approved_diagnoses'):
            if isinstance(stage.get(name), dict):
                _condense_unresolved(stage[name].get('unresolved'), report)
        direct = set(direct_fact_refs)
        for fact in out.get('facts', []):
            value = fact.get('value')
            if fact['id'] in direct or not isinstance(value, list) or len(value) <= 3:
                continue
            if not all(isinstance(row, dict) for row in value):
                continue
            replaced = _represent_list_fact(value)
            if _size(replaced) < _size(value):
                report['list_facts'] += 1
                report['list_fact_bytes_saved'] += _size(value) - _size(replaced)
                fact['value'] = replaced
    # 이름·숫자 보존은 필드 단위(safe_condense)에서 이미 보장한다. 여기서는 합계만 기록한다.
    report['bytes_saved'] = (report['narrative_bytes_saved'] + report['unresolved_bytes_saved'] + report['list_fact_bytes_saved'])
    return out, report


def _drop_redundant_sentences(stage, report, size_of, target_bytes=None, unit=lambda text: len(text.encode('utf-8'))):
    """서술문 필드마다 '첫 문장은 남기고, 지워도 그 필드의 영문·숫자 토큰이 하나도 줄지 않는 문장'만 뒤에서부터 지운다.
    target_bytes 가 있으면 전체 크기가 목표 이하가 되는 즉시 멈춘다(필요한 만큼만 줄인다).

    운영 실측(프로젝트 25 11단계): 서술문 3,089문장 중 이 조건의 문장이 1,976개(37만 바이트)였고, 한도까지 필요한 5.4만 바이트는 그 19%였다."""
    fields = []

    def collect(value):
        if isinstance(value, dict):
            for key, child in value.items():
                if isinstance(child, str) and key in NARRATIVE_KEYS:
                    fields.append((value, key))
                else:
                    collect(child)
        elif isinstance(value, list):
            for child in value:
                collect(child)
    collect(stage)
    removed = saved = 0
    for owner, key in fields:
        if target_bytes is not None and size_of() <= target_bytes:
            break
        sentences = [s for s in _SENTENCE.split(owner[key]) if s.strip()]
        if len(sentences) < 2:
            continue
        keep = list(range(len(sentences)))
        for index in range(len(sentences) - 1, 0, -1):               # 첫 문장(0)은 절대 지우지 않는다
            rest = [sentences[j] for j in keep if j != index]
            if S_TOKENS(sentences[index]) <= set().union(*[S_TOKENS(s) for s in rest]):
                keep.remove(index)
                if target_bytes is not None and size_of() - (sum(unit(sentences[j]) for j in range(len(sentences))) - sum(unit(sentences[j]) for j in keep)) <= target_bytes:
                    break
        if len(keep) == len(sentences):
            continue
        new = ' '.join(sentences[j] for j in keep)
        saved += unit(owner[key]) - unit(new); removed += len(sentences) - len(keep)
        owner[key] = new
    report['redundant_sentences_removed'] = report.get('redundant_sentences_removed', 0) + removed
    report['redundant_bytes_saved'] = report.get('redundant_bytes_saved', 0) + saved


def S_TOKENS(text):
    return tokens(text)


def summarize_summary_input(view, level, target_bytes=None, measure=None):
    """11단계 요약 작성·검토의 전송 뷰를 요약한다. 반환: (요약 뷰, 보고).

    운영 2026-10-18(프로젝트 25): 요약 입력 34.3만 토큰(한도 27.2만). 승인 방향 86개·진단 100개의 서술문 83만 바이트가 입력의 60%였다.
    level 1: 승인 서술문(방향·진단·영역 시사점)을 이름·숫자 보존 규칙(safe_condense)으로 축약한다(34.3만 → 27.7만 토큰, 이름·숫자 손실 0).
    level 2: 1 + 같은 필드 안에서 이름·숫자가 모두 다른 문장에 있는 중복 문장만 뒤에서부터 지운다(첫 문장은 유지). target_bytes 에 닿으면 멈춘다.
    ID·참조·과제 목록·미확인(이미 주제만 남은 상태)은 건드리지 않는다. 필드마다 영문·숫자 토큰이 줄면 그 필드는 원문으로 되돌린다."""
    if level not in (1, 2):
        raise ValueError('invalid_summary_level')
    out = copy.deepcopy(view)
    report = {'level': level, 'narratives': 0, 'narrative_bytes_saved': 0, 'reverted_fields': 0}
    stage = out.get('stage_data', {})
    for name in ('approved_areas', 'approved_directions', 'approved_actions'):
        if name in stage:
            _condense_narratives(stage[name], report)
    if level >= 2:
        # 중복 문장 제거는 승인 입력 블록에만 건다. 요약 검토의 draft(검토 대상 후보)는 어떤 경우에도 변형하지 않는다: 검토자는 후보의 실제 문장을 대조해야 한다.
        # (7차 재개 직전 시험이 잡은 결함: stage_data 전체를 훑어 draft 의 key_points 마지막 문장이 지워진 채 검토자에게 전달됐다.)
        approved = {name: stage[name] for name in ('approved_areas', 'approved_directions', 'approved_actions') if name in stage}
        # target_bytes 와 measure 는 같은 단위여야 한다. measure 는 (뷰) -> 크기, 기본은 UTF-8 바이트. 모델 한도는 글자 수로 재므로 build 는 글자 수 측정을 넘긴다.
        if measure is None:
            _drop_redundant_sentences(approved, report, lambda: _size(out), target_bytes)
        else:
            _drop_redundant_sentences(approved, report, lambda: measure(out), target_bytes, unit=len)
    report['bytes_saved'] = report['narrative_bytes_saved'] + report.get('redundant_bytes_saved', 0)
    return out, report
