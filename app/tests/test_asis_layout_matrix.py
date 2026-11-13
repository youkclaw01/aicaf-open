"""다대다·장문 구성의 네이티브 도형 충돌 회귀. 합성 자료이며 실제 Azure가 아니다."""
from copy import deepcopy
from pathlib import Path
import sys

from pptx import Presentation

APP = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP))
import asis_artifacts
from test_asis_configuration_delivery import configuration_case, configuration_notes


def test_multiple_dns_vnet_targets_do_not_share_same_rectangle(tmp_path):
    result, _ = configuration_case(tmp_path, extra_vnets=4)
    path = tmp_path / 'many-links.pptx'
    asis_artifacts.presentation(__import__('visual_spec_stub').with_visual_specs(result), path)
    collisions = []
    for number, (slide, _) in enumerate(configuration_notes(Presentation(path)), 1):
        positions = {}
        for shape in slide.shapes:
            if not shape.name.startswith('configuration_node:'):
                continue
            rectangle = (shape.left, shape.top, shape.width, shape.height)
            identifier = shape.name.split(':', 1)[1]
            if rectangle in positions and positions[rectangle] != identifier:
                collisions.append((number, positions[rectangle], identifier))
            positions[rectangle] = identifier
    assert not collisions, collisions


def test_long_resource_names_are_paginated_without_missing_relationships(tmp_path):
    result, _ = configuration_case(tmp_path)
    result = deepcopy(result)
    for fact in result['input']['facts']:
        if isinstance(fact['value'], list):
            for row in fact['value']:
                if isinstance(row, dict) and 'name' in row:
                    row['name'] = '합성장문리소스이름-공유사설연결과운영범위확인-' * 5
    expected = {edge['id'] for area in ('network', 'ai')
                for edge in asis_artifacts.configuration_relationships(result['input'], area)['edges']}
    label = '합성장문리소스이름-공유사설연결과운영범위확인-' * 5
    wrapped = asis_artifacts.wrapped_lines(label, 2.36, 10.4)
    assert len(wrapped) > 1, '공백 없는 리소스 이름도 상자 폭으로 분할해야 한다'
    assert ''.join(wrapped) == label
    path = tmp_path / 'long-labels.pptx'
    asis_artifacts.presentation(__import__('visual_spec_stub').with_visual_specs(result), path)
    actual = {edge['id'] for _, note in configuration_notes(Presentation(path)) for edge in note['edges']}
    assert actual == expected
