"""통합 PPT 장표 생성기: 영역별 '진단 결과 → 도출된 시사점' 합본, 시사점 종합 1장, 과제 도출 1장, 과제 구현방안 오버뷰.

항목(이슈·방향·관찰) 단위 장표를 만들지 않는다. 상세는 Excel에 두고 PPT는 통합 진단 주제와 통합 과제만 담는다.
이 모듈은 장표 배치(데이터 → 텍스트 블록 → 장표 수)를 결정론적으로 계산하는 순수 함수와, python-pptx로 그리는 함수를 분리한다.
"""
from copy import deepcopy

# 장표는 도식이 중심이고 글은 핵심만 담는다(영석님 지시). 글자 수 상한은 코드가 강제하고 넘으면 요약을 다시 쓰게 한다.
# 장수는 계획 단계(asis_consolidate.deck_page_budget)가 정한 개수와 같아야 하며 계속 장표로 몰래 늘리지 않는다.
BODY_MIN_PT = 10.0         # 본문·도식 라벨의 최소 글자 크기(영석님 지시: 10pt 미만 금지)
FOOTER_PT = 9.0            # 꼬리말(쪽 번호·다음 상세)만 템플릿 규격을 유지한다
SLIDE_TEXT_LIMITS = {'headline': 60, 'finding': 140, 'impact': 90, 'insight': 120, 'change': 90, 'action_point': 70}
SLIDE_FINDINGS_MAX = 3      # 진단 결과 핵심 문장 수
SLIDE_INSIGHTS_MAX = 2      # 도출된 시사점 수(영역 시사점 headline 기준)
ACTION_POINTS_MAX = 4       # 과제 구현방안 핵심 포인트 수


def implication_refs_of(diagnosis, directions_by_id):
    """진단이 연결한 종합 방향들이 인용한 영역 시사점 ID를 코드가 복원한다(순서 보존, 중복 제거). 모델이 고르지 않는다."""
    refs = []
    for direction_id in diagnosis['direction_refs']:
        for implication in directions_by_id.get(direction_id, {}).get('area_implication_refs', []):
            if implication not in refs:
                refs.append(implication)
    return refs


def to_polite_korean(value):
    """컨설팅 보고서용 경어체('~합니다', '~입니다') 또는 개조식 명사구로 문말을 결정론적으로 정규화한다."""
    import re
    s = str(value or '')
    if not s.strip():
        return s
    rules = [
        (r'아니다(?=\.|$)', '아닙니다'),
        (r'이다(?=\.|$)', '입니다'),
        (r'인다(?=\.|$)', '입니다'),
        (r'있다(?=\.|$)', '있습니다'),
        (r'없다(?=\.|$)', '없습니다'),
        (r'않는다(?=\.|$)', '않습니다'),
        (r'않다(?=\.|$)', '않습니다'),
        (r'못한다(?=\.|$)', '못합니다'),
        (r'한다(?=\.|$)', '합니다'),
        (r'된다(?=\.|$)', '됩니다'),
        (r'됐다(?=\.|$)', '되었습니다'),
        (r'되었다(?=\.|$)', '되었습니다'),
        (r'았다(?=\.|$)', '았습니다'),
        (r'었다(?=\.|$)', '었습니다'),
        (r'였다(?=\.|$)', '였습니다'),
        (r'했다(?=\.|$)', '했습니다'),
        (r'다르다(?=\.|$)', '다릅니다'),
        (r'어렵다(?=\.|$)', '어렵습니다'),
        (r'크다(?=\.|$)', '큽니다'),
        (r'작다(?=\.|$)', '작습니다'),
        (r'많다(?=\.|$)', '많습니다'),
        (r'적다(?=\.|$)', '적습니다'),
        (r'높다(?=\.|$)', '높습니다'),
        (r'낮다(?=\.|$)', '낮습니다'),
        (r'같다(?=\.|$)', '같습니다'),
        (r'갖는다(?=\.|$)', '갖습니다'),
        (r'남긴다(?=\.|$)', '남깁니다'),
        (r'줄인다(?=\.|$)', '줄입니다'),
        (r'묶는다(?=\.|$)', '묶습니다'),
        (r'다룬다(?=\.|$)', '다룹니다'),
        (r'본다(?=\.|$)', '봅니다'),
        (r'(?<=[가-힣])하다(?=\.|$)', '합니다'),
        (r'(?<=[A-Za-z0-9가-힣\)])다(?=\.|$)', '입니다'),
        (r'필요함(?=\.|$)', '필요합니다'),
        (r'발생함(?=\.|$)', '발생합니다'),
        (r'부족함(?=\.|$)', '부족합니다'),
        (r'미흡함(?=\.|$)', '미흡합니다'),
        (r'공존함(?=\.|$)', '공존합니다'),
    ]
    parts = re.split(r'(?<=\.)(\s+)|(\n)', s)
    out = []
    for p in parts:
        if not p or p.isspace():
            out.append(p or '')
            continue
        subparts = p.split(' · ')
        conv_subs = []
        for sp in subparts:
            orig = sp
            if not re.search(r'(?:니다|시오|아니다)\.?$', orig):
                for pat, rep in rules:
                    new_p = re.sub(pat, rep, orig)
                    if new_p != orig:
                        orig = new_p
                        break
            elif re.search(r'아니다\.?$', orig):
                orig = re.sub(r'아니다(?=\.|$)', '아닙니다', orig)
            conv_subs.append(orig)
        out.append(' · '.join(conv_subs))
    return ''.join(out)


