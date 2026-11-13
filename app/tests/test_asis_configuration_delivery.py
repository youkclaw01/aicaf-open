"""구성 참조가 실제 PPT·현황 원장에 함께 전달되는지 검증한다. 입력은 합성 단위 시험이다."""
from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
import sys

from openpyxl import load_workbook
from pptx import Presentation

APP = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(APP))

import asis_analysis
import asis_artifacts
import asis_model

SCHEMA = 'asis-configuration-relationships/1'


def configuration_case(tmp_path, extra_vnets=0):
    """실제 조회·고객 자료가 아닌, 원본 파일과 필드 경로가 일치하는 합성 입력이다."""
    prefix = '/subscriptions/11111111-1111-1111-1111-111111111111/resourceGroups/synthetic-unit/providers/'
    vnet = prefix + 'Microsoft.Network/virtualNetworks/vnet-unit'
    subnet = vnet + '/subnets/subnet-unit'
    endpoint = prefix + 'Microsoft.Network/privateEndpoints/endpoint-unit'
    zone = prefix + 'Microsoft.Network/privateDnsZones/privatelink.unit.invalid'
    account = prefix + 'Microsoft.CognitiveServices/accounts/account-unit'
    deployment = account + '/deployments/deployment-unit'
    configurations = {
        'virtualNetworks': [{'id': vnet, 'name': 'vnet-unit', 'addressPrefixes': ['10.20.0.0/16']}],
        'subnets': [{'id': subnet, 'name': 'subnet-unit', 'vnetId': vnet, 'addressPrefixes': ['10.20.1.0/24']}],
        'privateEndpoints': [{'id': endpoint, 'name': 'endpoint-unit', 'subnetId': subnet}],
        'dnsConfiguration': [{'id': zone, 'name': 'privatelink.unit.invalid', 'type': 'microsoft.network/privateDnsZones'}],
        'privateDnsLinks': [{'id': zone + '/virtualNetworkLinks/link-unit', 'zoneId': zone, 'vnetId': vnet}],
        'foundryAccountConfiguration': [{'id': account, 'name': 'account-unit', 'publicNetworkAccess': 'Disabled'}],
    }
    for number in range(extra_vnets):
        identifier = prefix + 'Microsoft.Network/virtualNetworks/vnet-extra-' + str(number)
        configurations['virtualNetworks'].append({'id': identifier, 'name': 'vnet-extra-' + str(number)})
        configurations['privateDnsLinks'].append({'id': zone + '/virtualNetworkLinks/link-extra-' + str(number),
                                                'zoneId': zone, 'vnetId': identifier})
    resources = [{'id': row['id'], 'name': row.get('name', row['id'].rsplit('/', 1)[-1])}
                 for rows in configurations.values() for row in rows]
    topology = {
        'resources': resources,
        'configurations': configurations,
        'verification': {'resourceCount': {'complete': True, 'collected': len(resources), 'uniqueIds': len(resources)}},
        'coverage': {'detailDatasets': {key: {'status': 'complete', 'recordCount': len(rows)}
                                      for key, rows in configurations.items()}},
        'collection': {'subscriptions': [], 'completedAtUtc': None},
    }
    records = {
        'privateEndpointDnsZoneGroups': [{'privateEndpointId': endpoint, 'zones': [{'privateDnsZoneId': zone}]}],
        'aiDeployments': [{'id': deployment, 'name': 'deployment-unit', 'accountId': account,
                           'modelName': 'SYNTHETIC-NOT-A-MODEL', 'skuName': 'SYNTHETIC'}],
    }
    evidence = {'sections': {key: {'status': 'collected', 'recordCount': len(rows), 'errorCount': 0, 'records': rows}
                             for key, rows in records.items()}}
    source = tmp_path / 'synthetic-configuration-source'
    source.mkdir()
    originals = {'topology.json': topology, 'aicaf-evidence.json': evidence}
    for name, value in originals.items():
        (source / name).write_text(json.dumps(value, ensure_ascii=False), encoding='utf-8')
    assessment = {'id': 999002, 'customer': 'SYNTHETIC UNIT TEST - NOT A CUSTOMER',
                  'title': 'Configuration delivery only', 'infra_case_id': 'synthetic-configuration-unit', 'track': 'existing'}
    data = asis_model.normalize(source, assessment, [], 'synthetic-configuration-r1',
                                classification='synthetic_unit_test_not_customer')
    return __import__('visual_spec_stub').with_visual_specs(asis_analysis.analyze(data)), originals


def configuration_notes(deck):
    found = []
    for slide in deck.slides:
        try:
            note = json.loads(slide.notes_slide.notes_text_frame.text)
        except (json.JSONDecodeError, TypeError):
            continue
        if isinstance(note, dict) and note.get('schema') == SCHEMA:
            found.append((slide, note))
    return found


def resolve_pointer(value, path):
    for key in path.strip('/').split('/'):
        key = key.replace('~1', '/').replace('~0', '~')
        value = value[int(key)] if isinstance(value, list) else value[key]
    return value


def test_configuration_refs_reach_native_ppt_and_same_backdata(tmp_path):
    result, originals = configuration_case(tmp_path)
    frozen = deepcopy(result)
    graphs = [asis_artifacts.configuration_relationships(result['input'], area) for area in ('network', 'ai')]
    assert all(graph['edges'] for graph in graphs), '합성 입력의 명시 관계가 먼저 확인되어야 한다'
    expected = {edge['id']: edge for graph in graphs for edge in graph['edges']}
    for edge in expected.values():
        referenced = edge['from_id'] if edge['relation'] == 'account_deployment' else edge['to_id']
        for proof in edge['provenance']:
            assert resolve_pointer(originals[proof['source_id']], proof['field_path']) == referenced
    pptx = tmp_path / 'configuration.pptx'
    asis_artifacts.presentation(__import__('visual_spec_stub').with_visual_specs(result), pptx)
    notes = configuration_notes(Presentation(pptx))
    delivered = {edge['id'] for _, note in notes for edge in note['edges']}
    assert delivered == set(expected), '관계 계산만으로 끝내지 않고 실제 다운로드 PPT에 전달해야 한다'
    connectors = [shape for slide, _ in notes for shape in slide.shapes
                  if shape.name.startswith('configuration_edge:')]
    assert {shape.name.split(':')[1] for shape in connectors} == set(expected)
    assert all(shape._element.tag.endswith('}cxnSp') for shape in connectors)
    assert all(note['runtime_verified'] is False for _, note in notes)
    xlsx = tmp_path / 'configuration.xlsx'
    asis_artifacts.workbook(result, xlsx)
    book = load_workbook(xlsx, data_only=False)
    assert '구성 연결' in book.sheetnames
    rows = [row for row in book['구성 연결'].iter_rows(min_row=7) if row[0].value]
    delivered_excel = set()
    for row in rows:
        identifier, provenance = row[0].value, row[6].value
        assert isinstance(identifier, str) and isinstance(provenance, str)
        delivered_excel.add(identifier)
        assert json.loads(provenance) == expected[identifier]['provenance']
    assert delivered_excel == set(expected)
    assert result == frozen, '전달 단계는 동결 입력·분석 원장을 변경하지 않는다'
