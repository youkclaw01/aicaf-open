"""영역별 진단·과제 계획 호출을 흉내 내는 시험용 응답. 입력(view)이 담은 이슈와 방향 연결만으로 올바른 영역 계획을 만든다."""


def area_item_plan_answer(view):
    issues = view['issues']
    return {'diagnoses': [{'scope': row['scope'], 'reason': '합성 항목별 계획 시험', 'issue_refs': [row['id']], 'direction_refs': list(row['direction_refs'])} for row in issues],
            'actions': [{'scope': row['scope'], 'reason': '합성 항목별 계획 시험', 'issue_refs': [row['id']], 'direction_refs': list(row['direction_refs']), 'diagnosis_indices': [k]}
                        for k, row in enumerate(issues)],
            'coverage': [{'issue_id': row['id'], 'diagnosis_indices': [k], 'action_indices': [k], 'disposition': 'included', 'reason': '합성 항목 배정', 'required_evidence': ''}
                         for k, row in enumerate(issues)]}


def is_area_item_plan_message(message):
    """영역 계획 호출인지: 입력이 stage_data 없이 area·issues·directions를 가진다."""
    try:
        import json
        view = json.loads(message.rsplit('\n입력:\n', 1)[-1])
    except (ValueError, IndexError):
        return None
    return view if isinstance(view, dict) and 'area' in view and 'issues' in view and 'stage_data' not in view else None


def write_synthesis_plan_links(pipe, data):
    """운영에서는 종합 단계가 종합 계획 체크포인트를 남긴다. 시험 입력도 같은 모양으로 이슈→방향 연결을 남긴다.
    승인 방향 i는 이슈 계획의 direction_id가 같은 이슈를 인용한 것으로 둔다."""
    import json
    approved = [row['id'] for row in data['stage_data']['approved_directions']['directions']]
    issues = data['context']['analysis_plan']['issues']
    slots = [{'issue_refs': [issue['id'] for issue in issues if issue.get('direction_id') == direction_id] or [issues[min(i, len(issues) - 1)]['id']]}
             for i, direction_id in enumerate(approved)]
    folder = pipe.folder / 'checkpoints' / 'synthesis-plan' / 'test-links'
    folder.mkdir(parents=True, exist_ok=True)
    (folder / 'accepted.json').write_text(json.dumps({'candidate': {'directions': slots}}), encoding='utf-8')


def consolidation_view(message):
    """통합 호출(진단 주제·과제 통합)인지: 입력이 candidates와 limit을 가진다."""
    try:
        import json
        view = json.loads(message.rsplit('\n입력:\n', 1)[-1])
    except (ValueError, IndexError):
        return None
    return view if isinstance(view, dict) and 'candidates' in view and 'limit' in view else None


def consolidation_answer(view, per_group=2):
    """시험용 통합 응답: 후보를 per_group개씩 묶되 상한을 넘지 않게 묶음 크기를 키운다."""
    count = len(view['candidates'])
    size = max(per_group, -(-count // view['limit']))
    groups = [list(range(start, min(start + size, count))) for start in range(0, count, size)]
    key = 'topics' if 'area' in view else 'actions'
    return {key: [{'title': '통합 %d' % (n + 1), 'reason': '합성 통합 시험', 'members': members} for n, members in enumerate(groups)]}


def new_stage_answer(view):
    """통합 단계와 장표 시각 명세 단계의 호출(입력에 stage_data가 없는 소형 입력)에 대한 시험용 응답. 해당하지 않으면 None이다.

    시험 대역은 이 함수를 먼저 불러 보고, None이면 자기 기존 응답 경로로 넘어간다. 한 곳에서 맞추므로 새 호출이 생겨도 대역마다 고치지 않는다."""
    if not isinstance(view, dict) or 'stage_data' in view:
        return None
    if 'candidates' in view and 'limit' in view:
        # 항목 작성·검토 시험은 통합 자체를 보지 않으므로 후보를 묶지 않고 하나씩 그대로 돌려준다(통합 시험은 per_group을 직접 정한다).
        return consolidation_answer(view, per_group=1)
    if 'topics' in view and 'layers' in view:
        return {'visuals': {topic['topic_id']: {'form': 'architecture', 'headline': '구성 개요', 'conclusion': '핵심 결론 한 줄',
                                                  'data': {'focus_layers': ['network'], 'gap_layers': ['network']}} for topic in view['topics']}}
    return None
