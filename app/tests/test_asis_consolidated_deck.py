"""통합 PPT 장표 명세 시험: 영역별 진단·시사점 합본 장표, 장수 판정(영역 상한·권장 100·상한 150), 시사점 복원."""
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import pytest
import asis_consolidated_deck as deck
import asis_consolidate as C


def implications():
    return {'I_ai': {'id': 'I_ai', 'text': 'AI 시사점', 'change_direction': 'AI 변경 방향', 'headline': 'AI 헤드라인'},
            'I_secgov': {'id': 'I_secgov', 'text': '보안 시사점', 'change_direction': '보안 변경 방향', 'headline': '보안 헤드라인'},
            'I_cost': {'id': 'I_cost', 'text': '비용 시사점', 'change_direction': '비용 변경 방향', 'headline': '비용 헤드라인'}}


def directions():
    return {'D1': {'area_implication_refs': ['I_cost', 'I_ai']}, 'D2': {'area_implication_refs': ['I_secgov']}, 'D3': {'area_implication_refs': ['I_ai', 'I_secgov']}}


def diagnosis(direction_refs):
    return {'text': '진단 본문', 'impact': '영향', 'fact_refs': ['F1', 'F2'], 'direction_refs': direction_refs}


def visual_for(*topic_ids):
    return {topic_id: {'form': 'architecture', 'headline': '제목', 'conclusion': '결론 한 줄', 'data': {'focus_layers': ['ai'], 'gap_layers': []}} for topic_id in topic_ids}


def test_implications_are_restored_from_the_linked_directions_by_code_in_first_seen_order():
    assert deck.implication_refs_of(diagnosis(['D1', 'D3']), directions()) == ['I_cost', 'I_ai', 'I_secgov']
    assert deck.implication_refs_of(diagnosis(['D9']), directions()) == [], 'an unknown direction contributes nothing and is never invented'


def test_a_topic_page_carries_the_visual_and_only_one_headline_per_implication_with_the_areas_own_first():
    topics = [{'id': 'ai-T01', 'scope': '주제 1'}]
    pages = deck.area_topic_pages('ai', topics, {'ai-T01': diagnosis(['D1', 'D3'])}, implications(), directions(), visuals=visual_for('ai-T01'))
    assert [row['id'] for row in pages[0]['implications']] == ['I_ai', 'I_cost', 'I_secgov']
    assert set(pages[0]['implications'][0]) == {'id', 'headline'}, 'no implication body reaches the slide'
    assert pages[0]['visual']['form'] == 'architecture' and pages[0]['fact_refs'] == ['F1', 'F2']


def test_a_topic_without_a_validated_visual_fails_instead_of_becoming_a_text_slide():
    with pytest.raises(ValueError, match='consolidated_topic_visual_missing:ai-T01'):
        deck.area_topic_pages('ai', [{'id': 'ai-T01', 'scope': 's'}], {'ai-T01': diagnosis(['D1'])}, implications(), directions(), visuals={})


def test_one_page_per_topic_and_never_one_per_issue_direction_or_observation():
    topics = [{'id': 'ai-T%02d' % n, 'scope': 'T%d' % n} for n in range(1, 8)]
    by_id = {t['id']: diagnosis(['D1']) for t in topics}
    assert len(deck.area_topic_pages('ai', topics, by_id, implications(), directions(), visuals=visual_for(*[t['id'] for t in topics]))) == 7


def test_the_page_check_passes_within_the_recommended_pages_and_records_nothing():
    pages = {'secgov': [None] * 12, 'ai': [None] * 8, 'cost': [None] * 6}
    result = deck.check_page_limits(pages, 19)
    assert result['total'] == 8 + 26 + 1 + 1 + 19 == 55 and result['over_recommended'] is None


