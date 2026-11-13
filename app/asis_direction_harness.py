"""방향 작성 하네스: luna 가 쓴 방향 후보를 코드가 먼저 점검해 검토 전에 고치게 한다. 모델 호출 없이 입력에서 계산하는 순수 코드다.

1) 작업표(work_table): 슬롯의 판단 항목·금지 사항·고객 진술 원문 표(극성 표시)·부분 수집 사실을 코드가 계산해 작성 지시문 뒤에 붙인다.
2) 검사(run_checks): hard(필수 참조·없는 ID·입력에 없는 이름·지역·위치·한자 혼입)와 soft(극성 의심·항목 누락 의심·미확인 합침/중복·부분 수집 한계 누락).
   hard 는 검토 전에 작성자에게 되돌린다. soft 는 의심 신호라서 되돌리지 않고 검토자 입력의 참고로만 쓴다.
검토는 전체 입력을 보는 독립 검토자(sol xhigh)가 맡는다. 발췌본 검토는 근거 누락으로 판정이 갈려 쓰지 않는다.

시험 근거(2026-10-17, 같은 10개 방향 입력, 사실 원문 대조 채점): 이 하네스+가드를 붙인 luna high 가 현행 sol 구성과 같은 수준의 점수를 내고 비용은 약 2/3.
알려진 한계: 진술 반전(극성이 문맥으로 뒤집힌 경우)과 판단 항목의 의미적 누락은 코드로 못 잡는다. 이 둘은 독립 검토의 몫이다.
"""
import json
import re

NEG = re.compile(r'않는다|않습니다|않아요|않고|않으며|않은|안 쓰|안 쓴|안 씁|안 합|안 해|못 |없어요|없습니다|없다|없고|모르겠|모릅니다|미정|아직 없|아직 안|정하지|따로 없|따로 모|따로 안|아니')
PROHIB = re.compile(r'않는다|않으며|않고|확정하지|생성하지|추정하지|간주하지|단정하지|정하지|전환하지|판단하지|대신하지|확대하지|한다고 보지|으로 보지|로 보지')
LIMIT = re.compile(r'부분|미확인|검증(?:되지|된 것은)|확정할 수 없|확인되지|판단할 수 없|불완전|제한적|구성만으로|한정|입증하지|아니다')
# H4 검사별 수정 지시. 통과 기준(어떤 검사가 실패인가)은 채점기가 정하고, 여기서는 작성자에게 주는 수정 방법만 정한다.
# 운영 2026-10-18 D_P0081: 부분 수집 사실의 확정 서술(incomplete_scope_assertion)에 '이름·숫자를 지운다'는 지시가 붙어
# 작성자가 3라운드 연속 같은 문장을 다시 냈다. 결함 종류와 지시가 맞아야 한 번에 고쳐진다.
H4_FIX = {
    'name_grounding': '입력에 없는 이름·숫자를 지우거나 입력에 있는 표기로 바꾼다',
    'number_grounding': '입력에 없는 이름·숫자를 지우거나 입력에 있는 표기로 바꾼다',
    'incomplete_scope_assertion': ('인용에 적힌 문장(수집 범위가 완전하지 않은 사실을 확정한 문장)만 고친다. 그 문장을 "수집된 범위에서 관측되었다"로 한정하고, '
                                   '전체·실제 동작이 검증되지 않았다는 한계를 같은 문장에 쓴다. 이름·숫자는 바꾸지 않는다'),
}
HAN = re.compile(r'[\u4e00-\u9fff\u3400-\u4dbf\u3040-\u30ff]')
RISKY = ['지역 선정', '지역 선택', '리전', '포털 DB', '서버 로그 파일', '국내 기준에 따른']
# 기준 풀네임은 입력에 기준 ID 접두가 있으면 근거가 있는 표기로 본다(예: CAF-AI-GOV1 -> Cloud Adoption Framework).
FRAMEWORK_NAMES = {'Well-Architected': 'WAF-', 'Cloud Adoption Framework': 'CAF-', 'AI Landing Zone': 'AILZ-', 'Microsoft Cloud Security Benchmark': 'MCSB-'}
STOP = set('그리고 그러나 하지만 있는 있다 없는 없다 한다 하는 해서 에서 으로 에게 대한 대해 통해 같은 이다 입니다 있어요 없어요 해요 이에요 합니다 있습니다 또는 이미 아직 가끔 위해 이를 이는 이와 하며 하고 하여 한편 다만 따라서 그러므로 포함 관련 경우 필요 여부 대상 범위 확인 판단 조건 기준 방향'.split())


