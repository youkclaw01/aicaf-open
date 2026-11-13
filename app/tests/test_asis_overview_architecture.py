"""개념 아키텍처: 계층 계산, 단순/복잡 판단, 그림 골격·프롬프트, 판독 검사, 그림 생성 재시도(모델 대역)."""
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import pytest
import asis_overview_architecture as A
import asis_architecture_image as I


def resources():
    kinds = ['microsoft.network/virtualnetworks'] * 2 + ['microsoft.network/networksecuritygroups'] * 3 + ['microsoft.compute/virtualmachines'] * 2 + \
            ['microsoft.cognitiveservices/accounts'] + ['microsoft.storage/storageaccounts'] * 2 + ['microsoft.compute/virtualmachines/extensions'] * 4
    return [{'id': '/r/%d' % i, 'type': t} for i, t in enumerate(kinds)]


def test_resources_become_layer_nodes_with_counts_and_sub_resources_are_not_counted():
    nodes = {n['layer']: n for n in A.overview_nodes(resources())}
    assert nodes['network']['count'] == 5 and nodes['compute']['count'] == 2 and nodes['ai']['count'] == 1 and nodes['data']['count'] == 2
    assert 'edge' not in nodes, 'a layer with no resource is not drawn'
    assert all(len(n['types']) <= 3 for n in nodes.values()), 'type names only, never resource names'


def test_layer_edges_use_declared_relationships_only_and_skip_inferred_or_traffic_claims():
    res = [{'id': '/a', 'type': 'microsoft.network/privateendpoints'}, {'id': '/b', 'type': 'microsoft.cognitiveservices/accounts'},
           {'id': '/c', 'type': 'microsoft.storage/storageaccounts'}]
    rels = [{'sourceId': '/a', 'targetId': '/b', 'inferred': False, 'observedTraffic': False},
            {'sourceId': '/a', 'targetId': '/c', 'inferred': True, 'observedTraffic': False},
            {'sourceId': '/a', 'targetId': '/c', 'inferred': False, 'observedTraffic': True},
            {'sourceId': '/a', 'targetId': '/a', 'inferred': False, 'observedTraffic': False}]
    assert A.overview_edges(res, rels) == [{'from': 'ai', 'to': 'network', 'count': 1}]


def test_a_simple_overview_is_drawn_natively_and_a_complex_one_as_an_image():
    nodes = [{'layer': k, 'label': A.layer_label(k), 'count': 1, 'types': []} for k in ('edge', 'network', 'compute', 'ai', 'data', 'observability', 'identity')]
    assert A.needs_image(nodes, [{'from': 'a', 'to': 'b', 'count': 1}] * 6) is False
    assert A.needs_image(nodes, [{'from': 'a', 'to': 'b', 'count': 1}] * 7) is True
    assert A.needs_image(nodes + [{'layer': 'other', 'label': '기타', 'count': 1, 'types': []}], []) is True


def skeleton():
    nodes = [{'layer': 'network', 'label': '네트워크 경계', 'count': 40, 'types': []}, {'layer': 'ai', 'label': 'AI 서비스', 'count': 3, 'types': []}]
    return A.image_skeleton(nodes, [{'from': 'ai', 'to': 'network', 'count': 3}], ['ai'], ['network'], 'AS-IS 개요')


def test_the_image_prompt_fixes_names_counts_links_emphasis_and_gaps_and_forbids_detail():
    prompt = A.image_prompt(skeleton())
    for must in ('네트워크 경계 (40)', 'AI 서비스 (3)', 'AI 서비스 - 네트워크 경계', 'CONCEPTUAL', 'no individual resources', 'orange dashed', '확인 필요'):
        assert must in prompt, must
    assert 'REVISION' in A.image_prompt(skeleton(), fix='remove the extra block')


def test_the_reading_check_reports_missing_and_extra_blocks_with_a_fix_instruction():
    sk = skeleton()
    ok = {'nodes': [{'id': 'network', 'found': True}, {'id': 'ai', 'found': True}], 'extras': []}
    assert A.check_image_reading(sk, ok) == []
    bad = {'nodes': [{'id': 'network', 'found': True}, {'id': 'ai', 'found': False}], 'extras': [{'label': 'Kubernetes'}]}
    codes = {f['code'] for f in A.check_image_reading(sk, bad)}
    assert codes == {'I1', 'I2'}


def test_image_generation_retries_with_only_the_failed_items_and_gives_up_after_three_rounds():
    sk = skeleton(); prompts = []
    def draw(prompt, refs):
        prompts.append(prompt); return b'png-%d' % len(prompts)
    reads = iter([{'nodes': [{'id': 'network', 'found': True}, {'id': 'ai', 'found': False}], 'extras': []},
                  {'nodes': [{'id': 'network', 'found': True}, {'id': 'ai', 'found': True}], 'extras': []}])
    out = I.make_image(sk, draw, lambda prompt, png: next(reads))
    assert out['attempts'] == 2 and out['png'] == b'png-2' and out['model'] == 'gpt-image-2.5-sunburst'
    assert 'REVISION' not in prompts[0] and 'REVISION' in prompts[1] and 'AI 서비스' in prompts[1]
    never = {'nodes': [], 'extras': []}
    with pytest.raises(RuntimeError, match='architecture_image_failed'):
        I.make_image(sk, draw, lambda prompt, png: never)
