"""SDK가 구형 문맥 기본값 대신 현재 계정의 실제 모델 한도를 사용하는지 검증한다."""
from pathlib import Path
import asyncio
import sys
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parent))
import asis_runtime
from test_asis_runtime_budget_resend import Agent, Client, gate


class CatalogClient(Client):
    catalog_calls = 0

    async def list_models(self):
        type(self).catalog_calls += 1
        return [
            {"id": "gpt-6.1-sol", "capabilities": {"limits": {
                "max_prompt_tokens": 922000, "max_context_window_tokens": 1050000}}},
            {"id": "gpt-6-luna", "capabilities": {"limits": {
                "max_prompt_tokens": 872000, "max_context_window_tokens": 1000000}}},
        ]


class CapturingAgent(Agent):
    options_seen = []
    messages_seen = []

    def __init__(self, **kwargs):
        type(self).options_seen.append(kwargs["default_options"])
        super().__init__(**kwargs)

    async def run(self, message, *, session, options):
        type(self).messages_seen.append(message.text)
        return await super().run(message, session=session, options=options)


def test_live_catalog_limits_reach_new_and_reused_document_agents(tmp_path, monkeypatch):
    """같은 작성 객체·입력·강도는 유지하고 SDK에 계정이 광고한 한도만 전달한다."""
    monkeypatch.setenv("GITHUB_TOKEN", "synthetic-not-a-credential")
    CatalogClient.catalog_calls = 0
    CapturingAgent.options_seen = []
    CapturingAgent.messages_seen = []
    Agent.seen = []
    Agent.fail_plan = {}

    async def execute():
        async with asis_runtime.Runtime(client_factory=CatalogClient, agent_factory=CapturingAgent,
                                        admission=gate(), base_directory=tmp_path / "sessions") as runtime:
            first = await runtime.ask("author", '{"source":"original"}', session_id="catalog-author", instructions="fixed")
            second = await runtime.ask("author", '{"source":"repair"}', session_id="catalog-author", instructions="fixed")
            return first, second

    first, second = asyncio.run(execute())
    assert len(CapturingAgent.options_seen) == 1, "고정 작성 객체를 교체하면 안 된다"
    options = CapturingAgent.options_seen[0]
    override = options.get("model_capabilities")
    assert override is not None, "실시간 모델 한도가 SDK에 전달되지 않아 구형 128K 기본값을 사용할 수 있다"
    assert override.limits.max_prompt_tokens == 922000
    assert override.limits.max_context_window_tokens == 1050000
    assert options["model"] == "gpt-6.1-sol" and options["reasoning_effort"] == "xhigh"
    assert options["timeout"] == 1200 and options["available_tools"] == []
    assert CapturingAgent.messages_seen == ['{"source":"original"}', '{"source":"repair"}']
    assert CatalogClient.catalog_calls == 1, "동일 계정의 모델 목록을 호출마다 다시 읽으면 안 된다"
    assert first[1]["sdk_model_limits"] == second[1]["sdk_model_limits"] == {
        "max_prompt_tokens": 922000, "max_context_window_tokens": 1050000}
    assert second[1]["agent_reused"] is True


def test_invalid_catalog_limits_fail_before_request_without_setup_retry(tmp_path, monkeypatch):
    """광고 한도가 잘못되면 구형 기본값이나 무의미한 설정 재시도로 대체하지 않는다."""
    monkeypatch.setenv("GITHUB_TOKEN", "synthetic-not-a-credential")

    async def invalid_models(self):
        return [{"id": "gpt-6.1-sol", "capabilities": {"limits": {
            "max_prompt_tokens": None, "max_context_window_tokens": 1050000}}}]

    monkeypatch.setattr(CatalogClient, "list_models", invalid_models)
    CapturingAgent.options_seen = []

    async def execute():
        async with asis_runtime.Runtime(client_factory=CatalogClient, agent_factory=CapturingAgent,
                                        admission=gate(), base_directory=tmp_path / "sessions") as runtime:
            with pytest.raises(RuntimeError, match="model_catalog_limits_invalid"):
                await runtime.ask("author", "original", session_id="bad-catalog", instructions="fixed")
            assert getattr(runtime, "setup_retries", []) == [], "결정적 한도 오류를 설정 장애처럼 재시도하면 안 된다"

    asyncio.run(execute())
    assert CapturingAgent.options_seen == [], "검증되지 않은 모델 한도로 작성 요청을 보내면 안 된다"