def sentences(text):
    parts = re.split(r'(?<=[.!?])\s+|(?<=요)\s+(?=[가-힣])|\n+', text or '')
    return [p.strip() for p in parts if len(p.strip()) >= 6]


def toks(s):
    out, seen = [], set()
    for w in re.findall(r'[가-힣A-Za-z0-9]{2,}', s or ''):
        if w in STOP or w in seen:
            continue
        seen.add(w)
        out.append(w)
    return out


def stem(w):
    return w[:2] if len(w) <= 4 else w[:3]


def key_stems(s, n=3):
    t = sorted(toks(s), key=lambda w: -len(w))[:n]
    return [stem(w) for w in t]


def customer_sentences(fact):
    v = fact.get('value')
    if not isinstance(v, str):
        return []
    v = re.sub(r'^\[고객\]\s*', '', v.strip())
    return sentences(v)


def work_table(ctx):
    """작업표: 슬롯의 판단 항목·금지 사항·고객 진술 원문 표·부분 수집 사실. 입력에서 코드로 계산한다."""
    slot = ctx['slot']
    scope_items = [x.strip() for x in re.split(r'[·,，、]|\s및\s|(?<=[가-힣])과\s|(?<=[가-힣])와\s', slot.get('scope') or '') if len(x.strip()) >= 4]
    sents = sentences(slot.get('reason') or '')
    prohib = [s for s in sents if PROHIB.search(s)]
    oblig = [s for s in sents if not PROHIB.search(s)]
    lines = ['## 이 항목의 작업표 (코드가 입력에서 계산한 것이며 모두 지킨다)', '']
    lines.append('### A. 판단 항목 체크리스트 — 항목마다 text 또는 change_direction 에 다루는 문장이 있어야 하고, 근거가 모자라면 unresolved 에 항목명으로 남긴다')
    n = 0
    for it in scope_items:
        n += 1
        lines.append(f'A{n}. {it}')
    for s in oblig:
        n += 1
        lines.append(f'A{n}. {s}')
    lines.append('')
    lines.append('### B. 금지 사항 — 하나라도 어기면 폐기된다')
    for i, s in enumerate(prohib, 1):
        lines.append(f'B{i}. {s}')
    lines.append('B*. 입력의 사실에 없는 위치·시스템명·제품명·지역·숫자를 쓰지 않는다. 기준 이름은 criteria 에 적힌 표기 또는 기준 ID 만 쓴다.')
    lines.append('')
    lines.append('### C. 고객 진술 원문 표 — 본문에서 이 진술을 옮길 때는 극성(긍정/부정/유보)과 대상·범위를 표의 원문 그대로 유지한다. 의역으로 부정을 긍정으로, 모름을 없음으로, 일부를 전부로 바꾸지 않는다')
    k = 0
    pairs = []
    for fid in slot['fact_refs']:
        f = ctx['facts'].get(fid)
        if not f or not str(fid).startswith('F_IV_'):
            continue
        ss = customer_sentences(f)
        for i, s in enumerate(ss):
            k += 1
            neg = bool(NEG.search(s))
            tag = '부정/모름' if neg else '긍정/서술'
            lines.append(f'C{k}. [{fid}] ({tag}) "{s}"')
            if neg and i > 0 and not NEG.search(ss[i - 1]):
                pairs.append((k - 1, k, fid))
    if k == 0:
        lines.append('(이 항목의 인용 사실에 고객 진술이 없다)')
    if pairs:
        lines.append('')
        lines.append('C-짝. 같은 답변 안에서 긍정 문장과 부정 문장이 이어진다. 두 문장을 하나로 합치거나 부정을 앞 문장의 대상에 옮기지 말고, 각각 따로 옮긴다:')
        for a, b, fid in pairs:
            lines.append(f'  - C{a}(긍정)과 C{b}(부정)는 서로 다른 대상이다. "{b}"의 부정은 C{b} 문장이 말한 대상에만 적용된다.')
    lines.append('')
    incomplete = [fid for fid in slot['fact_refs'] if ctx['facts'].get(fid, {}).get('scope_complete') is False]
    lines.append('### D. 부분 수집 사실 — 이 사실을 쓸 때는 "수집된 범위에서 관측" 또는 "부분 수집"을 반드시 쓰고, 전체·실제 동작의 증거로 쓰지 않는다')
    lines.append(', '.join(incomplete) if incomplete else '(없음)')
    lines.append('')
    lines.append('### E. 작성 순서 — 이 순서로 쓴다')
    lines.append('1) C의 고객 진술을 원문 극성 그대로 한두 문장으로 요약한다. 2) D의 수집 사실은 상태·범위 한정을 붙여 쓴다. 3) 기준(criteria)은 should/consider 수준 그대로 쓴다. '
                 '4) A의 항목을 순서대로 change_direction 에 한 문장 이상씩 쓴다. 5) A의 항목마다 근거로 확정할 수 없는 부분이 있으면 unresolved 한 건씩 쓴다. '
                 '증거가 같은 공백은 한 건, 필요한 증거가 다르면 별도 건으로 쓴다. 6) 끝내기 전에 C의 부정/모름 진술이 본문에서 부정/모름으로 남아 있는지, B의 금지 사항을 어기지 않았는지 확인한다.')
    return '\n'.join(lines), {'scope_items': scope_items, 'oblig': oblig, 'prohib': prohib}