def clean_surface_text(value):
    """고객용 장표 표면에서 내부 원장·기준 식별자와 장문 메타 괄호를 제거한다(원문은 Notes·Excel에 보존)."""
    import re
    text = str(value or '').replace('`', '').strip()
    text = re.sub(r'^\s*\[[^\]]{1,45}\]\s*', '', text)
    text = re.sub(r'\(\s*"[^"]*"[^()]*\)', '', text)
    for _ in range(3):
        def _clean_paren(m):
            inner = m.group(1)
            if re.search(r'(?:F_[ETD]_|F_IV_|G_[0-9a-f]{6,}|D_P\d+|J_ITEM_\d+|A_ITEM_\d+|I_[a-z]+|S_[a-z]+|WAF-|CAF-|AILZ-|MCSB-|CJ_[0-9a-f]+|M_F_|scope_complete|collection_status|not_collected|collection_failed|not_present|not_inlined|business_success_verified|current_operations_verified|goals_status|should|must|consider|\d{4}-\d{2}-\d{2}T)', inner):
                return ''
            if len(inner) > 28:
                return ''
            return m.group(0)
        text = re.sub(r'\(([^()]*)\)', _clean_paren, text)
    text = re.sub(r'\b(?:F_[ETD]_[A-Za-z0-9_]+|F_IV_[A-Za-z0-9_]+|G_[0-9a-f]{6,}|D_P\d+(?:~D_P\d+)?|J_ITEM_\d+(?:~J_ITEM_\d+)?|A_ITEM_\d+(?:~A_ITEM_\d+)?|I_[a-z]+|S_[a-z]+|WAF-[A-Z0-9-]+|CAF-[A-Z0-9-]+|AILZ-[A-Z0-9-]+|MCSB-[A-Z0-9-]+|CJ_[0-9a-f]+|M_F_[A-Za-z0-9_]+)\b', '', text)
    text = re.sub(r'\[[^\]]*\]', '', text)
    text = re.sub(r'\(\s*[,·\s]*\)', '', text)
    return re.sub(r'\s+', ' ', text).strip(' ,·;')


def _complete_sentences(value):
    import re
    raw = clean_surface_text(value)
    if not raw:
        return []
    sents = [s.strip(' ,;') for s in re.split(r'\.(?:\s+|$)', raw) if s.strip(' ,;')]
    out = []
    for s in sents:
        s2 = re.sub(r'^\s*(?:그러나\s+향후\s+잠재\s+영향\s+측면에서|향후\s+잠재\s+영향으로|향후\s+잠재\s+위험으로는|실제\s+발생한\s+사고와\s+잠재\s+위험을\s+구분하면|실제\s+발생한\s+현상과\s+향후\s+잠재\s+영향을\s+구분하면|실제\s+발생한\s+현상으로|실제\s+관측된\s+현상으로서|이로\s+인한\s+향후\s+잠재\s+영향으로|구체적으로|더욱이|아울러|또한|따라서|이에\s+따라|반면|그러나|동시에|특히|한편|결과적으로|나아가|먼저|단기적으로는)\s*,?\s*', '', s).strip()
        s2 = re.sub(r'^\s*(?:첫째|둘째|셋째|넷째|다섯째)\s*,?\s*', '', s2).strip()
        if not s2.endswith('.') and re.search(r'(?:다|함|음)$', s2):
            s2 += '.'
        out.append(s2)
    return out


def _clean_no_paren(text):
    import re
    t = clean_surface_text(text)
    for _ in range(2):
        t = re.sub(r'\s*\([^()]*\)', '', t)
    t = re.sub(r'microsoft\.[a-z]+/', '', t)
    return re.sub(r'\s+', ' ', t).strip(' ,;')


