"""공급자가 큰 입력을 잘라 받은 호출(운영 synthesis-shard-0144: 2.79MB 전송, 19,594토큰 수신)은 신뢰하지 않고 새 세션으로 한 번 다시 보낸다."""
from pathlib import Path
import asyncio, sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import asis_runtime
from asis_runtime import delivery_truncated, Runtime


def receipt(size, tokens):
    return {'input_utf8_bytes': size, 'usage': {'input_token_count': tokens}}


def test_only_a_large_input_with_a_far_too_small_token_count_counts_as_truncated():
    assert delivery_truncated(receipt(2_789_711, 19_594)) is True, 'the real production case'
    assert delivery_truncated(receipt(2_786_436, 763_289)) is False, 'the same shard first turn was normal (3.65 bytes/token)'
    assert delivery_truncated(receipt(2_826_698, 773_723)) is False
    assert delivery_truncated(receipt(50_000, 100)) is False, 'small inputs are never judged'
    assert delivery_truncated(receipt(2_000_000, None)) is False, 'no usage means no evidence'
    assert delivery_truncated(receipt(2_000_000, 0)) is False
    assert delivery_truncated({'input_utf8_bytes': 2_000_000}) is False
    assert delivery_truncated(None) is False


class Scripted(Runtime):
    def __init__(self, replies):
        super().__init__(client=object())
        self.replies, self.sessions = list(replies), []
        self._entered = True

    async def _ask(self, role, message, *, session_id, instructions='', image=None):
        self.sessions.append(session_id)
        return self.replies.pop(0)


def test_a_truncated_delivery_is_discarded_and_resent_once_on_a_fresh_session():
    runtime = Scripted([('{"verdict":"blocked"}', receipt(2_789_711, 19_594)), ('{"verdict":"pass"}', receipt(2_789_711, 760_000))])
    text, got = asyncio.run(runtime.ask('content_review', 'x', session_id='asis-review-abc'))
    assert text == '{"verdict":"pass"}', 'the answer to the truncated delivery must not be used'
    assert runtime.sessions == ['asis-review-abc', 'asis-review-abc-resend1']
    assert runtime.resent_after_truncated_delivery[0]['reason'] == 'input_truncated_in_delivery'
    assert runtime.resent_after_truncated_delivery[0]['input_tokens'] == 19_594


def test_a_second_truncation_is_not_resent_again_and_a_normal_call_is_untouched():
    runtime = Scripted([('a', receipt(2_789_711, 19_594)), ('b', receipt(2_789_711, 19_000))])
    text, _ = asyncio.run(runtime.ask('content_review', 'x', session_id='s1'))
    assert text == 'b' and runtime.sessions == ['s1', 's1-resend1'], 'at most one resend'
    normal = Scripted([('ok', receipt(2_786_436, 763_289))])
    assert asyncio.run(normal.ask('content_review', 'x', session_id='s2'))[0] == 'ok'
    assert normal.sessions == ['s2'] and not hasattr(normal, 'resent_after_truncated_delivery')
