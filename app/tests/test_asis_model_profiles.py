"""별도 워커의 명시 모델 프로필만 바꾸고 운영 기본과 역할 격리를 보존한다."""
from pathlib import Path
import json
import os
import subprocess
import sys

APP = Path(__file__).resolve().parents[1]


def read_policy(profile=None):
    env = dict(os.environ, PYTHONPATH=str(APP))
    env.pop('AICAF_ASIS_MODEL_PROFILE', None)
    if profile is not None:
        env['AICAF_ASIS_MODEL_PROFILE'] = profile
    result = subprocess.run([sys.executable, '-B', '-c', 'from asis_runtime import policy; import json; print(json.dumps(policy()))'],
                            cwd=APP, env=env, capture_output=True, text=True, encoding='utf-8')
    return result


def test_explicit_argon_profile_uses_the_same_verified_model_and_high_for_every_role():
    result = read_policy('gemini-4-argon')
    assert result.returncode == 0, result.stderr
    policy = json.loads(result.stdout)
    assert set(policy['models'].values()) == {'gemini-4-argon'}
    assert set(policy['reasoning_by_role'].values()) == {'high'}
    assert policy['reasoning_effort'] == 'high' and policy['fallback_allowed'] is False
    assert policy['transport'] == 'chat-completions'


def test_missing_profile_uses_default_argon_and_legacy_profile_preserves_gpt_policy():
    result = read_policy()
    assert result.returncode == 0
    policy = json.loads(result.stdout)
    assert set(policy['models'].values()) == {'gemini-4-argon'}
    assert policy['transport'] == 'chat-completions'
    legacy = read_policy('legacy-gpt')
    assert legacy.returncode == 0
    legacy_policy = json.loads(legacy.stdout)
    assert legacy_policy['models']['author'] == 'gpt-6.1-sol'
    assert legacy_policy['models']['direction_author'] == 'gpt-6-luna'
    assert legacy_policy['reasoning_by_role']['content_review'] == 'xhigh'
    assert 'transport' not in legacy_policy


def test_unknown_profile_is_rejected_instead_of_falling_back():
    result = read_policy('unsupported-model-profile')
    assert result.returncode != 0 and 'invalid_asis_model_profile' in result.stderr
