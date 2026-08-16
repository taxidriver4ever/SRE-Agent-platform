"""应用配置读取工具。

当前最小版本只有 SQLite 数据库路径配置。配置统一从环境变量读取，使本地、
测试和部署环境可以使用不同数据库，而不需要修改源码。
"""

import os
from dataclasses import dataclass
from pathlib import Path


def database_path() -> Path:
    """返回 SQLite 数据库的绝对路径。

    环境变量 ``GATEWAY_AUTH_DB`` 可覆盖默认值。转换成绝对路径可以避免从不同
    工作目录启动应用时，SQLite 意外创建到不同位置。

    Returns:
        SQLite 数据库文件的绝对路径；默认是项目下的 ``data/auth.db``。
    """
    return Path(os.getenv("GATEWAY_AUTH_DB", "data/auth.db")).resolve()


@dataclass(frozen=True, slots=True)
class ProviderSettings:
    """四个 Provider 的厂商密钥、Base URL 与统一超时配置。

    这里的密钥只用于后端访问模型厂商，与用户访问 Gateway 时提交的
    ``gw_sk_`` Client API Key 完全无关。
    """

    openai_api_key: str | None
    openai_base_url: str
    claude_api_key: str | None
    claude_base_url: str
    deepseek_api_key: str | None
    deepseek_base_url: str
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
        ollama_base_url=os.getenv("OLLAMA_BASE_URL", "http://127.0.0.1:11434"),
        # Evidence Planner 的本地模型请求可能超过一分钟；仍以有限超时防止挂死。
        timeout_seconds=float(os.getenv("PROVIDER_TIMEOUT_SECONDS", "180")),
    )