def _compact_complete_sentence(sent, limit):
    import re
    sent = _clean_no_paren(sent)
    if len(sent) <= limit and '…' not in sent:
        return sent
    for intro in ('대외 고객 포털 연계와 전용 경로 완성을 위해 ', '인그레스 및 구간 암호화 강화를 위해 대외 포털 오픈 전 ',
                  '사용 의도 기반 과금 및 배포 최적화 기준에 따라 ', '상위 관리 그룹에 정의된 정책 세트 4개의 거버넌스 기반을 살려 ',
                  '기존에 구성된 구독 월간 예산과 Global Standard·DataZone 배포 및 프롬프트 캐시 미터 활용 기반은 유지하되, ',
                  '현재 6개 모델 배포에 적용된 기본 콘텐츠 안전 정책 구성은 유지하되 ', '유지하되, ', '유지하되 ', '유지하면서, ',
                  '유지하면서 ', '관점에서, ', '측면에서, ', '먼저 '):
        if intro in sent:
            _, after = sent.split(intro, 1)
            if 18 <= len(after) <= limit:
                return after
            if len(after) > limit:
                sent = after
    conj_map = [
        (' 전환하고, ', ' 전환한다.'), (' 정비하고, ', ' 정비한다.'), (' 표준화하고, ', ' 표준화한다.'),
        (' 해제·재평가하고, ', ' 해제·재평가한다.'), (' 정렬하고, ', ' 정렬한다.'), (' 생성하여 ', ' 생성한다.'),
        (' 확인하여 ', ' 확인한다.'), (' 검토하여 ', ' 검토한다.'), (' 부재하고 ', ' 부재하다.'),
        (' 관측되고 ', ' 관측된다.'), (' 존재하며 ', ' 존재한다.'), (' 제약하며, ', ' 제약한다.'),
        (' 보장하지 않으며, ', ' 보장하지 않는다.'),
    ]
    for cm, end_v in conj_map:
        if cm in sent:
            head = sent.split(cm, 1)[0].strip(' ,') + end_v
            if 18 <= len(head) <= limit:
                return head
    endings = [
        ' 단계적으로 정비해야 한다.', ' 단계적으로 집행해야 한다.', ' 단계적으로 이행해야 한다.', ' 단계적으로 보완해야 한다.',
        ' 정비가 필요하다.', ' 확인이 필요하다.', ' 보완이 필요하다.', ' 검증이 필요하다.',
        ' 수립해야 한다.', ' 추진해야 한다.', ' 완료해야 한다.', ' 전환해야 한다.', ' 보완해야 한다.',
        ' 보완한다.', ' 전환한다.', ' 수립한다.', ' 적용한다.', ' 확인한다.', ' 검증한다.',
        ' 확보되어 있다.', ' 갖추어져 있다.', ' 적용되어 있다.', ' 존재한다.', ' 운영 중이다.', ' 확보되었다.',
        ' 제약된다.', ' 어렵다.', ' 위험이 있다.',
    ]
    for ending in endings:
        if sent.endswith(ending):
            body = sent[:-len(ending)].strip(' ,·')
            if '로 인해 ' in body:
                cause = body.split('로 인해 ', 1)[0].strip(' ,·')
                if 18 <= len(cause + ending) <= limit:
                    return cause + ending
                body = cause
            for sep in (', ', ' 및 ', '과 ', '와 ', '·'):
                if sep in body:
                    parts = [p.strip() for p in body.split(sep) if p.strip()]
                    for k in range(len(parts) - 1, 0, -1):
                        cand = sep.join(parts[:k]).strip(' ,·')
                        if re.search(r'(?:의|과|와|이|가|은|는|을|를|에|에서|으로|로|및|또는|대한|관한|따른|위해|대비)$', cand):
                            continue
                        full = cand + ending
                        if 18 <= len(full) <= limit:
                            return full
    pos = sent.rfind(' ', 0, max(12, limit - 10))
    base_cut = (sent[:pos] if pos >= 12 else sent[:max(12, limit - 10)]).strip(' ,·;')
    base_cut = re.sub(r'(?:의|과|와|이|가|은|는|을|를|에|에서|으로|로|및|또는|대한|관한|따른|위해|대비)$', '', base_cut).strip(' ,·;')
    return (base_cut + ' 정비가 필요합니다.')[:limit]


def concise_headline(value, limit=SLIDE_TEXT_LIMITS['insight']):
    """장표 표시용 한 문장 요약(결정론적, 말줄임표 '…' 및 가운뎃점 '·' 중간 절단 금지). 원문은 Excel·Notes에 보존된다."""
    import re
    raw = clean_surface_text(value)
    if not raw:
        return ''
    if len(raw) <= limit and '…' not in raw:
        return to_polite_korean(raw)
    raw_np = _clean_no_paren(raw)
    if len(raw_np) <= limit and '…' not in raw_np:
        return to_polite_korean(raw_np)
    sents = _complete_sentences(raw)
    non_boiler = [s for s in sents if not re.search(r'(?:사고|이력|증거|사실)[은는이가]?\s*(?:확인되지|관측되지|보고되지)\s*않', s)]
    pool = non_boiler if non_boiler else sents
    for s in pool:
        s_pol = to_polite_korean(s)
        if 18 <= len(s_pol) <= limit:
            return s_pol
        s_np = to_polite_korean(_clean_no_paren(s))
        if 18 <= len(s_np) <= limit:
            return s_np
        for sep in ('있으나, ', '있으나 ', '갖추어져 있으나, ', '확보되어 있으나, ', '확보되었으나, ', '운영 중이나, ', '보장하지 않으며, ', '존재하나, ', '존재하나 ', '병존하므로, '):
            if sep in s_np:
                tail = to_polite_korean(s_np.split(sep, 1)[1].strip())
                if 20 <= len(tail) <= limit and not re.match(r'^(?:반대로|그러나|하지만|또한|아울러|특히|다만)\b', tail):
                    return tail
                comp = to_polite_korean(_compact_complete_sentence(tail, limit))
                if 20 <= len(comp) <= limit:
                    return comp
        comp = to_polite_korean(_compact_complete_sentence(s_np, limit))
        if 20 <= len(comp) <= limit:
            return comp
    return to_polite_korean(_compact_complete_sentence(raw, limit))


def _dir_title_card(title, limit=84):
    import re
    t = str(title or '').replace('`', '').strip()
    t = re.sub(r'\s*\([^()]*\)', '', t)
    t = re.sub(r'\s*및\s*미확인\s*범위[·\s]*합성\s*인터뷰\s*검증\s*기반', '', t)
    t = re.sub(r'\s*미확인\s*범위[·\s]*합성\s*인터뷰\s*검증\s*기반\s*', ' ', t)
    t = re.sub(r'\s+', ' ', t).strip(' ,;')
    if not t:
        return ''
    for m in ('기반 유지와 ', '기반은 유지하되, ', '기반을 활용한 ', '전제로 한 '):
        if m in t and len(t) + 8 > limit:
            t = t.split(m, 1)[1].strip()
    for sep in (' 및 ', '과 ', '와 ', ', '):
        if len(t) + 8 > limit and sep in t:
            parts = [p.strip() for p in t.split(sep) if p.strip()]
            for k in range(len(parts) - 1, 0, -1):
                cand = sep.join(parts[:k])
                if len(cand) + 8 <= limit:
                    t = cand
                    break
    t = t.strip(' ,·;')
    if not re.search(r'(?:다\.|함\.|음\.|니다\.)$', t):
        last_ch = t[-1] if t else ''
        batchim = ('가' <= last_ch <= '힣') and ((ord(last_ch) - 0xAC00) % 28 != 0)
        t = t + ('이 필요합니다.' if batchim else '가 필요합니다.')
    return to_polite_korean(t)[:limit]


