"""Gateway 完整调用链、模型路由和 Usage/Logs 测试。"""

import sqlite3

from fastapi.testclient import TestClient

from app.gateway.model_router import ModelRouter
from app.gateway.protocol import ProtocolParser
from app.gateway.provider import BaseProviderAdapter, ProviderRequestError, ProviderResult
from app.gateway.repository import UsageLogRepository
from app.gateway.schema import ChatCompletionRequest
from app.gateway.service import GatewayService
from app.main import create_app


class FakeProviderAdapter(BaseProviderAdapter):
    """不访问网络的测试 Provider，用于验证 Gateway 编排逻辑。"""

    provider_name = "openai"

    async def complete(self, request, model):
        return ProviderResult(
            response_id="provider-response-1",
            model=model,
            content="测试回复",
            finish_reason="stop",
            prompt_tokens=12,
            completion_tokens=5,
        )


class FailingProviderAdapter(BaseProviderAdapter):
    """模拟厂商失败，用于验证失败日志和 502 响应。"""

    provider_name = "openai"

    async def complete(self, request, model):
        raise ProviderRequestError("provider unavailable", 500)


class RecordingOllamaAdapter(FakeProviderAdapter):
    """记录网关按请求数据传入的 Ollama 模型名。"""

    provider_name = "ollama"

    def __init__(self) -> None:
        self.models: list[str] = []

    async def complete(self, request, model):
        self.models.append(model)
        return await super().complete(request, model)


def test_model_router_supports_four_providers():
    """显式前缀和模型名称可以路由到四个目标厂商。"""
    router = ModelRouter()

    assert router.route("openai/gpt-4o-mini").provider == "openai"
    assert router.route("claude/claude-sonnet-4").provider == "claude"
    assert router.route("deepseek/deepseek-chat").provider == "deepseek"
    assert router.route("ollama/qwen3:8b").provider == "ollama"
    assert router.route("claude-sonnet-4").provider == "claude"
    assert router.route("deepseek-chat").provider == "deepseek"


def test_protocol_parser_returns_an_independent_normalized_request():
    """Parser 输出独立对象，Adapter 修改时不会污染客户端原始请求。"""
    original = ChatCompletionRequest(
        model="gpt-4o-mini",
        messages=[{"role": "user", "content": "你好"}],
    )

    parsed = ProtocolParser().parse(original)

    assert parsed == original
    assert parsed is not original
    assert parsed.messages[0] is not original.messages[0]


def test_gateway_endpoint_calls_provider_and_records_usage(tmp_path):
    """受鉴权保护的 Gateway 接口返回统一响应并记录成功 Usage。"""
    database_path = tmp_path / "gateway.db"
    app = create_app(database_path)
    with TestClient(app) as client:
        # 替换真实 Adapter，确保测试不依赖外部厂商或 API Key。
        real_service = app.state.gateway_service
        app.state.gateway_service = GatewayService(
            ProtocolParser(),
            ModelRouter(),
            {"openai": FakeProviderAdapter()},
            real_service.usage_repository,
            real_service.operation_repository,
        )
        token = client.post("/v1/auth/tokens").json()["token"]
        response = client.post(
            "/v1/gateway/chat/completions",
            headers={"Authorization": f"Bearer {token}"},
            json={
                "model": "openai/gpt-4o-mini",
                "messages": [{"role": "user", "content": "你好"}],
            },
        )

    assert response.status_code == 200
    body = response.json()
    assert body["provider"] == "openai"
    assert body["model"] == "gpt-4o-mini"
    assert body["choices"][0]["message"]["content"] == "测试回复"
    assert body["usage"] == {
        "prompt_tokens": 12,
        "completion_tokens": 5,
        "total_tokens": 17,
    }

    with sqlite3.connect(database_path) as connection:
        log = connection.execute(
            """
            SELECT provider, model, total_tokens, success, status_code
            FROM gateway_usage_logs
            """
        ).fetchone()
        operation = connection.execute(
            """
            SELECT operation, token_id, success, status_code
            FROM gateway_operation_logs
            WHERE operation = 'gateway.chat.completion'
            """
        ).fetchone()
    assert log == ("openai", "gpt-4o-mini", 17, 1, 200)
    assert operation == ("gateway.chat.completion", 1, 1, 200)


def test_gateway_selects_ollama_model_from_each_request(tmp_path):
    """Ollama 模型由每次请求的 model 数据决定，不绑定为 qwen3。"""
    app = create_app(tmp_path / "ollama-model-selection.db")
    adapter = RecordingOllamaAdapter()
    with TestClient(app) as client:
        real_service = app.state.gateway_service
        app.state.gateway_service = GatewayService(
            ProtocolParser(),
            ModelRouter(),
            {"ollama": adapter},
            real_service.usage_repository,
            real_service.operation_repository,
        )
        token = client.post("/v1/auth/tokens").json()["token"]
        headers = {"Authorization": f"Bearer {token}"}
        for model in ("llama3.2:3b", "deepseek-r1:7b"):
            response = client.post(
                "/v1/gateway/chat/completions",
                headers=headers,
                json={
                    "model": f"ollama/{model}",
                    "messages": [{"role": "user", "content": "你好"}],
                },
            )
            assert response.status_code == 200
            assert response.json()["model"] == model

    assert adapter.models == ["llama3.2:3b", "deepseek-r1:7b"]


def test_gateway_failure_returns_502_and_records_failure(tmp_path):
    """厂商失败时客户端收到 502，SQLite 同时保留失败指标。"""
    database_path = tmp_path / "gateway-failure.db"
    app = create_app(database_path)
    with TestClient(app) as client:
        real_service = app.state.gateway_service
        app.state.gateway_service = GatewayService(
            ProtocolParser(),
            ModelRouter(),
            {"openai": FailingProviderAdapter()},
            real_service.usage_repository,
            real_service.operation_repository,
        )
        token = client.post("/v1/auth/tokens").json()["token"]
        response = client.post(
            "/v1/gateway/chat/completions",
            headers={"Authorization": f"Bearer {token}"},
            json={
                "model": "gpt-4o-mini",
                "messages": [{"role": "user", "content": "你好"}],
            },
        )

    assert response.status_code == 502
    with sqlite3.connect(database_path) as connection:
        log = connection.execute(
            "SELECT success, status_code, error_message FROM gateway_usage_logs"
        ).fetchone()
    assert log == (0, 500, "provider unavailable")


def test_gateway_requires_valid_api_token(tmp_path):
    """Gateway 模型调用接口必须复用 Auth 模块的 Bearer Token 鉴权。"""
    app = create_app(tmp_path / "unauthorized.db")
    with TestClient(app) as client:
        response = client.post(
            "/v1/gateway/chat/completions",
            json={
                "model": "gpt-4o-mini",
                "messages": [{"role": "user", "content": "你好"}],
            },
        )

    assert response.status_code == 401
    assert response.json() == {"detail": "Unauthorized"}