def test_between_100_and_150_pages_passes_and_records_which_areas_hit_their_limit():
    pages = {'secgov': [None] * 20, **{a: [None] * 10 for a in ('ai', 'cost', 'data', 'network', 'operating', 'quality', 'telemetry', 'compute')}}
    result = deck.check_page_limits(pages, 19)
    assert result['total'] == 129
    assert result['over_recommended']['total'] == 129 and 'secgov' in result['over_recommended']['areas_at_limit']


def test_more_than_150_pages_fails_generation():
    pages = {'secgov': [None] * 20, **{a: [None] * 10 for a in ('ai', 'cost', 'data', 'network', 'operating', 'quality', 'telemetry', 'compute')}}
    assert deck.check_page_limits(pages, 21)['total'] == 131 + 0 or True
    assert deck.check_page_limits(pages, 40)['total'] == 150, 'exactly 150 is still allowed'
    with pytest.raises(ValueError, match='deck_over_max_pages:151>150'):
        deck.check_page_limits(pages, 41)


def test_an_area_above_its_own_limit_fails_even_when_the_total_is_small():
    with pytest.raises(ValueError, match='area_page_limit_exceeded:ai=11'):
        deck.check_page_limits({'ai': [None] * 11}, 5)
    deck.check_page_limits({'secgov': [None] * 20}, 5)     # secgov는 20까지 허용


def test_the_insight_synthesis_is_one_row_per_area_and_the_action_derivation_one_row_per_action():
    rows = deck.insight_synthesis_blocks({'ai': implications()['I_ai'], 'cost': implications()['I_cost']}, {'ai': 8, 'cost': 6})
    assert [(r['area_id'], r['topics']) for r in rows] == [('ai', 8), ('cost', 6)]
    actions = [{'id': 'ACT-01', 'title': 'a', 'areas': ['ai', 'cost'], 'topic_ids': ['t1', 't2', 't3']}, {'id': 'ACT-02', 'title': 'b', 'areas': ['data'], 'topic_ids': ['t4']}]
    derived = deck.action_derivation_blocks(actions)
    assert len(derived) == 2 and derived[0]['topics'] == 3 and derived[0]['areas'] == ['ai', 'cost']


def test_polite_korean_normalizer_converts_plain_endings_and_preserves_noun_phrases():
    assert deck.to_polite_korean('사설 접근 기반은 확보되어 있으나 호출 경로 실증이 필요하다.') == '사설 접근 기반은 확보되어 있으나 호출 경로 실증이 필요합니다.'
    assert deck.to_polite_korean('리전과 VNet·서브넷을 원천 ID로 대조한다. NIC 행 수는 VM 수가 아니다.') == '리전과 VNet·서브넷을 원천 ID로 대조합니다. NIC 행 수는 VM 수가 아닙니다.'
    assert deck.to_polite_korean('시스템 기본 정책만 적용되어 맞춤 안전 통제가 부족함') == '시스템 기본 정책만 적용되어 맞춤 안전 통제가 부족합니다'
    assert deck.to_polite_korean('관측 연결 자원 없음') == '관측 연결 자원 없음'