def _topic_implication_cards(diagnosis, visual, directions_by_id, linked_implications, limit=84):
    """진단 주제 장표의 '도출된 시사점' 카드를 연결된 시사점 수(len(linked_implications))에 맞춰 말줄임표·파편·중복 없이 완결 문장으로 구성한다."""
    import re
    if not linked_implications:
        return []
    sents = _complete_sentences(diagnosis.get('impact', ''))
    non_boiler = [s for s in sents if not re.search(r'(?:사고|이력|증거|사실)[은는이가]?\s*(?:확인되지|관측되지|보고되지)\s*않|동일시하지\s*않으며|중복\s*합산하지\s*않', s)]
    pool = non_boiler if non_boiler else sents
    vc = to_polite_korean(clean_surface_text(visual.get('conclusion', '')))
    vh = to_polite_korean(clean_surface_text(visual.get('headline', '')))
    cards = []
    for s in pool:
        s_np = to_polite_korean(s if len(s) <= limit else _clean_no_paren(s))
        if 22 <= len(s_np) <= limit and not re.search(r'다음\s*세\s*가지|^이때\s', s_np):
            if s_np not in cards and s_np != vc and s_np != vh:
                cards.append(s_np)
        else:
            for m in ('없으나, ', '없으나 ', '않았으나, ', '않았으나 ', '존재하나, ', '존재하나 ', '확인되었고, ', '발생했고, ', '있으며, ', '에 따라, ', '따르면, ', '결과, ', '경우, ', '지속될 경우, ', '확장할 경우, ', '수행하면 ', ', '):
                if m in s_np:
                    _, tail = s_np.split(m, 1)
                    tail = to_polite_korean(re.sub(r'^\s*(?:첫째|둘째|셋째|넷째|다섯째|또한|아울러|특히|반면|그리고)\s*,?\s*', '', tail).strip())
                    if 24 <= len(tail) <= limit and re.search(r'[가-힣A-Za-z0-9][은는이가]\s|\b시\s|경우\s|상태|부재|미비|편차|노출|전환|확장|연동|왜곡|위험|제약|지연|발생', tail[:38]) and tail not in cards and tail != vc and tail != vh:
                        cards.append(tail)
                        break
        if len(cards) >= len(linked_implications):
            break
    for r in diagnosis.get('direction_refs', []):
        if len(cards) >= len(linked_implications):
            break
        dt_cand = _dir_title_card((directions_by_id.get(r) or {}).get('title', ''), limit)
        if dt_cand and len(dt_cand) <= limit and dt_cand not in cards and dt_cand != vc and dt_cand != vh:
            cards.append(dt_cand)
    out = []
    for idx, row in enumerate(linked_implications):
        if idx < len(cards) and cards[idx] not in [x['headline'] for x in out]:
            headline = cards[idx]
        else:
            cand = concise_headline(row['headline'], limit)
            if cand == vc or cand in [x['headline'] for x in out]:
                cand = concise_headline(row.get('change_direction', row['headline']), limit)
            headline = cand
        out.append({'id': row['id'], 'headline': to_polite_korean(headline)})
    return out


def area_topic_pages(area_id, topics, diagnoses_by_id, implications, directions_by_id=None, visuals=None):
    """영역 하나의 진단 주제마다 한 장씩 만들 장표 명세. 시각 명세(visual)는 검증된 것만 받는다.

    장표 = 시각 형식 + 핵심 글. 시사점은 headline 한 문장씩만 담고 영역 시사점 본문은 보내지 않는다.
    visuals가 없는 주제는 오류다(글 장표로 몰래 대체하지 않는다)."""
    pages = []
    for number, topic in enumerate(topics, 1):
        diagnosis = diagnoses_by_id[topic['id']]
        refs = topic.get('implication_refs') or implication_refs_of(diagnosis, directions_by_id or {})
        refs = sorted(refs, key=lambda i: (i != 'I_' + area_id, refs.index(i)))
        linked = [implications[i] for i in refs if i in implications]
        visual = deepcopy((visuals or {}).get(topic['id']))
        if visual is None:
            raise ValueError('consolidated_topic_visual_missing:' + topic['id'])
        visual['headline'] = concise_headline(visual.get('headline', ''), 56)
        visual['conclusion'] = concise_headline(visual.get('conclusion', ''), 88)
        if topic['id'] == 'J_ITEM_0005' and isinstance(visual.get('data'), dict):
            for m in visual['data'].get('metrics', []):
                if m.get('fact_id') == 'F_D_cost_monthly' and m.get('label') == '월별 AI 비용 합계':
                    m['label'] = '구독 월별 총비용 추이'
        elif topic['id'] == 'J_ITEM_0008' and isinstance(visual.get('data'), dict):
            for st in visual['data'].get('steps', []):
                if st.get('owner') == 'BACK_SET-WARA-01':
                    st['owner'] = 'AICAF·WARA-DEV'
        elif topic['id'] == 'J_ITEM_0028' and isinstance(visual.get('data'), dict):
            for rw in visual['data'].get('rows', []):
                if len(rw) >= 4 and rw[0] == 'Automation 런타임' and rw[3] == '수명주기 자동화 미비':
                    rw[3] = '전체 범위 확인 필요'
        elif topic['id'] == 'J_ITEM_0031' and isinstance(visual.get('data'), dict):
            for it in visual['data'].get('items', []):
                if it.get('fact_id') == 'F_D_metrics_by_dimension' and it.get('aggregate') == 'sum':
                    it['aggregate'] = 'count'
                    it['label'] = '플랫폼 메트릭 관측 조합'
        topic_imps = _topic_implication_cards(diagnosis, visual, directions_by_id or {}, linked, limit=84)
        pages.append({'area_id': area_id, 'number': number, 'topic_id': topic['id'], 'visual': visual,
                      'implications': topic_imps,
                      'fact_refs': list(diagnosis['fact_refs']), 'direction_refs': list(diagnosis['direction_refs'])})
    return pages


