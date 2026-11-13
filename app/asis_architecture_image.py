"""개념 아키텍처 그림을 Foundry gpt-image로 만들어 장표에 붙일 PNG를 돌려준다(복잡한 도식 전용).

호출 함수는 arch_job의 검증된 호출(429 대기·재시도·관리 ID 토큰)을 그대로 쓴다. 이 모듈은 골격 -> 프롬프트 -> 이미지 -> 판독 -> 검사 -> 재시도만 맡는다.
이미지는 장표에서 IMAGE_MIN_WIDTH_IN 이상 폭으로 놓아야 하며, 그림 속 글자는 판독 검사가 확인한다. 실패하면 근거 없이 넘어가지 않고 예외로 실패한다.
"""
import hashlib
import json

import asis_overview_architecture as A

MAX_ROUNDS = 3


def read_prompt(skeleton):
    want = [{'id': n['id'], 'label': n['label']} for n in skeleton['nodes']]
    return ('Read this architecture overview diagram. For each expected block below, decide whether it is drawn with that Korean label. Return JSON '
            '{"nodes":[{"id":"...","found":true|false,"seen_label":"text you read"}],"extras":[{"label":"text"}]}. '
            "'extras' = every additional block or service whose label is not in the expected list (ignore the title). Expected blocks: " + json.dumps(want, ensure_ascii=False))


def make_image(skeleton, draw_image, ask_json, refs=()):
    """개념 아키텍처 그림 1장. draw_image(prompt, refs)->PNG bytes, ask_json(prompt, png)->dict 는 arch_job의 호출 함수를 그대로 넘긴다.

    검사가 실패하면 실패 항목만 고치도록 지시해 다시 그린다(최대 MAX_ROUNDS). 모두 실패하면 마지막 검사 결과와 함께 예외를 낸다."""
    fix = None; previous = None; history = []
    for attempt in range(1, MAX_ROUNDS + 1):
        png = draw_image(A.image_prompt(skeleton, fix), list(refs) + ([('previous.png', previous, 'image/png')] if previous else []))
        reading = ask_json(read_prompt(skeleton), png)
        fails = A.check_image_reading(skeleton, reading)
        history.append({'attempt': attempt, 'fails': [f['code'] for f in fails], 'sha256': hashlib.sha256(png).hexdigest()})
        if not fails:
            return {'png': png, 'attempts': attempt, 'history': history, 'model': A.IMAGE_MODEL}
        fix = ' '.join(f['fix'] for f in fails); previous = png
    raise RuntimeError('architecture_image_failed:' + json.dumps(history, ensure_ascii=False))
