"""Terraform(aztfexport) 상태와 수집 스냅샷 원본의 대조기.

목적: Terraform 변환이 수집 원본과 값이 다르게 만드는 지점을 코드로 표시한다. 이 모듈은 근거를 만들지 않는다.
  - 입력은 이미 확보된 두 문서뿐이다: 수집 원본(`topology.json`의 구성 레코드)과 Terraform state(`terraform.tfstate`).
  - 네트워크·Azure·파일 쓰기를 하지 않는다. 결과는 호출자가 받아 쓴다.
  - 필드 대응은 실측(2026-10-16, PROD-YOUK 구독, aztfexport v0.20.0)으로 확인한 것만 FIELD_MAP에 선언한다.
    선언되지 않은 필드는 비교하지 않고 `unmapped_fields`로만 보고한다(추측 변환 금지).

판정 값
  match        두 값이 의미상 같다
  value_filled 원본에는 관측값이 없는데(null) Terraform이 기본값(false/0 등)을 채웠다. 근거로 쓰면 안 된다
  mismatch     두 값이 의미상 다르다
  both_absent  두 쪽 모두 관측값이 없다
  tf_only      Terraform에만 값이 있다(원본에 필드가 없다)
"""
from __future__ import annotations

from typing import Any

SCHEMA = 'asis-terraform-comparison/1'

# (수집 원본 필드, Terraform 속성, 변환) — 변환은 값 의미를 보존하는 것만 둔다.
#   same     그대로 비교
#   invert   논리 반대(disableX ↔ xEnabled)
#   public   'Enabled'/'Disabled' 문자열 ↔ bool
#   lower    대소문자 무시 문자열
FIELD_MAP: dict[str, dict[str, Any]] = {
    'azurerm_cognitive_account': {
        'source': 'foundryAccountConfiguration',
        'fields': [
            ('kind', 'kind', 'same'),
            ('sku', 'sku_name', 'sku_name'),
            ('customSubDomainName', 'custom_subdomain_name', 'lower'),
            ('publicNetworkAccess', 'public_network_access_enabled', 'public'),
            ('localAuthenticationDisabled', 'local_auth_enabled', 'invert'),
            ('dynamicThrottlingEnabled', 'dynamic_throttling_enabled', 'same'),
            ('restrictOutboundNetworkAccess', 'outbound_network_access_restricted', 'same'),
        ],
    },
}

_ABSENT = object()


def _norm(value: Any) -> Any:
    if value in ('', [], {}):
        return None
    return value


def _convert(kind: str, source: Any, terraform: Any) -> tuple[Any, Any]:
    """두 값을 같은 의미 축으로 맞춘다. 변환하지 못하면 원래 값을 돌려준다."""
    if kind == 'invert':
        return (None if source is None else (not source), terraform)
    if kind == 'public':
        if isinstance(source, str) and source.casefold() in ('enabled', 'disabled'):
            return (source.casefold() == 'enabled', terraform)
        return (source, terraform)
    if kind == 'lower':
        return (source.casefold() if isinstance(source, str) else source, terraform.casefold() if isinstance(terraform, str) else terraform)
    if kind == 'sku_name':
        name = source.get('name') if isinstance(source, dict) else source
        return (name, terraform)
    return (source, terraform)


def classify(kind: str, source: Any, terraform: Any) -> str:
    """한 필드의 판정. 원본의 '관측 없음'(None/빈 값)과 Terraform의 채워진 값(false/0)을 구분하는 것이 핵심이다."""
    expected, actual = _convert(kind, source, terraform)
    if expected is _ABSENT:
        return 'tf_only' if _norm(actual) is not None or actual is False or actual == 0 else 'both_absent'
    if expected is None and (actual is None or actual in ('', [], {})):
        return 'both_absent'
    if expected is None:
        return 'value_filled'
    if actual is None or actual in ('', [], {}):
        return 'mismatch'
    return 'match' if expected == actual else 'mismatch'


def _tf_attributes(state: dict) -> dict[str, list[dict]]:
    """state를 {terraform 타입: [속성 사전, ...]}로 읽는다. data 모드는 제외한다."""
    out: dict[str, list[dict]] = {}
    for resource in state.get('resources', []):
        if resource.get('mode') != 'managed':
            continue
        for instance in resource.get('instances', []):
            out.setdefault(resource['type'], []).append(instance.get('attributes') or {})
    return out


def compare(source_configuration: dict[str, list[dict]], state: dict) -> dict:
    """수집 원본 구성 레코드와 Terraform state를 리소스 ID로 짝지어 대조한다.

    source_configuration: {'foundryAccountConfiguration': [레코드, ...], ...} (topology.json의 configurations)
    반환: 리소스별 필드 판정, 요약 건수, 짝이 없는 쪽의 ID 목록, 비교하지 않은 필드 목록.
    """
    tf = _tf_attributes(state)
    rows: list[dict] = []
    summary = {'match': 0, 'value_filled': 0, 'mismatch': 0, 'both_absent': 0, 'tf_only': 0}
    only_source: list[str] = []
    only_terraform: list[str] = []
    unmapped: dict[str, dict[str, list[str]]] = {}
    for tf_type, spec in FIELD_MAP.items():
        records = {str(row.get('id', '')).casefold(): row for row in source_configuration.get(spec['source'], []) if isinstance(row, dict)}
        attributes = {str(item.get('id', '')).casefold(): item for item in tf.get(tf_type, [])}
        only_source += sorted(records.keys() - attributes.keys())
        only_terraform += sorted(attributes.keys() - records.keys())
        declared_source = {field[0] for field in spec['fields']}
        declared_tf = {field[1] for field in spec['fields']}
        for rid in sorted(records.keys() & attributes.keys()):
            fields = []
            for source_field, tf_field, kind in spec['fields']:
                source_value = records[rid].get(source_field, _ABSENT)
                tf_value = attributes[rid].get(tf_field)
                verdict = classify(kind, None if source_value is _ABSENT else source_value, tf_value) if source_value is not _ABSENT else classify(kind, _ABSENT, tf_value)
                if source_value is _ABSENT:
                    verdict = 'tf_only' if (tf_value is not None and tf_value not in ('', [], {})) else 'both_absent'
                summary[verdict] += 1
                fields.append({'source_field': source_field, 'terraform_field': tf_field, 'transform': kind,
                               'source_value': None if source_value is _ABSENT else source_value, 'terraform_value': tf_value, 'verdict': verdict})
            rows.append({'resource_id': rid, 'terraform_type': tf_type, 'fields': fields})
        sample_source = next(iter(records.values()), {})
        sample_tf = next(iter(attributes.values()), {})
        unmapped[tf_type] = {'source_fields_not_compared': sorted(set(sample_source) - declared_source),
                             'terraform_attributes_not_compared': sorted(set(sample_tf) - declared_tf)}
    return {'schema': SCHEMA, 'summary': summary, 'resources': rows, 'only_in_source': only_source,
            'only_in_terraform': only_terraform, 'unmapped_fields': unmapped,
            'usable_as_evidence': summary['value_filled'] == 0 and summary['mismatch'] == 0 and not only_source}