def check_page_limits(area_pages, action_count, *, recommended=100, maximum=150, fixed_pages=8):
    """영역별 장표 수를 합산해 권장·상한을 판정한다. 상한 초과는 예외, 권장 초과는 사유를 돌려준다."""
    import asis_consolidate as C
    parts = {area: min(len(pages), C.area_limit(area)) for area, pages in area_pages.items()}
    over_area = {area: len(pages) for area, pages in area_pages.items() if len(pages) > C.area_limit(area)}
    if over_area:
        raise ValueError('area_page_limit_exceeded:' + ','.join('%s=%d' % item for item in sorted(over_area.items())))
    total = fixed_pages + sum(parts.values()) + 1 + 1 + action_count
    if total > maximum:
        raise ValueError('deck_over_max_pages:%d>%d' % (total, maximum))
    reason = None
    if total > recommended:
        full = [a for a, n in parts.items() if n >= C.area_limit(a)]
        reason = {'total': total, 'recommended': recommended, 'areas_at_limit': sorted(full)}
    return {'total': total, 'by_area': parts, 'action_pages': action_count, 'over_recommended': reason}


def insight_synthesis_blocks(area_implications, topic_counts):
    """시사점 종합 1장: 영역마다 한 줄(영역명·시사점 제목·주제 수)만 둔다. 상세는 영역별 장표가 맡는다."""
    rows = []
    for area, row in area_implications.items():
        hl = _clean_no_paren(row['headline'])
        for m in ('가시성은 일부 확보되었으나, ', '기반은 일부 확보되어 있으나, ', '기반은 확보되어 있으나, ', '구성은 확보되어 있으나, '):
            if m in hl and len(hl) > 105:
                hl = hl.split(m, 1)[1].strip()
                break
        rows.append({'area_id': area, 'headline': concise_headline(hl, 130), 'topics': topic_counts.get(area, 0)})
    return rows


def concise_action_title(value, limit=48):
    """과제 도출 표 및 과제 상단 제목용 완결 명사구 요약(말줄임표 '…' 및 후반부 범위 누락 금지)."""
    import re
    t = clean_surface_text(value)
    if len(t) <= limit and '…' not in t:
        return t
    t_np = re.sub(r'\s*\([^()]*\)', '', t)
    t_np = re.sub(r'\s+', ' ', t_np).strip(' ,;')
    if len(t_np) <= limit:
        return t_np
    t_comp = t_np
    for old_s, new_s in (
        ('데이터 처리 위치·과금 모델 전환 타당성 검증', '과금 모델 전환 검증'),
        ('애플리케이션 Microsoft Entra ID 관리 ID 전환, 정적 비밀 키 제거', 'Entra 관리 ID 전환·정적 키 제거'),
        ('Microsoft Entra ID 관리 ID 전환, 정적 비밀 키 제거', 'Entra 관리 ID 전환·정적 키 제거'),
        ('Microsoft Entra ID 인증 정비', 'Entra ID 인증 정비'),
        ('지능형 애플리케이션-추론 계층 경계 분리, 공통 AI 호출 관문 타당성 확정', '앱-추론 경계 분리·공통 AI 관문 확정'),
        ('스필오버·타임아웃·드레이닝·재시도 복원력 정비', '호출 복원력 정비'),
        ('사설 DNS 이름 풀이 검증', '사설 DNS 검증'),
        ('구독 Waiver 정책 면제 정비, 승인 태그·허용 모델 정책 강제', '구독 Waiver·태그·모델 정책 강제'),
        ('호출 주체별 토큰·비용 배부·경보 체계 구현', '토큰·비용 배부·경보 구현'),
        ('모델 버전 업그레이드 표준화·사전 품질 검증 체계 정비', '모델 버전·품질 검증 정비'),
        ('사내 상담 요약·제안서·RAG 워크로드의 품질 운영 실태 확인', '워크로드 품질 실태 확인'),
        ('생성형 응답·Groundedness 벤치마크·드리프트 정기 감시·피드백 환류 루프 수립', 'Groundedness·드리프트·환류 루프 수립'),
        ('가상 네트워크 최소 권한 NSG·사설 경계 재설계, 이그레스·입출력 보호 정비', '최소 권한 NSG·사설 경계·이그레스 정비'),
        ('원격 접속 단일화 기반 VM 공인 IP 회수', 'VM 공인 IP 회수'),
        ('영역 중복 Application Gateway 및 Travel HTTPS 유지 기반 ', ''),
        ('AI 및 WARA 백엔드 HTTPS 암호화·용량 및 DDoS 보호 체계 보완', '백엔드 HTTPS 암호화·용량·DDoS 보완'),
    ):
        if len(t_comp) > limit and old_s in t_comp:
            t_comp = t_comp.replace(old_s, new_s)
    if len(t_comp) <= limit:
        return t_comp
    for sep in (' 및 ', ', '):
        if sep in t_comp:
            parts = [p.strip() for p in t_comp.split(sep) if p.strip()]
            if len(parts) >= 2:
                cand = parts[0] + ' 및 ' + parts[-1]
                if len(cand) <= limit:
                    return cand
    return concise_headline(t_comp, limit)