def all_text(doc):
    d = (doc.get('directions') or [{}])[0]
    return ' '.join(str(d.get(k) or '') for k in ('title', 'text', 'customer_impact', 'change_direction')) + ' ' + json.dumps(doc.get('unresolved') or [], ensure_ascii=False)


def run_checks(doc, ctx, tinfo=None, grader=None):
    hard, soft = [], []
    slot = ctx['slot']
    dirs = doc.get('directions') or []
    if len(dirs) != 1 or dirs[0].get('id') != slot['id']:
        hard.append({'code': 'H1', 'where': '/directions', 'quote': '', 'why': '방향이 정확히 1건이어야 하고 id 가 슬롯과 같아야 한다', 'fix': 'directions 를 슬롯 id 한 건으로 쓴다'})
        return {'hard': hard, 'soft': soft}
    d = dirs[0]
    for k in ('title', 'text', 'customer_impact', 'change_direction'):
        if not str(d.get(k) or '').strip():
            hard.append({'code': 'H1', 'where': '/directions/0/' + k, 'quote': '', 'why': '필수 필드가 비었다', 'fix': k + ' 를 채운다'})
    for fld in ('fact_refs', 'criterion_refs', 'goal_refs', 'area_implication_refs'):
        miss = sorted(set(slot.get(fld, [])) - set(d.get(fld, [])))
        if miss:
            hard.append({'code': 'H2', 'where': '/directions/0/' + fld, 'quote': ', '.join(miss)[:120], 'why': '슬롯이 지정한 필수 참조가 빠졌다', 'fix': fld + ' 에 ' + ', '.join(miss) + ' 를 추가한다'})
    for fld, known in (('fact_refs', set(ctx['facts'])), ('criterion_refs', set(ctx['criteria'])), ('goal_refs', set(slot.get('goal_refs', []))), ('area_implication_refs', set(slot.get('area_implication_refs', [])))):
        unk = sorted(set(d.get(fld, [])) - known)
        if unk:
            hard.append({'code': 'H3', 'where': '/directions/0/' + fld, 'quote': ', '.join(unk)[:120], 'why': '입력에 없는 참조를 인용했다', 'fix': '해당 참조를 지운다'})
    text = all_text(doc)
    prompt = ctx['prompt']
    # H4 코드 채점기 (이름·숫자·언어)
    if grader is not None:
        cand = dict(d)
        cand['direction_refs'] = list(d.get('area_implication_refs', []))
        gi = {'facts': ctx['facts_list'], 'criteria': ctx['criteria_list'], 'goals': [{'id': x} for x in slot.get('goal_refs', [])],
              'directions': [{'id': i, 'fact_refs': []} for i in set(slot.get('area_implication_refs', []))]}
        res = grader.grade(cand, gi, reference=None)
        for r in res['checks']:
            if not r['passed'] and not r.get('skipped') and r['check'] in ('name_grounding', 'number_grounding', 'incomplete_scope_assertion'):
                hard.append({'code': 'H4', 'where': '/directions/0', 'quote': r['reason'][:140], 'why': r['check'] + ' 실패',
                             'fix': H4_FIX.get(r['check'], H4_FIX['name_grounding'])})
    han = sorted({c for c in HAN.findall(json.dumps(doc, ensure_ascii=False)) if c not in prompt})
    if han:
        hard.append({'code': 'H5', 'where': '/', 'quote': ''.join(han), 'why': '한자·일본어 문자가 섞였다', 'fix': '해당 단어를 한국어로 바꾼다'})
    for w in RISKY:
        if w in text and w not in prompt:
            i = text.find(w)
            hard.append({'code': 'H6', 'where': '/directions/0', 'quote': text[max(0, i - 25):i + len(w) + 25], 'why': f"입력에 없는 표현 '{w}' 을 추가했다(위치·지역·이름 추가 금지)", 'fix': '입력의 사실이 말한 대상 그대로 바꾼다'})
    for nm, prefix in FRAMEWORK_NAMES.items():
        if nm in text and nm not in prompt and prefix not in prompt:
            i = text.find(nm)
            hard.append({'code': 'H6', 'where': '/directions/0', 'quote': text[max(0, i - 25):i + len(nm) + 25], 'why': f"입력에 근거가 없는 프레임워크 이름 '{nm}'", 'fix': '입력의 기준 ID 만 쓴다'})
    for j, u in enumerate(doc.get('unresolved') or []):
        bad = [r for r in u.get('fact_refs', []) if r not in ctx['facts']]
        if bad:
            hard.append({'code': 'H7', 'where': f'/unresolved/{j}/fact_refs', 'quote': ', '.join(bad)[:100], 'why': '입력에 없는 사실 참조', 'fix': '지운다'})
    # soft S1 극성 의심: 부정/모름 고객 진술이 본문에서 부정 표현 없이 옮겨졌는가
    body = ' '.join(str(d.get(k) or '') for k in ('text', 'customer_impact', 'change_direction'))
    for fid in d.get('fact_refs', []):
        f = ctx['facts'].get(fid)
        if not f or not str(fid).startswith('F_IV_'):
            continue
        for s in customer_sentences(f):
            if not NEG.search(s):
                continue
            ks = key_stems(s, 3)
            if len(ks) < 2:
                continue
            for m in re.finditer(re.escape(ks[0]), body):
                win = body[max(0, m.start() - 70):m.start() + 110]
                if sum(1 for k in ks if k in win) >= 2 and not NEG.search(win) and not LIMIT.search(win):
                    soft.append({'code': 'S1', 'where': '/directions/0/text', 'quote': win.replace('\n', ' ')[:120], 'why': f'고객 진술 [{fid}] "{s[:50]}" 은 부정/모름인데 이 문장에는 부정·유보 표현이 없다(극성 변경 의심)', 'fix': '진술 원문의 부정·모름을 그대로 쓴다'})
                    break
    # soft S2 판단 항목 누락 의심
    if tinfo:
        items = tinfo['scope_items'] + tinfo['oblig']
        for it in items:
            ks = key_stems(it, 2)
            if ks and not any(k in text for k in ks):
                soft.append({'code': 'S2', 'where': '/directions/0/change_direction', 'quote': it[:80], 'why': '판단 항목의 핵심어가 본문·미확인 어디에도 없다(항목 누락 의심)', 'fix': '이 항목을 change_direction 에 쓰거나, 근거가 없으면 unresolved 에 항목명으로 남긴다'})
    # soft S3 미확인 합침·중복
    un = [u.get('topic') or '' for u in (doc.get('unresolved') or [])]
    for j, t in enumerate(un):
        if t.count('·') >= 3 or t.count(' 및 ') >= 2:
            soft.append({'code': 'S3', 'where': f'/unresolved/{j}/topic', 'quote': t[:90], 'why': '서로 다른 공백 여러 개를 한 건에 합쳤다 의심', 'fix': '필요한 증거가 다른 공백은 별도 건으로 나눈다'})
    for a in range(len(un)):
        for b in range(a + 1, len(un)):
            ta, tb = set(toks(un[a])), set(toks(un[b]))
            if len(ta) >= 3 and len(tb) >= 3 and len(ta & tb) / min(len(ta), len(tb)) >= 0.75:
                soft.append({'code': 'S3', 'where': f'/unresolved/{b}/topic', 'quote': un[b][:90], 'why': f'미확인 {a + 1}번과 같은 공백을 표현만 바꿔 반복한 것으로 의심', 'fix': '필요한 증거가 같으면 한 건으로 합친다'})
    # soft S4 부분 수집 사실을 인용했는데 한계 표현이 없음
    inc = [fid for fid in d.get('fact_refs', []) if ctx['facts'].get(fid, {}).get('scope_complete') is False]
    if inc and not LIMIT.search(body):
        soft.append({'code': 'S4', 'where': '/directions/0/text', 'quote': ', '.join(inc)[:80], 'why': '부분 수집 사실을 인용했는데 한계 표현이 본문에 없다', 'fix': '수집된 범위에서 관측되었다는 한정과 검증되지 않았다는 한계를 쓴다'})
    return {'hard': hard, 'soft': soft}


