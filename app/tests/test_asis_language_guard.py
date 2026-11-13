"""작성 후보의 일본어·한자 혼입은 모델 검토 전에 코드가 잡고, 정상 한국어·원문 인용은 통과시킨다."""
from pathlib import Path
import asyncio, json, sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import asis_language_guard as guard
import asis_analysis
from asis_pipeline import Pipeline
from test_asis_connection import SyntheticRuntime, synthetic_input

# 운영 개정에서 독립 검토가 실제로 지적한 혼입 문장이다.
REAL_DEFECTS = [
    '한국어 관찰 문장의 관측된을 観측된으로 표기한 경우',
    '提供된 근거를 확인한다.',
    '설명 일부에 には 가 섞인 문장',
    '분석 범위에 コード 가 섞인 문장',
    '받아쓰기 기능을 受け쓰기 로 표기한 문장',
    '既존 환경을 설명하는 문장',
    '문장 구분자、가 섞인 문장',
]
CLEAN = [
    '네트워크 보안 그룹의 인바운드 규칙을 확인한다.',
    'Azure OpenAI 배포의 Content Filter 설정은 Microsoft Foundry에서 확인한다.',
    '수집값 1,234건 중 98.5%가 정상이다.',
    '고객 인터뷰의 “자동 승인” 표현을 근거로 한다.',
]


def test_every_real_defect_sentence_is_detected_with_position_and_quote():
    for text in REAL_DEFECTS:
        found = guard.find_language_defects({'items': [{'reason': text}]})
        assert found, text
        assert found[0]['where'] == '/items/0/reason' and found[0]['quote'].startswith(text[:10]) and found[0]['fix']


def test_clean_korean_english_numbers_and_quotes_have_no_false_positive():
    for text in CLEAN:
        assert guard.find_language_defects({'scope': text, 'reason': text}) == [], text


def test_quote_fields_ids_and_refs_are_not_checked_and_input_literals_are_allowed():
    candidate = {'quote': '既存 システム', 'id': 'I_既存', 'fact_refs': ['F_既'], 'reason': '고객이 말한 既存 표현을 그대로 인용한다.'}
    assert guard.find_language_defects(candidate) != []  # reason 본문의 가나·한자는 입력에 근거가 없으면 거부
    allowed = guard.allowed_text_from_input({'goals': [{'quote': '既存 システム 유지'}], 'facts': [], 'criteria': []})
    assert guard.find_language_defects(candidate, allowed_text=allowed) == []


def test_pipeline_returns_language_defects_to_same_author_before_review(tmp_path):
    state = {'injected': False, 'author_calls': 0, 'reviews_before_fix': 0}

    class Runtime(SyntheticRuntime):
        async def ask(self, role, message, **kwargs):
            payload = json.loads(message.rsplit('\n입력:\n', 1)[-1])
            if 'stage_data' not in payload:
                # 영역별 진단·과제 계획 호출(소형 입력)은 기본 합성 대역이 처리한다.
                return await super().ask(role, message, **kwargs)
            stage = payload['stage_data']
            value, receipt = await super().ask(role, message, **kwargs)
            if role in ('author', 'direction_author') and stage.get('mode') == 'synthesis' and not state['injected'] and not stage.get('accumulated_defects'):
                state['injected'] = True
                body = json.loads(value)
                body['directions'][0]['text'] = '提供된 근거에 따른 종합 판단이다.'
                return json.dumps(body, ensure_ascii=False), receipt
            if role in ('author', 'direction_author') and stage.get('mode') == 'synthesis':
                state['author_calls'] += 1
            if role == 'content_review' and state['injected'] and state['author_calls'] == 0:
                state['reviews_before_fix'] += 1
            return value, receipt

    result = asyncio.run(Pipeline(Runtime(), tmp_path / 'run').analyze(synthetic_input(tmp_path)))
    assert state['injected'], 'defect was not injected'
    text = json.dumps(result['stages'][1], ensure_ascii=False)
    assert '提供' not in text, 'language defect reached the final document'