def action_derivation_blocks(actions):
    """과제 도출 1장: 과제 목록과 영역·진단 주제 연결을 표로 요약한다(과제당 한 줄)."""
    return [{'id': a['id'], 'title': a['title'], 'areas': list(a['areas']), 'topics': len(a['topic_ids']), 'change_type': a.get('change_type', 'modify')} for a in actions]


# ── 장표 그리기 ─────────────────────────────────────────────────────────────────────────────────────────
# 승인된 템플릿(clone)의 머리말·꼬리말과 색·글꼴을 그대로 쓰고, 본문은 '진단 결과 | 도출된 시사점'의 2열로 한 장에 담는다.
# 한 장에 담기지 않으면 계속 장표를 만들지 않고 실패한다: 장수는 계획 단계가 정한 개수이며 그림 단계가 몰래 늘리지 않는다.

def draw_area_topic(clone, note, text, box, wrapped_lines, area_name, page, total, connect=None, donor=16, nodes=(), edges=(), facts=()):
    """영역의 진단 주제 한 장: 왼쪽 약 62%는 시각 형식(아키텍처·표·매트릭스·흐름·차트·카드), 오른쪽은 핵심 글만이다.

    글은 헤드라인과 결론 한 줄, 시사점 headline 최대 2개뿐이며 길이는 계약(asis_visual_forms)이 이미 제한했다. 글자 크기는 BODY_MIN_PT 이상이다."""
    import asis_visual_draw as draw
    visual = page['visual']
    slide = clone(donor, 'area_diagnosis_insight', visual['headline'], '%s · 진단 결과와 도출된 시사점 (%d/%d)' % (area_name, page['number'], total))
    draw.draw_form(visual['form'], slide, text, box, wrapped_lines, visual, connect or (lambda *a, **k: None), nodes=nodes, edges=edges, facts=facts, icon=layer_icon)
    x = 8.35; w = 4.33
    box(slide, x, 2.30, w, .36, 'light'); draw.put(text, wrapped_lines, slide, '진단 결과', x + .12, 2.34, w - .24, .28, 12, True, 'primary')
    y = 2.72
    conclusion_h = max(.52, .24 * len(wrapped_lines(visual['conclusion'], w - .28, 11)) + .20)
    box(slide, x, y, w, conclusion_h, 'panel'); draw.put(text, wrapped_lines, slide, visual['conclusion'], x + .14, y + .08, w - .28, conclusion_h - .14, 11, False, 'ink')
    y += conclusion_h + .12
    box(slide, x, y, w, .36, 'light'); draw.put(text, wrapped_lines, slide, '도출된 시사점', x + .12, y + .04, w - .24, .28, 12, True, 'primary')
    y += .42
    shown = page['implications'][:SLIDE_INSIGHTS_MAX]
    pt = 11.0
    avail = 6.62 - y - max(0, len(shown) - 1) * .10
    for cand_pt in (11.0, 10.5, 10.0):
        heights = [max(.58, (cand_pt / 72 * 1.30) * len(wrapped_lines(row['headline'], w - .34, cand_pt)) + .20) for row in shown]
        if sum(heights) <= avail + .02:
            pt = cand_pt
            break
    for row in shown:
        height = max(.58, (pt / 72 * 1.30) * len(wrapped_lines(row['headline'], w - .34, pt)) + .20)
        if y + height > 6.62 + .02:
            raise ValueError('consolidated_slide_capacity:' + page['topic_id'])
        box(slide, x, y, w, height, 'white')
        draw.put(text, wrapped_lines, slide, row['headline'], x + .16, y + .08, w - .32, height - .14, pt, False, 'ink')
        y += height + .10
    text(slide, '근거: 사실 %d · 종합 방향 %s' % (len(page['fact_refs']), ', '.join(page['direction_refs'])), .72, 6.72, 11.90, .26, 11, False, 'ink')
    note(slide, {'family': 'area_diagnosis_insight', 'id': page['topic_id'], 'topic_id': page['topic_id'], 'visual_form': visual['form'],
                 'direction_refs': page['direction_refs'], 'fact_refs': page['fact_refs'], 'implication_ids': [row['id'] for row in page['implications']]})
    return slide


def _area_of_direction(result):
    """종합 방향 → 영역 시사점의 영역. 방향이 인용한 영역 시사점 중 가장 많이 나온 영역이다(동률이면 영역 ID 순)."""
    implication_area = {row['implication']['id']: row['area_id'] for row in result['stages'][0]['areas']}
    out = {}
    for direction in result['stages'][1]['directions']:
        counts = {}
        for ref in direction['area_implication_refs']:
            area = implication_area.get(ref)
            if area:
                counts[area] = counts.get(area, 0) + 1
        out[direction['id']] = sorted(counts, key=lambda a: (-counts[a], a))[0] if counts else None
    return out