def context_from_scoped(scoped):
    """운영 방향 입력(asis_synthesis.direction_input 결과)에서 작업표·검사에 쓰는 문맥을 만든다. 모델 뷰 변환 전의 정본 입력을 쓴다."""
    slot = scoped['stage_data']['direction_assignment']['slot']
    facts_list = list(scoped.get('facts', []))
    criteria_list = list(scoped.get('criteria', []))
    return {'slot': slot, 'facts': {f['id']: f for f in facts_list}, 'facts_list': facts_list,
            'criteria': {c['id']: c for c in criteria_list}, 'criteria_list': criteria_list,
            'prompt': json.dumps(scoped, ensure_ascii=False)}


def work_table_for(scoped):
    """작성 지시문 뒤에 붙일 작업표 텍스트."""
    table, _ = work_table(context_from_scoped(scoped))
    return table


def hard_defects(scoped, candidate, grader=None):
    """운영 결정적 결함 형식({where, quote, why, fix, evidence_refs})의 hard 결함 목록. 비어 있으면 통과."""
    ctx = context_from_scoped(scoped)
    _, tinfo = work_table(ctx)
    res = run_checks(candidate, ctx, tinfo, grader)
    return [{'where': x['where'], 'quote': x['quote'], 'why': 'harness:' + x['code'] + ' ' + x['why'], 'fix': x['fix'], 'evidence_refs': []} for x in res['hard']]


