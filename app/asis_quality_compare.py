"""모델 품질 비교용 채점기: 같은 입력(관측 O_P0014, 영역 network)으로 두 모델의 관측 작성 결과를 코드로 채점한다.

비교 대상은 모델이 쓴 관측 문장이다. 채점 항목은 입력의 실제 값에서 코드가 계산한 정답이며, 모델 판단·승인 산출물을 정답으로 쓰지 않는다.
정답은 값을 직접 읽어 정했다(docs 참조). 점수는 항목별 통과/실패와 사유만 낸다. 합산 점수를 만들어 순위를 정하지 않는다.
"""
from __future__ import annotations

import json
import re

# 입력 사실에서 읽은 값 (F_T_applicationGatewayConfiguration, F_E_wafPolicies, F_IV_XB3-4_transcript)
GROUND = {
    'gateway_name': 'AG-HUB-01',
    'waf_policy_name': 'WAF-01',
    'waf_state': 'Enabled',
    'waf_mode': 'Detection',
    'rule_set': 'Microsoft_DefaultRuleSet',
    'rule_set_version': '2.2',
    'custom_rule_count': 0,
    'waf_scope_complete': False,
    'gateway_scope_complete': True,
    'interview_waf_claim': '웹 방화벽 기능은 켜지 않았다',
}

CHECKS = []


def check(name, why):
    def deco(fn):
        CHECKS.append((name, why, fn))
        return fn
    return deco


def _has(text, *needles):
    return all(n.casefold() in text.casefold() for n in needles)


@check('mode_is_detection', 'WAF 모드는 Detection이다(Prevention이 아니다)')
def _mode(t):
    return _has(t, 'Detection')


@check('state_is_enabled', '정책 state는 Enabled다')
def _state(t):
    return _has(t, 'Enabled')


@check('policy_named', '연결된 정책 이름 WAF-01을 말한다')
def _policy(t):
    return _has(t, 'WAF-01')


@check('gateway_named', '게이트웨이 이름 AG-HUB-01을 말한다')
def _gateway(t):
    return _has(t, 'AG-HUB-01')


@check('rule_set', '규칙 세트 Microsoft_DefaultRuleSet을 말한다')
def _ruleset(t):
    return _has(t, 'Microsoft_DefaultRuleSet')


@check('interview_claim_reported', '인터뷰 진술(웹 방화벽을 켜지 않았다)을 진술로 구분해 옮긴다')
def _claim(t):
    interview = bool(re.search(r'인터뷰|진술|설명했|답했|고객', t))
    off = bool(re.search(r'켜지\s*않|사용하지\s*않|비활성|꺼', t))
    return interview and off


@check('config_and_claim_both_present', '구성값(Enabled·Detection)과 진술이 한 서술에 함께 있다')
def _both(t):
    return _has(t, 'Detection') and _has(t, 'Enabled') and bool(re.search(r'켜지\s*않|사용하지\s*않|비활성|꺼', t))


@check('no_detection_equals_blocking', 'Detection을 차단(Prevention)으로 읽지 않는다', )
def _nodetect(t):
    bad = re.search(r'(Detection)[^.。]{0,30}(차단\s*(한다|합니다|하고|됨|된다)|방어한다)', t)
    return not bad


@check('waf_scope_incomplete_stated', 'WAF 정책 자료의 scope_complete=false(부분 수집)를 한계로 밝힌다')
def _scope(t):
    return bool(re.search(r'불완전|부분\s*수집|scope_complete|전체\s*범위|확정하지|단정하지|미확인|확인되지', t))


def _tokens(text):
    """식별자·수치 후보. 영문 식별 토큰과 숫자를 모두 소문자로 모은다."""
    clean = re.sub(r'\d{4}-\d{2}-\d{2}', ' ', text)
    return set(x.lower() for x in re.findall(r'[A-Za-z][A-Za-z0-9._-]*[A-Za-z0-9]|[A-Za-z]', clean)), set(re.findall(r'(?<![A-Za-z0-9_.-])\d+(?:\.\d+)?(?![A-Za-z0-9_-])', clean))


_SOURCE = {'text': ''}


def set_source(text: str):
    """채점 기준이 되는 입력 원문. 응답이 말한 수치·식별자가 이 안에 있는지로 만들어낸 값을 판정한다."""
    _SOURCE['text'] = text.casefold()


@check('no_invented_number', '응답의 수치가 입력 원문에 모두 있다(만들어낸 수치 없음)')
def _nonumbers(t):
    _, nums = _tokens(t)
    return all(n.casefold() in _SOURCE['text'] for n in nums)


@check('no_invented_resource', '응답의 리소스·기준 식별자(대문자-하이픈, 도메인)가 입력 원문에 모두 있다')
def _noresource(t):
    ids = set(re.findall(r'(?<![A-Za-z0-9_])[A-Za-z]{2,}(?:-[A-Za-z0-9]+)+(?![A-Za-z0-9_])', t)) | set(re.findall(r'\b[a-z0-9-]+(?:\.[a-z0-9-]+)+\.(?:com|net|io|kr)\b', t))
    return all(i.casefold() in _SOURCE['text'] for i in ids)


@check('korean_only_prose', '일본어·한자 혼입이 없다')
def _lang(t):
    return not re.search(r'[\u3040-\u30ff\u4e00-\u9fff]', t)


def grade(text: str) -> dict:
    results = []
    for name, why, fn in CHECKS:
        try:
            ok = bool(fn(text))
        except Exception as exc:  # 채점 오류는 통과로 처리하지 않는다
            ok = False
            why = why + ' (채점 오류: ' + type(exc).__name__ + ')'
        results.append({'check': name, 'passed': ok, 'why': why})
    return {'passed': sum(r['passed'] for r in results), 'total': len(results), 'checks': results}


def extract_observation_text(model_output: str) -> str:
    """모델 출력(JSON)에서 관측 본문만 꺼낸다. 형식이 틀리면 빈 문자열을 돌려주고 채점에서 전부 실패한다."""
    try:
        data = json.loads(model_output)
    except ValueError:
        m = re.search(r'\{.*\}', model_output, re.S)
        if not m:
            return ''
        try:
            data = json.loads(m.group(0))
        except ValueError:
            return ''
    obs = data.get('observations') if isinstance(data, dict) else None
    if isinstance(obs, list) and obs and isinstance(obs[0], dict):
        return str(obs[0].get('text') or '')
    return str(data.get('text') or '') if isinstance(data, dict) else ''
