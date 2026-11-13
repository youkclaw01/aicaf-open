"""장표 시각 형식 그리기: 개념 아키텍처·비교 표·상태 매트릭스·흐름도·수치 차트·스코어 카드.

배치(좌표·크기)는 순수 함수가 계산하고, 그리기는 주입받은 text/box 헬퍼로 한다. 본문·도식 라벨은 BODY_MIN_PT(10pt) 미만으로 줄이지 않으며,
라벨이 박스에 들어가지 않으면 글자를 줄이지 않고 예외를 낸다(계약이 글자 수를 이미 제한한다).
색은 템플릿 팔레트(primary 326B91, light EDF3F8, panel F5F7F6, muted 596660, white)만 쓴다. 갭 표시는 주황 파선 대신 템플릿의 dashed 박스를 쓴다.
"""
from asis_consolidated_deck import BODY_MIN_PT
import asis_overview_architecture as A

# 장표 본문 영역(템플릿 머리말·꼬리말을 피한다)
X0, X1 = .72, 12.68
Y0, Y1 = 2.30, 6.55
WIDTH = X1 - X0
STATE_FILL = {'충족': 'light', '부분': 'panel', '미확인': 'white', '해당없음': 'white'}
STATE_MARK = {'충족': '충족', '부분': '부분', '미확인': '확인 필요', '해당없음': '해당 없음'}


def grid(count, x0, x1, gap=.12):
    """가로 count칸의 (x, width) 목록."""
    width = (x1 - x0 - gap * (count - 1)) / count
    return [(x0 + i * (width + gap), width) for i in range(count)]


def fit_or_fail(wrapped_lines, value, width, height, point=BODY_MIN_PT):
    """글자를 줄이지 않는다. 박스에 들어가지 않으면 실패시킨다(줄 수 x 줄 높이 > 높이)."""
    lines = wrapped_lines(value, width, point)
    if len(lines) * point / 72 * 1.25 > height + .03:
        raise ValueError('visual_label_capacity:' + str(value)[:30])
    return lines


def put(text, wrapped_lines, slide, value, x, y, w, h, point=BODY_MIN_PT, bold=False, color='ink', align=None):
    chosen = point
    lines = wrapped_lines(value, w, chosen)
    words = [w0 for p0 in str(value).split('\n') for w0 in p0.split(' ') if w0]
    if len(lines) * chosen / 72 * 1.25 > h + .03 or len(lines) > max(1, len(words)):
        for cand in (24, 20, 18, 16, 14, 12, 11, 10.5, BODY_MIN_PT):
            if cand >= BODY_MIN_PT and cand <= point:
                ls = wrapped_lines(value, w, cand)
                if len(ls) <= max(1, int((h + .01) / (cand / 72 * 1.18))) and (len(ls) <= max(1, len(words)) or cand == BODY_MIN_PT):
                    chosen, lines = cand, ls
                    break
    lines = fit_or_fail(wrapped_lines, value, w, h, chosen)
    kwargs = {} if align is None else {'align': align}
    return text(slide, '\n'.join(lines), x, y, w, h, chosen, bold, color, **kwargs)


# ── 개념 아키텍처(네이티브) ──────────────────────────────────────────────────────────────────────────────────