def soft_signals(scoped, candidate, grader=None):
    """soft 의심 신호(검토자 참고용). 결함으로 취급하지 않는다."""
    ctx = context_from_scoped(scoped)
    _, tinfo = work_table(ctx)
    return run_checks(candidate, ctx, tinfo, grader)['soft']


DIRECTION_GUARD = '''## 추가 엄수 규칙 (아래 규칙은 위의 모든 지시보다 우선하며, 어기면 이 작성물은 폐기된다)

이 규칙은 같은 입력에서 실제로 발생한 결함 유형에서 만들었다. 하나도 어기지 않는다.

### G1. 고객 진술은 뒤집거나 범위를 바꾸지 않는다
- 인터뷰(transcript) 사실을 인용할 때 원문의 긍정·부정, 대상, 범위, 정도를 그대로 유지한다. "한다"를 "하지 않는다"로, "일부"를 "전체"로, "미정"을 "없음"으로 바꾸지 않는다.
- 문장을 쓰기 전에 해당 진술의 원문 구절을 사실 본문에서 찾아 그 구절에 담긴 부정·한정 표현을 확인한다. 원문이 "A는 쓰고 B는 안 쓴다"이면 A와 B를 각각 그대로 쓴다.
- 입력이 "무엇을 뜻하는지 확정할 수 없다"고 한 진술(예: 판단 기준, 책임자, 적용 범위)은 한 가지 의미로 특정하지 않는다. 원문 표현 그대로 옮기고, 특정은 unresolved로 넘긴다.

### G2. 입력에 없는 정보를 추가하지 않는다
- 저장 위치, 시스템·제품·서비스 이름, 숫자, 날짜, 담당자, 기간, 범위 한정어를 입력의 사실에 없는 상태로 쓰지 않는다. 특히 "포털 DB", "서버 로그 파일" 같은 위치 한정은 해당 사실이 그 위치를 명시한 경우에만 쓴다.
- 기준·프레임워크 이름은 입력의 criteria 또는 사실에 적힌 표기 그대로만 쓴다. 입력에 없는 이름(예: 입력에 적히지 않은 공식 문서 명칭)을 일반 지식으로 보태지 않는다. 필요하면 기준 ID(예: WAF-AI-SE6)로만 가리킨다.
- 일반 지식으로 "대체로 그렇다"는 추론을 사실처럼 쓰지 않는다.

### G3. 슬롯이 요구한 판단을 하나도 빠뜨리지 않는다
- 작성 전에 direction_assignment.slot의 scope와 reason을 읽고, 거기에 나열된 판단 항목(예: 비교 대안, 추적 범위, 권한·책임 확인, 선행 조건)을 번호로 모두 뽑는다.
- 뽑은 항목마다 change_direction 또는 text 또는 unresolved 중 최소 한 곳에 그 항목을 다룬 문장이 있어야 한다. 근거가 없어 판단할 수 없는 항목은 지우지 말고 unresolved에 그 항목명으로 남긴다.
- 비교 대안이 "A 유지, B 적용, A·B 조합"처럼 여러 개로 지정되었으면 모든 대안을 쓴다. 조합·선행 조건(가용 용량 확인 등)을 생략하지 않는다.

### G4. 확인되지 않은 것을 확정하지 않는다
- 사실의 scope_complete가 false이거나 수집이 부분적이면 "확인되었다"가 아니라 "수집된 범위에서 관측되었다"로 쓰고 부분 수집임을 쓴다.
- 구성이 있다는 사실을 실제 동작·적용·효과가 검증된 것으로 쓰지 않는다.
- 입력이 단정을 허용하지 않는 항목은 단정 대신 unresolved에 쓴다. unresolved를 줄이려고 단정하지 않는다. 근거가 모자라면 unresolved가 늘어도 된다.

### G5. 최종 출력 전 자체 점검 (출력에는 쓰지 않고 내부에서 수행)
1. 본문의 모든 고객 진술을 해당 사실 원문과 한 구절씩 대조했다.
2. 본문의 모든 이름·위치·숫자가 입력에 있다.
3. 슬롯의 판단 항목이 모두 text·change_direction·unresolved 중 한 곳에 있다.
4. 단정한 문장마다 근거 사실이 fact_refs에 있고, 그 사실이 단정을 허용한다.
5. 한자·일본어 문자가 없다(고유 표기가 입력에 있는 경우만 예외).
하나라도 실패하면 고쳐서 다시 점검한 뒤 출력한다.

### G6. 독립된 공백은 합치지 않는다 (unresolved 작성 규칙)
- unresolved 한 건은 "서로 다른 사실이 있어야 닫히는 공백" 하나만 담는다. 확인에 필요한 증거나 확인 주체가 다르면 별도 건으로 쓴다.
- 슬롯의 scope·reason에 나열된 판단 항목마다, 입력 근거만으로 확정할 수 없는 부분이 있으면 그 항목명으로 unresolved 를 1건씩 남긴다. 여러 항목을 한 건의 topic 에 "·"로 나열해 합치지 않는다.
- 같은 공백을 표현만 바꿔 두 번 쓰지 않는다. 쪼개는 기준은 "필요한 증거가 다른가"이다.
- 이미 입력 근거로 확정된 것은 unresolved 에 넣지 않는다. 개수를 늘리거나 줄이는 것을 목표로 삼지 않는다.
- 각 건의 reason 에는 어떤 사실이 없어 확정할 수 없는지와 required_evidence 에 필요한 증거를 쓴다.
'''
