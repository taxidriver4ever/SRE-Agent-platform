"""Agent 服务配置。

配置统一由环境变量注入，源码和配置示例中不保存真实 Gateway Token。
使用不可变 dataclass 可以防止运行期间意外修改全局连接参数。
"""

import os
import tempfile
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv


# 只加载 Agent 项目根目录中的本地 .env；操作系统环境变量优先，不会被覆盖。
load_dotenv(Path(__file__).resolve().parents[2] / ".env", override=False)


def _required_env(name: str) -> str:
    """读取必填环境变量，拒绝缺失或纯空白配置。"""
    value = os.getenv(name, "").strip()
    if not value:
        raise RuntimeError(f"{name} must be configured in sre-agent/.env or the environment")
    return value


@dataclass(frozen=True, slots=True)
class Settings:
    """应用启动时需要的完整配置快照。

    Attributes:
        gateway_base_url: 相邻 ``sre-gateway`` 服务的根地址。
        gateway_api_key: 访问网关的 ``gw_sk_...`` Token，而非模型厂商密钥。
        gateway_model: 网关可识别的 ``provider/model`` 模型路由名。
        gateway_timeout_seconds: 单次网关 HTTP 请求的超时秒数。
        agent_max_iterations: 一次 Agent 运行允许的最大 LLM 推理轮数。
    """

    gateway_base_url: str
    gateway_api_key: str | None
    gateway_model: str
    gateway_timeout_seconds: float
    gateway_max_tokens: int
    agent_max_iterations: int
    # 以下配置均指向本机 Kind 实验平台；使用环境变量后也能连接到远端只读端点。
    kubernetes_namespace: str
    prometheus_base_url: str
    loki_base_url: str
    tempo_base_url: str
    mysql_host: str
    mysql_port: int
    mysql_user: str
    mysql_password: str
    mysql_database: str
    repository_path: str
    service_catalog_path: str
    repository_cache_path: str
    repository_allowed_hosts: tuple[str, ...]
    tool_timeout_seconds: float
    tool_output_limit: int
    model_context_window: int
    context_compaction_ratio: float
    context_reserved_output_tokens: int
    application_mysql_host: str
    application_mysql_port: int
    application_mysql_user: str
    application_mysql_password: str
    application_mysql_database: str
    auth_token_ttl_hours: int
    initial_username: str
    initial_password: str
    default_project_id: str
    tool_policy_path: str
    prometheus_bearer_token: str | None
    loki_bearer_token: str | None
    sandbox_workspace_root: str
    sandbox_image: str
    sandbox_cpus: float
    sandbox_memory_mb: int
    sandbox_pids_limit: int
    sandbox_timeout_seconds: float