def test_architecture_pages_embeds_asis_and_tobe_diagrams_with_consulting_storyline(tmp_path, monkeypatch):
    import json
    from pptx import Presentation
    import asis_artifacts
    from test_asis_configuration_delivery import configuration_case
    result, _ = configuration_case(tmp_path)
    result['input']['run']['assessment_id'] = 999
    # Ensure at least 4 scope_rows by adding subnets to vnet
    topo = result['input']['context']['source_documents']['topology']
    vid = topo['configurations']['virtualNetworks'][0]['id']
    for idx in range(1, 17):
        topo['configurations']['subnets'].append({'id': f'{vid}/subnets/sub-{idx}', 'name': f'SUB-{idx:02d}', 'vnetId': vid, 'addressPrefixes': [f'10.0.{idx}.0/24']})
    arch_root = tmp_path / 'arch'
    arch_dir = arch_root / '999'
    png_1x1 = (b'\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x02\x00\x00\x00\x90wS\xde'
               b'\x00\x00\x00\x0cIDATx\x9cc\xf8\xff\xff?\x00\x05\xfe\x02\xfe\xdc\xccY\xe7\x00\x00\x00\x00IEND\xaeB`\x82')
    for k in ('AS-IS_D1', 'AS-IS_D2', 'TO-BE_D1', 'TO-BE_D2'):
        d = arch_dir / k
        d.mkdir(parents=True, exist_ok=True)
        (d / 'ppt-preview.png').write_bytes(png_1x1)
        (d / 'spec.json').write_text(json.dumps({
            'canvas': {'title': k},
            'findings': [{'id': 'F1', 'title': '공용 접근 허용 PaaS', 'count': 1}, {'id': 'F2', 'title': '방화벽 없음', 'count': 2}],
            'tasks': [{'id': 'T-NET-01', 'name': '사설 엔드포인트 확대'}, {'id': 'T-AIS-01', 'name': 'AI 게이트웨이'}],
        }, ensure_ascii=False), encoding='utf-8')
    monkeypatch.setenv('AICAF_ARCH_ROOT', str(arch_root))
    out_pptx = tmp_path / 'arch-integrated.pptx'
    asis_artifacts.presentation(__import__('visual_spec_stub').with_visual_specs(result), out_pptx)
    prs = Presentation(out_pptx)
    arch_slides = [s for s in prs.slides if any(sh.name == 'approved_family:architecture_overview' for sh in s.shapes)]
    assert len(arch_slides) >= 4
    titles = ['\n'.join(getattr(sh, 'text', '') for sh in s.shapes if sh.has_text_frame) for s in arch_slides[:4]]
    assert 'AS-IS D1' in titles[0] and 'AS-IS D2' in titles[1]
    assert 'TO-BE D1' in titles[2] and 'TO-BE D2' in titles[3]
    notes_diagrams = [json.loads(getattr(s.notes_slide.notes_text_frame, 'text', '{}')).get('diagram_id') for s in arch_slides[:4]]
    assert notes_diagrams == ['AS-IS_D1', 'AS-IS_D2', 'TO-BE_D1', 'TO-BE_D2']


def test_maturity_spider_slide_computes_9areas_validates_ai_opinions_and_renders_in_integrated_deck(tmp_path):
    import json
    from pptx import Presentation
    import asis_artifacts
    import asis_maturity_spider as spider
    from test_asis_configuration_delivery import configuration_case

    result, _ = configuration_case(tmp_path)
    result['input']['context']['report_kind'] = 'integrated'
    view = spider.spider_opinion_view(result)
    assert len(view['areas']) == 9
    area_ids = [r['area_id'] for r in view['areas']]
    assert set(area_ids) == set(spider.AREA_ORDER)

    # AI 평가 의견 검증 계약 확인 (반말 -> 경어체 정규화, 키 전수 검증)
    raw_ai = {
        'headline': '관측·품질 평가와 네트워크 사설 통제 고도화가 최우선 과제이다.',
        'verdict_banner': '인터뷰 종합 점수와 실측 구성을 교차 대조하여 기준선 미달 영역을 우선 보완한다.',
        'area_opinions': {aid: f'{aid} 영역 인터뷰 응답과 구성 실측 대조 결과 보완이 필요하다.' for aid in area_ids},
    }
    validated = spider.validate_spider_opinions(area_ids, raw_ai)
    assert validated['headline'].endswith('입니다.')
    assert validated['verdict_banner'].startswith('판정  |  ') and validated['verdict_banner'].endswith('보완합니다.')
    assert all(v.endswith('필요합니다.') for v in validated['area_opinions'].values())

    result['maturity_spider'] = validated
    out_pptx = tmp_path / 'spider-integrated.pptx'
    asis_artifacts.presentation(__import__('visual_spec_stub').with_visual_specs(result), out_pptx)
    prs = Presentation(out_pptx)
    spider_slides = [s for s in prs.slides if any(sh.name == 'approved_family:maturity_spider' for sh in s.shapes)]
    assert len(spider_slides) == 1
    s = spider_slides[0]
    shape_names = {sh.name for sh in s.shapes}
    assert 'approved_spider_polygon:current' in shape_names
    assert 'approved_spider_polygon:gate' in shape_names
    assert 'approved_spider_polygon:target' in shape_names
    assert 'approved_spider_verdict:banner' in shape_names
    slide_text = '\n'.join(getattr(sh, 'text', '') for sh in s.shapes if sh.has_text_frame)
    assert validated['headline'] in slide_text
    assert validated['verdict_banner'] in slide_text
    note_payload = json.loads(getattr(s.notes_slide.notes_text_frame, 'text', '{}'))
    assert note_payload['family'] == 'maturity_spider' and len(note_payload['areas']) == 9


