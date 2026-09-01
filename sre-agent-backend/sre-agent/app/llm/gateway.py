"""对接相邻 ``sre-gateway`` 服务的异步 LLM 客户端。

本模块负责 HTTP 协议转换和错误归一化；它只持有 Gateway API Key，不接触
OpenAI、Claude 等模型厂商密钥。厂商鉴权由网关服务独立管理。
"""

from typing import Any

import httpx

from app.llm.base import LLMMessage, LLMResponse


class GatewayError(Exception):
    """Gateway 客户端错误基类。"""


class GatewayConfigurationError(GatewayError):
    """Agent 缺少 Gateway 访问配置。"""


class GatewayRequestError(GatewayError):
    """Gateway 网络、HTTP 或响应协议异常。"""


class GatewayLLM:
    """通过统一 Gateway 调用任意已配置的模型 Provider。

    可注入 ``httpx.AsyncClient`` 以复用上层连接池或使用 MockTransport 测试。
    未注入时由本类创建并拥有客户端，应用关闭时必须调用 ``close``。
    """

    def __init__(
        self,
        base_url: str,
        api_key: str | None,
        model: str,
        timeout: float = 60,
        max_tokens: int = 512,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        """保存连接配置并准备异步 HTTP 客户端。

        Args:
            base_url: Gateway 根地址，例如 ``http://127.0.0.1:8000``。
            api_key: 用户侧 Gateway Token，格式通常为 ``gw_sk_...``。
            model: Gateway 的模型路由名，例如 ``openai/gpt-4o-mini``。
            timeout: 自建 HTTP 客户端的单次请求超时秒数。
            client: 可选外部客户端；传入后其生命周期由调用方管理。
        """
        # 统一移除尾部斜杠，避免拼接 API 路径时产生双斜杠。
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.model = model
        self.max_tokens = max(256, min(1200, max_tokens))
        # 记录所有权，防止 close() 误关掉由其他组件共享的外部连接池。
        self._owns_client = client is None
        self._client = client or httpx.AsyncClient(timeout=timeout)

    async def complete(self, messages: list[LLMMessage]) -> LLMResponse:
        """调用 Gateway 非流式 Chat Completions 接口。

        请求消息会转换成网关接受的字典格式；成功响应会归一化成领域层的
        ``LLMResponse``。网络、HTTP 状态码和响应结构问题统一抛出
        ``GatewayRequestError``，避免上层依赖 httpx 异常类型。
        """
        # 延迟校验让未配置密钥的实例仍可用于启动应用和响应健康检查。
        if not self.api_key:
            raise GatewayConfigurationError("GATEWAY_API_KEY is not configured")

        try:
            # Token 只放在 Authorization 请求头中，永不写入 payload 或错误文本。
            response = await self._client.post(
                f"{self.base_url}/v1/gateway/chat/completions",
                headers={"Authorization": f"Bearer {self.api_key}"},
                json={
                    "model": self.model,
                    # 网关目前只支持普通文本角色，LLMMessage 在此处完成边界转换。
                    "messages": [
                        {"role": message.role, "content": message.content}
                        for message in messages
                    ],
                    # 当前调用均依赖 JSON/短摘要；限制生成长度并降低随机性可减少
                    # 本地模型超时和 Structured Output 重试。
                    "temperature": 0,
                    "max_tokens": self.max_tokens,
                    # 当前网关明确拒绝流式请求，Agent 循环也按完整 JSON 响应解析。
                    "stream": False,
                },
            )
            # 将非 2xx 状态转换成 HTTPStatusError，交给下方分支保留状态码。
            response.raise_for_status()
            data: dict[str, Any] = response.json()

            # choices[0] 是网关当前非流式响应的唯一选择。缺失字段会被统一捕获
            # 并转成协议异常，不把 KeyError 等实现细节泄漏给 API 调用方。
            choice = data["choices"][0]
            usage = data.get("usage") or {}
            return LLMResponse(
                content=choice["message"]["content"],
                model=data.get("model", self.model),
                provider=data.get("provider"),
                prompt_tokens=int(usage.get("prompt_tokens", 0)),
                completion_tokens=int(usage.get("completion_tokens", 0)),
            )
        except httpx.HTTPStatusError as exc:
            # 仅提取响应正文中的 detail；绝不拼接包含 Authorization 的 Request。
            detail = _safe_error_detail(exc.response)
            raise GatewayRequestError(
                f"Gateway returned HTTP {exc.response.status_code}: {detail}"
            ) from exc
        except (httpx.HTTPError, KeyError, IndexError, TypeError, ValueError) as exc:
            # 同时覆盖网络错误、非法 JSON 和网关响应字段类型不正确等情况。
            raise GatewayRequestError("Gateway request or response is invalid") from exc

    async def close(self) -> None:
        """关闭本实例自行创建的连接池。

        注入的外部客户端不在本类释放，所有权仍归注入方。
        """
        if self._owns_client:
            await self._client.aclose()


def _safe_error_detail(response: httpx.Response) -> str:
    """从错误响应中安全提取简短描述。

    网关通常返回 ``{"detail": "..."}``。非 JSON 响应使用固定文案；服务端
    文本最多保留 500 字符，避免把超大上游响应继续传播到日志和客户端。
    """
    try:
        body = response.json()
        return str(body.get("detail", "request failed"))[:500]
    except ValueError:
        return "request failed"