def architecture_layout(nodes):
    """계층 노드를 격자로 배치한다. 한 줄에 최대 4블록, 행은 최대 2개가 되게 해 블록 높이(약 1.8in)를 확보한다. 노드가 없으면 빈 배치다.

    행 사이에는 연결선이 지나갈 통로를 둔다. 블록은 본문 폭 7.4in 안에서 균등하다."""
    if not nodes:
        return {}
    columns = max(1, min(4, -(-len(nodes) // 2))) if len(nodes) > 3 else len(nodes)
    rows = -(-len(nodes) // columns)
    lane = .45
    cell_h = min(1.90, (Y1 - Y0 - lane * (rows - 1)) / rows)
    cells = grid(columns, X0, X0 + 7.4, .20)
    positions = {}
    for index, node in enumerate(nodes):
        row, col = divmod(index, columns)
        x, w = cells[col]
        positions[node['layer']] = (x, Y0 + row * (cell_h + lane), w, cell_h)
    return positions


def route(layout, a, b):
    """두 블록을 잇는 직각 경로(점 목록). 어떤 경우에도 다른 블록 위를 지나지 않는다.

    같은 줄에서 바로 옆이면 사이 틈으로 가로 이동하고, 사이에 다른 블록이 있으면 줄 아래 통로로 우회한다.
    다른 줄이면 두 블록 사이 통로에서 꺾는다."""
    ax, ay, aw, ah = layout[a]; bx, by, bw, bh = layout[b]
    if abs(ay - by) < .01:
        left, right = (a, b) if ax < bx else (b, a)
        lx, ly, lw, lh = layout[left]; rx, ry, rw, rh = layout[right]
        between = [k for k, (x, y, w, h) in layout.items() if k not in (a, b) and abs(y - ay) < .01 and lx + lw - .001 <= x and x + w <= rx + .001]
        if not between:
            y = ly + lh / 2
            return [(lx + lw, y), (rx, y)]
        lane = ly + lh + .25
        return [(lx + lw / 2, ly + lh), (lx + lw / 2, lane), (rx + rw / 2, lane), (rx + rw / 2, ry + rh)]
    upper, lower = (a, b) if ay < by else (b, a)
    ux, uy, uw, uh = layout[upper]; wx, wy, ww, wh = layout[lower]
    mid = uy + uh + (wy - uy - uh) / 2
    return [(ux + uw / 2, uy + uh), (ux + uw / 2, mid), (wx + ww / 2, mid), (wx + ww / 2, wy)]


def draw_architecture(slide, text, box, wrapped_lines, data, nodes, edges, connect, icon=None):
    """왼쪽 약 62%: 계층 블록(공식 아이콘·개수), 강조 계층은 연한 파랑 면, 갭 계층은 파선과 하단의 '확인 필요'. 연결선은 선언된 관계가 있는 계층 사이를 직각으로 잇는다."""
    layout = architecture_layout(nodes)
    if not layout:
        box(slide, X0, Y0, 7.4, 1.6, 'white', True)
        put(text, wrapped_lines, slide, '수집된 구성 정보 없음', X0 + .16, Y0 + .20, 7.1, .34, 12, True, 'primary')
        put(text, wrapped_lines, slide, '확인 필요: 이 주제의 구성 근거는 수집되지 않았다', X0 + .16, Y0 + .70, 7.1, .34)
        return layout
    for edge in edges:
        if edge['from'] in layout and edge['to'] in layout:
            points = route(layout, edge['from'], edge['to'])
            for start, end in zip(points, points[1:]):
                connect(slide, start, end, 'arch-%s-%s' % (edge['from'], edge['to']))
    for node in nodes:
        x, y, w, h = layout[node['layer']]
        focus = node['layer'] in data['focus_layers']
        gap = node['layer'] in data['gap_layers']
        box(slide, x, y, w, h, 'light' if focus else 'white', gap)
        left = x + .12
        if icon is not None and icon(slide, node['layer'], x + .12, y + .12, .42):
            left = x + .62
        label_w = w - (left - x) - .12
        cursor = y + .12
        # 위에서 아래로 쌓는다: 줄 수에 따라 높이를 정하고, 남는 높이가 모자라면 글자를 줄이지 않고 실패시킨다.
        label_lines = len(wrapped_lines(node['label'], label_w, 12))
        label_h = max(.34, .22 * label_lines + .08)
        put(text, wrapped_lines, slide, node['label'], left, cursor, label_w, label_h, 12, True, 'primary' if focus else 'ink')
        cursor = max(cursor + label_h, y + .12 + .46) + .04
        put(text, wrapped_lines, slide, '%d개' % node['count'], x + .12, cursor, w - .24, .30, 14, True, 'ink')
        cursor += .34
        detail = ''
        for count in (2, 1):
            candidate = ', '.join(node['types'][:count])
            if candidate and len(wrapped_lines(candidate, w - .24, 11)) <= 2:
                detail = candidate; break
        reserve = .32 if gap else 0
        if detail:
            lines = len(wrapped_lines(detail, w - .24, 11))
            detail_h = .20 * lines + .06
            if cursor + detail_h > y + h - reserve - .04:
                raise ValueError('visual_label_capacity:' + node['label'])
            put(text, wrapped_lines, slide, detail, x + .12, cursor, w - .24, detail_h, 11, False, 'ink')
        if gap:
            put(text, wrapped_lines, slide, '확인 필요', x + .12, y + h - .32, w - .24, .26, BODY_MIN_PT, True, 'primary')
    return layout


# ── 비교 표 / 상태 매트릭스 ─────────────────────────────────────────────────────────────────────────────────

def table_layout(columns, rows, row_data=()):
    """표 칸 좌표. 첫 열이 넓고 나머지는 균등하다(단, 마지막 열에 긴 정책·식별자가 있으면 폭을 보강). 반환: (헤더 [(x,w)], 행 높이)."""
    first = 2.0 if columns <= 4 else 1.7
    rest = grid(columns - 1, X0 + first + .08, X0 + 7.4, .06)
    xs = [(X0, first)] + rest
    if columns == 4 and any(len(str(r[3])) >= 17 and ' ' not in str(r[3]) for r in row_data if len(r) >= 4):
        xs = [(X0, 1.72), (X0 + 1.78, 1.72), (X0 + 3.56, 1.52), (X0 + 5.14, 2.26)]
    return xs, min(.86, (Y1 - Y0 - .44) / max(1, rows))


def draw_table(slide, text, box, wrapped_lines, data):
    xs, row_h = table_layout(len(data['columns']), len(data['rows']), data.get('rows', ()))
    for (x, w), title in zip(xs, data['columns']):
        if len(wrapped_lines(title, w - .16, 12)) > 2:
            raise ValueError('visual_label_capacity:' + str(title)[:30])
        box(slide, x, Y0, w, .40, 'primary')
        put(text, wrapped_lines, slide, title, x + .08, Y0 + .03, w - .16, .34, 12, True, 'white')
    for r, row in enumerate(data['rows']):
        y = Y0 + .44 + r * row_h
        for c, ((x, w), cell) in enumerate(zip(xs, row)):
            box(slide, x, y, w, row_h - .04, 'panel' if c == 0 else 'white')
            put(text, wrapped_lines, slide, cell, x + .08, y + .04, w - .16, row_h - .12, 12, c == 0, 'primary' if c == 0 else 'ink')


def draw_matrix(slide, text, box, wrapped_lines, data):
    xs, row_h = table_layout(len(data['columns']) + 1, len(data['rows']))
    box(slide, xs[0][0], Y0, xs[0][1], .40, 'primary')
    put(text, wrapped_lines, slide, '항목', xs[0][0] + .08, Y0 + .03, xs[0][1] - .16, .34, 12, True, 'white')
    for (x, w), title in zip(xs[1:], data['columns']):
        if len(wrapped_lines(title, w - .16, 12)) > 2:
            raise ValueError('visual_label_capacity:' + str(title)[:30])
        box(slide, x, Y0, w, .40, 'primary')
        put(text, wrapped_lines, slide, title, x + .08, Y0 + .03, w - .16, .34, 12, True, 'white')
    for r, row in enumerate(data['rows']):
        y = Y0 + .44 + r * row_h
        box(slide, xs[0][0], y, xs[0][1], row_h - .04, 'panel')
        put(text, wrapped_lines, slide, row['label'], xs[0][0] + .08, y + .04, xs[0][1] - .16, row_h - .12, 12, True, 'primary')
        for (x, w), state in zip(xs[1:], row['states']):
            box(slide, x, y, w, row_h - .04, STATE_FILL[state], state in ('미확인',))
            put(text, wrapped_lines, slide, STATE_MARK[state], x + .08, y + .04, w - .16, row_h - .12, 12, state == '충족', 'primary' if state != '해당없음' else 'muted')


# ── 흐름도 ─────────────────────────────────────────────────────────────────────────────────────────────────

def flow_layout(count):
    """단계를 한 줄에 배치한다(3~6단계). 반환: [(x, y, w, h)]."""
    cells = grid(count, X0, X0 + 7.4, .30)
    return [(x, Y0 + 1.00, w, 1.80) for x, w in cells]


def draw_flow(slide, text, box, wrapped_lines, data, connect):
    cells = flow_layout(len(data['steps']))
    for index, (step, (x, y, w, h)) in enumerate(zip(data['steps'], cells)):
        box(slide, x, y, w, h, 'white' if step['broken'] else 'light', step['broken'])
        put(text, wrapped_lines, slide, step['owner'], x + .08, y + .08, w - .16, .46, 11, False, 'ink')
        put(text, wrapped_lines, slide, step['label'], x + .08, y + .58, w - .16, 1.06, 14, True, 'primary')
        if step['broken']:
            put(text, wrapped_lines, slide, '끊김·확인 필요', x + .08, y + h + .06, w - .16, .36, 11, True, 'primary')
        if index + 1 < len(cells):
            nx, ny, nw, nh = cells[index + 1]
            connect(slide, (x + w, y + h / 2), (nx, ny + nh / 2), 'flow-%d' % index)


# ── 수치 차트 / 스코어 카드(값은 코드가 인용한 사실에서 계산한다) ──────────────────────────────────────────────

def aggregate(fact, how):
    """사실 값에서 수치를 코드가 만든다. 값이 없거나 형식이 맞지 않으면 None(미수집)이다. 모델은 수치를 쓰지 않는다."""
    if not isinstance(fact, dict):
        return None
    if 'collection_status' in fact and fact.get('collection_status') not in ('collected', 'not_present'):
        return None
    value = fact.get('value')
    if how == 'count':
        return len(value) if isinstance(value, list) else None
    if how in ('sum', 'sum_by_day'):
        rows = value if isinstance(value, list) else []
        if fact.get('id') == 'F_D_cost_monthly':
            if any(isinstance(r, dict) and r.get('basis') == 'ActualCost' for r in rows):
                rows = [r for r in rows if isinstance(r, dict) and r.get('basis') == 'ActualCost']
            completed_months = [r for r in rows if isinstance(r, dict) and 'MTD' not in str(r.get('period') or '')]
            if completed_months:
                rows = completed_months
        if any(isinstance(r, dict) and r.get('metric') == 'TokenTransaction' for r in rows):
            rows = [r for r in rows if isinstance(r, dict) and r.get('metric') == 'TokenTransaction']
        elif len({r.get('metric') for r in rows if isinstance(r, dict) and r.get('metric')}) > 1:
            first_metric = next(r.get('metric') for r in rows if isinstance(r, dict) and r.get('metric'))
            rows = [r for r in rows if isinstance(r, dict) and r.get('metric') == first_metric]
        numbers = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            for key in ('Cost', 'cost', 'amount', 'total', 'count', 'value', 'TotalTokens', 'PromptTokens', 'CompletionTokens', 'Requests'):
                v = row.get(key)
                if isinstance(v, (int, float)) and not isinstance(v, bool):
                    numbers.append(float(v))
                    break
        return round(sum(numbers), 2) if numbers else None
    return None


def _metric_note(fact, how, val):
    if not isinstance(fact, dict) or val is None:
        return ''
    scope = '전체 수집' if fact.get('scope_complete') is True else '부분 수집' if fact.get('scope_complete') is False else ''
    rows = fact.get('value') if isinstance(fact.get('value'), list) else []
    if fact.get('id') == 'F_D_cost_monthly' and how in ('sum', 'sum_by_day'):
        ac = [r for r in rows if isinstance(r, dict) and r.get('basis') == 'ActualCost'] or rows
        mtd = [r for r in ac if isinstance(r, dict) and 'MTD' in str(r.get('period') or '')]
        done = [r for r in ac if isinstance(r, dict) and 'MTD' not in str(r.get('period') or '')]
        if done and mtd:
            return 'USD · 완료 %d개월' % len(done)
    if any(isinstance(r, dict) and r.get('metric') == 'TokenTransaction' for r in rows) and how in ('sum', 'sum_by_day'):
        return '토큰 트랜잭션'
    has_usd = any(isinstance(r, dict) and (r.get('Currency') or r.get('currency') or 'Cost' in r or 'amount' in r) for r in rows)
    unit = ' USD' if has_usd and how in ('sum', 'sum_by_day') else ('건' if how == 'count' else '')
    if val == 0 and fact.get('collection_status') == 'not_present':
        unit += ' (부재)'
    elif val == 0 and fact.get('scope_complete') is False:
        unit += ' (미관측)'
    days = {str(r.get(dk))[:10] for r in rows if isinstance(r, dict) for dk in ('UsageDate', 'date', 'day', 'time', 'period', 'observed_from') if r.get(dk)}
    if how == 'sum_by_day' and days:
        return '%s · %d개 기간%s' % (unit.strip(), len(days), (' · ' + scope) if scope else '') if unit.strip() else '%d개 기간%s' % (len(days), (' · ' + scope) if scope else '')
    if unit and scope:
        return '%s · %s' % (unit.strip(), scope)
    return unit.strip() or scope


def metric_values(items, facts):
    """차트·스코어 카드 항목의 값을 인용 사실에서 계산한다. 값이 없으면 None으로 남기고 장표에 '미수집'으로 표시한다."""
    by_id = {fact['id']: fact for fact in facts}
    out = []
    for item in items:
        fact = by_id.get(item['fact_id'], {})
        val = aggregate(fact, item['aggregate'])
        out.append({'label': item['label'], 'value': val, 'note': _metric_note(fact, item['aggregate'], val), 'fact_id': item['fact_id']})
    return out


def draw_chart(slide, text, box, wrapped_lines, values):
    """가로 막대: 값이 있는 항목은 최대값 대비 길이, 없는 항목은 '미수집' 파선 칸이다."""
    present = [row['value'] for row in values if row['value'] is not None]
    peak = max(present) if present else 1
    for index, row in enumerate(values):
        y = Y0 + .30 + index * 1.15
        put(text, wrapped_lines, slide, row['label'], X0, y, 2.2, .46, 12, True, 'primary')
        if row['value'] is None:
            box(slide, X0 + 2.3, y, 4.9, .50, 'white', True)
            put(text, wrapped_lines, slide, '미수집', X0 + 2.4, y + .10, 4.7, .30, BODY_MIN_PT, True, 'muted')
        else:
            length = max(.10, 3.2 * (row['value'] / peak) if peak else .10)
            box(slide, X0 + 2.3, y, length, .50, 'primary')
            shown = '{:,}'.format(row['value']) + ((' (' + row['note'] + ')') if row.get('note') else '')
            put(text, wrapped_lines, slide, shown, X0 + 2.38 + length, y + .02, max(1.8, 5.0 - length), .56, 11, True, 'ink')


def draw_scorecard(slide, text, box, wrapped_lines, values):
    cells = grid(len(values), X0, X0 + 7.4, .20)
    for (x, w), row in zip(cells, values):
        box(slide, x, Y0 + .30, w, 2.20, 'light')
        shown = '미수집' if row['value'] is None else '{:,}'.format(row['value'])
        put(text, wrapped_lines, slide, shown, x + .10, Y0 + .50, w - .20, .70, 26, True, 'primary')
        if row.get('note'):
            put(text, wrapped_lines, slide, row['note'], x + .10, Y0 + 1.20, w - .20, .42, BODY_MIN_PT, False, 'muted')
        put(text, wrapped_lines, slide, row['label'], x + .10, Y0 + 1.64, w - .20, .54, 12, False, 'ink')


def draw_form(form, slide, text, box, wrapped_lines, visual, connect, *, nodes=(), edges=(), facts=(), icon=None):
    """장표 한 장의 시각 형식을 그린다. 반환값은 그린 형식 이름이다."""
    data = visual['data']
    if form == 'architecture':
        draw_architecture(slide, text, box, wrapped_lines, data, list(nodes), list(edges), connect, icon)
    elif form == 'table':
        draw_table(slide, text, box, wrapped_lines, data)
    elif form == 'matrix':
        draw_matrix(slide, text, box, wrapped_lines, data)
    elif form == 'flow':
        draw_flow(slide, text, box, wrapped_lines, data, connect)
    elif form == 'chart':
        draw_chart(slide, text, box, wrapped_lines, metric_values(data['metrics'], facts))
    elif form == 'scorecard':
        draw_scorecard(slide, text, box, wrapped_lines, metric_values(data['items'], facts))
    else:
        raise ValueError('visual_form_unknown:' + str(form))
    return form