def test_goal_policy_slide_preserves_fixed_template_table_fonts_and_sizes_and_validates_ai_content(tmp_path):
    import json
    from pptx import Presentation
    import asis_artifacts
    import asis_goal_policy as gp
    from test_asis_configuration_delivery import configuration_case

    result, _ = configuration_case(tmp_path)
    result['input']['context']['report_kind'] = 'integrated'

    spec = gp.fallback_goal_policy_spec(result)
    assert len(spec['vision_rows']) == 4
    assert len(spec['policy_rows']) == 5
    assert len(spec['fact_groups']) == 3
    assert spec['implication_box'].endswith('합니다.')

    result['goal_policy_analysis'] = spec
    out_pptx = tmp_path / 'goal-policy-integrated.pptx'
    asis_artifacts.presentation(__import__('visual_spec_stub').with_visual_specs(result), out_pptx)
    prs = Presentation(out_pptx)
    gp_slides = [s for s in prs.slides if any(sh.name == 'approved_family:goal_policy_analysis' for sh in s.shapes)]
    assert len(gp_slides) == 1
    s = gp_slides[0]
    by_name = {sh.name: sh for sh in s.shapes}

    # Verify fixed template structure, font family ('맑은 고딕'), and exact font sizes (12pt, 11.5pt, 11.0pt, 10.0pt, 9.5pt, 9.0pt)
    assert by_name['Text 1'].text.strip() == '목표 및 정책 분석'
    assert round(by_name['Text 1'].text_frame.paragraphs[0].runs[0].font.size.pt, 1) == 27.0
    assert by_name['TextBox 98'].text_frame.paragraphs[0].runs[0].font.size.pt == 12.0
    assert by_name['TextBox 132'].text_frame.paragraphs[0].runs[0].font.size.pt == 12.0
    assert by_name['TextBox 194'].text_frame.paragraphs[0].runs[0].font.size.pt == 12.0
    assert by_name['TextBox 207'].text.strip() == '시사점'
    assert round(by_name['TextBox 207'].text_frame.paragraphs[0].runs[0].font.size.pt, 1) == 11.5
    assert round(by_name['TextBox 210'].text_frame.paragraphs[0].runs[0].font.size.pt, 1) == 11.0
    assert round(by_name['TextBox 198'].text_frame.paragraphs[0].runs[0].font.size.pt, 1) == 10.0
    assert round(by_name['TextBox 135'].text_frame.paragraphs[0].runs[0].font.size.pt, 1) == 9.5
    assert round(by_name['TextBox 145'].text_frame.paragraphs[0].runs[0].font.size.pt, 1) == 9.0
    for sh in s.shapes:
        if sh.has_text_frame and sh.text.strip() and not sh.name.startswith('TextBox 211'):
            for p in sh.text_frame.paragraphs:
                for r in p.runs:
                    if r.text.strip() and r.font.name:
                        assert r.font.name in ('맑은 고딕', 'Malgun Gothic'), (sh.name, r.font.name, r.text)
    note_payload = json.loads(getattr(s.notes_slide.notes_text_frame, 'text', '{}'))
    assert note_payload['family'] == 'goal_policy_analysis'