def get_settings() -> Settings:
    """读取环境变量并生成独立配置对象。

    API Key 在真正调用模型时才校验。这样即便部署系统尚未注入密钥，服务也
    能启动并响应健康检查，运维侧可以获得明确的 503 配置错误。
    """
    return Settings(
        gateway_base_url=os.getenv("GATEWAY_BASE_URL", "http://127.0.0.1:8000").rstrip("/"),
        gateway_api_key=os.getenv("GATEWAY_API_KEY"),
        # 本项目默认走本地 Docker vLLM；仍可用环境变量切换到其他 Provider。
        gateway_model=os.getenv("GATEWAY_MODEL", "vllm/qwen3-4b"),
        # 本地模型首次加载通常比公网 API 慢，默认预留更宽松的超时时间。
        gateway_timeout_seconds=float(os.getenv("GATEWAY_TIMEOUT_SECONDS", "180")),
        # 诊断链路只生成结构化 JSON 和短摘要。限制输出预算能显著缩短本地
        # 推理时间，同时仍给证据综合保留足够空间。
        gateway_max_tokens=max(256, min(1200, int(os.getenv("GATEWAY_MAX_TOKENS", "512")))),
        agent_max_iterations=int(os.getenv("AGENT_MAX_ITERATIONS", "8")),
        kubernetes_namespace=os.getenv("KUBERNETES_NAMESPACE", "sre-lab"),
        prometheus_base_url=os.getenv("PROMETHEUS_BASE_URL", "http://127.0.0.1:19090").rstrip("/"),
        loki_base_url=os.getenv("LOKI_BASE_URL", "http://127.0.0.1:13100").rstrip("/"),
        tempo_base_url=os.getenv("TEMPO_BASE_URL", "http://127.0.0.1:13200").rstrip("/"),
        mysql_host=os.getenv("MYSQL_HOST", "127.0.0.1"),
        mysql_port=int(os.getenv("MYSQL_PORT", "13307")),
        mysql_user=os.getenv("MYSQL_USER", "sre_reader"),
        mysql_password=os.getenv("MYSQL_PASSWORD", "sre_reader_dev_only"),
        mysql_database=os.getenv("MYSQL_DATABASE", "sre_lab"),
        repository_path=os.getenv("SRE_REPOSITORY_PATH", r"D:\SRE-Agent-platform\sre-broken-system"),
        service_catalog_path=os.getenv(
            "SERVICE_CATALOG_PATH",
            r"D:\SRE-Agent-platform\sre-broken-system\sre-lab-infra\service-catalog.yaml",
        ),
        # 远程仓库只允许克隆到独立缓存目录，绝不覆盖用户现有工作树。
        repository_cache_path=os.getenv(
            "SRE_REPOSITORY_CACHE_PATH",
            r"D:\SRE-Agent-platform\.cache\sre-agent-repositories",
        ),
        # Kubernetes 注解属于外部输入。默认只允许公共代码托管主机，避免 SSRF。
        repository_allowed_hosts=tuple(
            host.strip() for host in os.getenv(
                "SRE_REPOSITORY_ALLOWED_HOSTS", "github.com,gitlab.com,bitbucket.org"
            ).split(",") if host.strip()
        ),
        # 所有外部诊断操作都有统一超时和输出上限，避免卡死或把海量日志灌入模型。
        tool_timeout_seconds=float(os.getenv("TOOL_TIMEOUT_SECONDS", "15")),
        tool_output_limit=int(os.getenv("TOOL_OUTPUT_LIMIT", "12000")),
        model_context_window=max(4096, int(os.getenv("MODEL_CONTEXT_WINDOW", "32768"))),
        context_compaction_ratio=min(
            0.95, max(0.50, float(os.getenv("CONTEXT_COMPACTION_RATIO", "0.80")))
        ),
        context_reserved_output_tokens=max(
            512, int(os.getenv("CONTEXT_RESERVED_OUTPUT_TOKENS", "4096"))
        ),
        application_mysql_host=os.getenv("APPLICATION_MYSQL_HOST", "127.0.0.1"),
        application_mysql_port=int(os.getenv("APPLICATION_MYSQL_PORT", "13308")),
        application_mysql_user=os.getenv("APPLICATION_MYSQL_USER", "sre_agent"),
        application_mysql_password=_required_env("APPLICATION_MYSQL_PASSWORD"),
        application_mysql_database=os.getenv("APPLICATION_MYSQL_DATABASE", "sre_agent"),
        auth_token_ttl_hours=int(os.getenv("AUTH_TOKEN_TTL_HOURS", "24")),
        # 登录凭据必须来自未提交的 .env 或进程环境，源码不再提供默认密码。
        initial_username=_required_env("SRE_INITIAL_USERNAME"),
        initial_password=_required_env("SRE_INITIAL_PASSWORD"),
        default_project_id=os.getenv("SRE_DEFAULT_PROJECT_ID", "sre-lab"),
        tool_policy_path=os.getenv(
            "SRE_TOOL_POLICY_PATH",
            str(Path(__file__).resolve().parents[2] / "config" / "tool-policy.yaml"),
        ),
        # 真实观测凭证仅由后端 HTTP Client 使用，不会进入 Tool Schema 或 LLM 上下文。
        prometheus_bearer_token=os.getenv("PROMETHEUS_BEARER_TOKEN") or None,
        loki_bearer_token=os.getenv("LOKI_BEARER_TOKEN") or None,
        sandbox_workspace_root=os.getenv(
            "SRE_SANDBOX_WORKSPACE_ROOT",
            str(Path(tempfile.gettempdir()) / "sre-agent-sandbox-tasks"),
        ),
        sandbox_image=os.getenv("SRE_SANDBOX_IMAGE", "python:3.12-alpine"),
        sandbox_cpus=float(os.getenv("SRE_SANDBOX_CPUS", "1.0")),
        sandbox_memory_mb=int(os.getenv("SRE_SANDBOX_MEMORY_MB", "512")),
        sandbox_pids_limit=int(os.getenv("SRE_SANDBOX_PIDS_LIMIT", "128")),
        sandbox_timeout_seconds=float(os.getenv("SRE_SANDBOX_TIMEOUT_SECONDS", "120")),
    )