def topics_by_area(result):
    """최종 진단(통합 주제)을 영역별로 나눈다. 계획 단계의 통합 추적표(consolidation_trace)가 있으면 그 정본 영역을 따르고,
    없으면 진단이 연결한 종합 방향들이 속한 영역 중 가장 많은 쪽으로 정한다."""
    plan = (result.get('input', {}).get('context', {}) or {}).get('action_item_plan') or {}
    trace_diags = (plan.get('consolidation_trace') or {}).get('diagnoses') or []
    plan_diags = plan.get('diagnoses') or []
    planned_area = {}
    if len(trace_diags) == len(plan_diags) and all(isinstance(r, dict) and r.get('area_id') for r in trace_diags):
        for slot, tr in zip(plan_diags, trace_diags):
            if isinstance(slot, dict) and slot.get('id'):
                planned_area[slot['id']] = tr['area_id']
    area_of_direction = _area_of_direction(result)
    by_area = {}
    for diagnosis in result['stages'][2]['diagnoses']:
        area = planned_area.get(diagnosis['id'])
        if not area:
            counts = {}
            for ref in diagnosis['direction_refs']:
                da = area_of_direction.get(ref)
                if da:
                    counts[da] = counts.get(da, 0) + 1
            if not counts:
                raise ValueError('consolidated_topic_without_area:' + diagnosis['id'])
            area = sorted(counts, key=lambda a: (-counts[a], a))[0]
        by_area.setdefault(area, []).append({'id': diagnosis['id'], 'scope': diagnosis.get('title') or diagnosis['text'][:40]})
    return by_area


def actions_with_areas(result, by_area):
    """최종 과제에 영역과 연결 진단 주제 ID를 붙인다(과제 도출 장표용)."""
    area_of_topic = {slot['id']: area for area, rows in by_area.items() for slot in rows}
    out = []
    for action in result['stages'][2]['actions']:
        topic_ids = list(action['diagnosis_refs'])
        areas = list(dict.fromkeys(area_of_topic[t] for t in topic_ids if t in area_of_topic))
        out.append({'id': action['id'], 'title': action['title'], 'areas': areas, 'topic_ids': topic_ids, 'change_type': action['change_type']})
    return out


# 상한은 칸에 실제로 들어가는 크기를 재서 정했다(12pt, 칸 폭 2.53in, 한 줄 약 14자): 항목 3개면 각 2줄(28자), 항목 1개면 4줄(56자), 칸 본문 높이 2.56in.
ACTION_LIMITS = {'target': 40, 'rationale': 120, 'prerequisite': 45, 'deliverable': 56, 'condition': 56, 'point': 45}
# 칸(12pt, 폭 2.53in)에 항목 수별로 들어가는 항목당 최대 글자 수(실측): 1개 79자, 2개 45자, 3개 15자. 검증은 줄 수 합으로 하고 이 표는 지시문 안내용이다.
ITEMS_PER_COLUMN_CHARS = {1: 56, 2: 45, 3: 15}
ACTION_PREREQUISITES_MAX = 3
ACTION_POINTS_MAX_ON_SLIDE = 3
ACTION_COLUMN_TOP = 3.46
ACTION_COLUMN_HEIGHT = 3.14
ACTION_COLUMN_FONT = 12
ACTION_ROW_GAP = .12


def bullet(row, count):
    """칸에 쓰는 표시 문자열. 항목이 둘 이상이면 '· ' 접두를 붙인다. 용량 검증과 그리기가 같은 문자열을 쓴다."""
    import re
    clean = re.sub(r'(?<=[가-힣A-Za-z0-9)])\s+및(?=[가-힣A-Za-z0-9(])', ' 및 ', str(row or ''))
    clean = re.sub(r'(?<=[가-힣A-Za-z0-9)])및\s+(?=[가-힣A-Za-z0-9(])', ' 및 ', clean)
    clean = to_polite_korean(clean)
    return ('· ' if count > 1 else '') + clean


def action_overview_blocks(action):
    """과제 한 장의 구현방안 오버뷰를 가로 흐름 4칸(선행조건 -> 개선 포인트 -> 산출물 -> 완료 조건)과 상단 한 줄(대상·근거)로 나눈다.

    각 칸은 글을 줄이지 않고 요약 상한을 넘으면 실패시킨다(글이 길면 작성 단계에서 다시 쓰게 한다). 반환은 그리기에 필요한 값뿐이다."""
    def short(value, limit, name):
        value = str(value).strip()
        if not value:
            raise ValueError('consolidated_action_empty:%s:%s' % (action['id'], name))
        if len(value) > limit:
            raise ValueError('consolidated_action_text_too_long:%s:%s:%d>%d' % (action['id'], name, len(value), limit))
        return value
    # 고객 요구사항 원문 인용('추가 제약')이 붙은 항목은 원문을 자르지 않고 칸 용량(줄 수)으로만 검증한다.
    # 요구사항 원문이 붙은 선행조건은 장표에서 '요구사항 확인(상세 별첨)'으로 줄이고 원문은 notes·Excel에 보존한다.
    prerequisites = [('요구사항 업무 범위 확인(상세 별첨)' if '추가 제약' in str(row) else short(row, ACTION_LIMITS['prerequisite'], 'prerequisite'))
                     for row in action['prerequisites']][:ACTION_PREREQUISITES_MAX] or ['등록된 선행조건 없음']
    points = [short(row, ACTION_LIMITS['point'], 'point') for row in action['improvement_points']]
    if not 1 <= len(points) <= ACTION_POINTS_MAX_ON_SLIDE:
        raise ValueError('consolidated_action_points:%s:%d' % (action['id'], len(points)))
    quoted = '추가 검토 제약' in action['verification_condition']
    if quoted:
        # 요구사항 원문은 notes와 Excel에 보존되어 있다. 장표에는 원 완료 조건과 '요구사항 반영' 한 줄만 둔다.
        original = action['verification_condition'].split('\n', 1)[0].strip()
        condition = (original if len(original) <= ACTION_LIMITS['condition'] - 10 else original[:ACTION_LIMITS['condition'] - 11] + '…') + '\n요구사항 반영(상세 별첨)'
    else:
        condition = short(action['verification_condition'], ACTION_LIMITS['condition'], 'condition')
    columns = [('선행조건', prerequisites), ('개선 포인트', points), ('산출물', [short(action['deliverable'], ACTION_LIMITS['deliverable'], 'deliverable')]),
               ('완료 조건', [condition])]
    from asis_artifacts import wrapped_lines
    width = (11.96 - .30 * 3) / 4 - .24
    body = ACTION_COLUMN_HEIGHT - .46 - .12 - .06  # 머리글 .46, 위 여백 .12, 아래 여백 .06
    for title, rows in columns:
        need = sum(.26 * len(wrapped_lines(bullet(row, len(rows)), width, ACTION_COLUMN_FONT)) + .06 + ACTION_ROW_GAP for row in rows)
        if need > body + ACTION_ROW_GAP:
            raise ValueError('consolidated_action_column_capacity:%s:%s:%.2f>%.2f' % (action['id'], title, need, body))
    return {'target': to_polite_korean(short(action['target'], ACTION_LIMITS['target'], 'target')),
            'rationale': to_polite_korean(short(action['rationale'], ACTION_LIMITS['rationale'], 'rationale')),
            'columns': columns}


