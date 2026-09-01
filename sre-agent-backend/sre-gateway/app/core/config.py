"""Gateway 数据库与模型 Provider 配置。"""

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parents[2] / ".env", override=False)


def _required_env(name: str) -> str:
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"{name} must be configured in sre-gateway/.env or the environment")
    return value


@dataclass(frozen=True, slots=True)
class MySQLSettings:
    host: str
    port: int
    user: str
    password: str
    database: str


def mysql_settings() -> MySQLSettings:
    """读取 Gateway MySQL 连接配置，密码不得在源码中提供默认值。"""
    return MySQLSettings(
        host=os.getenv("GATEWAY_MYSQL_HOST", "127.0.0.1"),
        port=int(os.getenv("GATEWAY_MYSQL_PORT", "13308")),
        user=os.getenv("GATEWAY_MYSQL_USER", "sre_agent"),
        password=_required_env("GATEWAY_MYSQL_PASSWORD"),
        database=os.getenv("GATEWAY_MYSQL_DATABASE", "sre_agent"),
    )


@dataclass(frozen=True, slots=True)
class ProviderSettings:
    """模型 Provider 的服务端密钥、Base URL 与统一超时配置。

    这里的密钥只用于后端访问模型厂商，与用户访问 Gateway 时提交的
    ``gw_sk_`` Client API Key 完全无关。
    """

    openai_api_key: str | None
    openai_base_url: str
    claude_api_key: str | None
    claude_base_url: str
    deepseek_api_key: str | None
    deepseek_base_url: str
    vllm_api_key: str | None
    vllm_base_url: str
    ollama_base_url: str
    timeout_seconds: float


def provider_settings() -> ProviderSettings:
    """从环境变量读取 Provider 配置，不在源码中保存任何厂商密钥。"""
    return ProviderSettings(
        openai_api_key=os.getenv("OPENAI_API_KEY"),
        openai_base_url=os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1"),
        claude_api_key=os.getenv("CLAUDE_API_KEY") or os.getenv("ANTHROPIC_API_KEY"),
        claude_base_url=os.getenv("CLAUDE_BASE_URL", "https://api.anthropic.com/v1"),
        deepseek_api_key=os.getenv("DEEPSEEK_API_KEY"),
        deepseek_base_url=os.getenv("DEEPSEEK_BASE_URL", "https://api.deepseek.com"),
        # EMPTY 与 vLLM 官方本地示例一致，只用于环回开发；正式部署应注入随机值。
        vllm_api_key=os.getenv("VLLM_API_KEY", "EMPTY"),
        vllm_base_url=os.getenv("VLLM_BASE_URL", "http://127.0.0.1:18000/v1"),
        ollama_base_url=os.getenv("OLLAMA_BASE_URL", "http://127.0.0.1:11434"),
        # Evidence Planner 的本地模型请求可能超过一分钟；仍以有限超时防止挂死。
        timeout_seconds=float(os.getenv("PROVIDER_TIMEOUT_SECONDS", "180")),
    )