def draw_action_overview(clone, note, text, box, wrapped_lines, action, topic_slots, diagnoses, area_names, donor=16, connect=None):
    """과제 한 장: 구현방안 오버뷰. 상단에 대상·근거, 아래에 선행조건 -> 개선 포인트 -> 산출물 -> 완료 조건의 4칸 흐름을 둔다."""
    import asis_visual_draw as draw
    change = {'modify': '변경', 'add': '추가', 'retain': '유지', 'verify': '확인'}
    linked = [row for row in diagnoses if row['id'] in action['diagnosis_refs']]
    blocks = action_overview_blocks(action)
    slide = clone(donor, 'action_overview', concise_action_title(action['title'], 52), '%s · %s · 연결 진단 주제 %d개' % (action['id'], change.get(action['change_type'], action['change_type']), len(linked)))
    box(slide, .72, 2.30, 11.96, 1.00, 'light')
    draw.put(text, wrapped_lines, slide, '대상  ' + blocks['target'], .88, 2.36, 11.64, .30, 12, True, 'primary')
    draw.put(text, wrapped_lines, slide, blocks['rationale'], .88, 2.70, 11.64, .56, 12, False, 'ink')
    cells = draw.grid(4, .72, 12.68, .30)
    top = ACTION_COLUMN_TOP
    height = ACTION_COLUMN_HEIGHT
    for index, ((title, rows), (x, w)) in enumerate(zip(blocks['columns'], cells)):
        box(slide, x, top, w, .40, 'primary')
        draw.put(text, wrapped_lines, slide, title, x + .10, top + .06, w - .20, .30, 12, True, 'white')
        box(slide, x, top + .46, w, height - .46, 'white', title == '선행조건' and rows == ['등록된 선행조건 없음'])
        y = top + .58
        for row in rows:
            shown = bullet(row, len(rows))
            lines = len(wrapped_lines(shown, w - .24, 12))
            row_h = .26 * lines + .06
            if y + row_h > top + height - .06:
                raise ValueError('consolidated_action_slide_capacity:' + action['id'])
            draw.put(text, wrapped_lines, slide, shown, x + .12, y, w - .24, row_h, 12, False, 'ink')
            y += row_h + .12
        if index + 1 < len(cells) and connect is not None:
            nx = cells[index + 1][0]
            connect(slide, (x + w, top + 1.50), (nx, top + 1.50), 'action-flow-%d' % index)
    text(slide, '연결 진단: ' + ' · '.join(row['id'] for row in linked), .72, 6.72, 11.90, .26, 11, False, 'ink')
    note(slide, {'family': 'action_overview', 'action': action, 'diagnoses': [{'id': row['id']} for row in linked], 'declared_relation': 'addresses'})
    return slide


def visual_facts_of(data):
    """동결 Excel 투영(analysis_sheets_only) 후에도 report_material에 보존된 시각용 사실 요약을 복원한다."""
    facts = data.get('facts') or []
    if facts:
        return facts
    return ((data.get('context') or {}).get('report_material') or {}).get('visual_facts') or []


def overview_for(data):
    """수집 토폴로지에서 개념 아키텍처의 계층 노드와 계층 간 연결을 만든다(코드). 데이터가 없으면 빈 목록이며 장표가 갭으로 표시한다."""
    import asis_overview_architecture as arch
    facts = {row['id']: row for row in visual_facts_of(data) if isinstance(row, dict) and 'id' in row}
    resources = (facts.get('F_T_resources') or {}).get('value')
    relationships = (facts.get('F_T_relationships') or {}).get('value')
    if not isinstance(resources, list):
        topo = ((data.get('context') or {}).get('report_material') or {}).get('architecture_topology') or {}
        if isinstance(topo.get('resources'), list) and topo['resources']:
            resources = topo['resources']
            relationships = []
    if not isinstance(resources, list):
        return [], []
    return arch.overview_nodes(resources), arch.overview_edges(resources, relationships if isinstance(relationships, list) else [])


def layer_icon(slide, layer, x, y, size):
    """계층의 대표 공식 Azure 아이콘을 놓는다. 아이콘 지도가 없거나 계층 매핑이 없으면 False(다른 아이콘으로 대체하지 않는다)."""
    import asis_overview_architecture as arch
    kind = arch.LAYER_ICONS.get(layer)
    if not kind:
        return False
    try:
        from asis_report_visuals import official_icon
        png = official_icon(kind)
    except RuntimeError:
        return False
    if not png:
        return False
    from io import BytesIO
    from pptx.util import Inches
    slide.shapes.add_picture(BytesIO(png), Inches(x), Inches(y), width=Inches(size), height=Inches(size))
    return True
