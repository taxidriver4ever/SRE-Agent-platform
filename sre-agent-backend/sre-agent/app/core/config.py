"""Agent 服务配置。

配置统一由环境变量注入，源码和配置示例中不保存真实 Gateway Token。
使用不可变 dataclass 可以防止运行期间意外修改全局连接参数。
"""

import os
from dataclasses import dataclass


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
    # Evidence 原文统一进入 MinIO；SQLite 只保存 evidence_id 到 oss_key 的映射。
    minio_endpoint: str
    minio_public_endpoint: str
    minio_access_key: str
    minio_secret_key: str
    minio_bucket: str
    minio_secure: bool
    minio_presign_expire_minutes: int
    upload_max_bytes: int
    large_text_threshold_bytes: int
    active_context_character_budget: int
    application_database_path: str
    auth_token_ttl_hours: int
    initial_username: str
    initial_password: str


def get_settings() -> Settings:
    """读取环境变量并生成独立配置对象。

    API Key 在真正调用模型时才校验。这样即便部署系统尚未注入密钥，服务也
    能启动并响应健康检查，运维侧可以获得明确的 503 配置错误。
    """
    return Settings(
        gateway_base_url=os.getenv("GATEWAY_BASE_URL", "http://127.0.0.1:8000").rstrip("/"),
        gateway_api_key=os.getenv("GATEWAY_API_KEY"),
        # 本项目默认走本地 Docker Ollama；仍可用环境变量切换到其他 Provider。
        gateway_model=os.getenv("GATEWAY_MODEL", "ollama/qwen3:4b"),
        # 本地模型首次加载通常比公网 API 慢，默认预留更宽松的超时时间。
        gateway_timeout_seconds=float(os.getenv("GATEWAY_TIMEOUT_SECONDS", "180")),
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
        # SDK 内部访问地址与返回浏览器的签名地址分开配置。Agent 在宿主机运行时
        # 两者通常相同；未来 Agent 进入 Docker 后可分别使用 minio:9000 与公网域名。
        minio_endpoint=os.getenv("MINIO_ENDPOINT", "127.0.0.1:19100"),
        minio_public_endpoint=os.getenv("MINIO_PUBLIC_ENDPOINT", "127.0.0.1:19100"),
        minio_access_key=os.getenv("MINIO_ACCESS_KEY", "sreagent"),
        minio_secret_key=os.getenv("MINIO_SECRET_KEY", "sreagent-dev-secret"),
        minio_bucket=os.getenv("MINIO_BUCKET", "sre-agent-evidence"),
        minio_secure=os.getenv("MINIO_SECURE", "false").strip().lower() in {"1", "true", "yes", "on"},
        minio_presign_expire_minutes=int(os.getenv("MINIO_PRESIGN_EXPIRE_MINUTES", "15")),
        # 默认单对象 50 MiB；浏览器直传避免大文件经过 FastAPI 内存和带宽。
        upload_max_bytes=int(os.getenv("UPLOAD_MAX_BYTES", str(50 * 1024 * 1024))),
        # 粘贴文本超过 12 KiB 时，前端将其转换成 .log 并按附件流程直传 MinIO。
        large_text_threshold_bytes=int(os.getenv("LARGE_TEXT_THRESHOLD_BYTES", str(12 * 1024))),
        active_context_character_budget=int(os.getenv("ACTIVE_CONTEXT_CHARACTER_BUDGET", "8000")),
        # Auth 与 Conversation 使用独立业务数据库，不与 Evidence Store 原文混表。
        application_database_path=os.getenv("APPLICATION_DATABASE_PATH", ".data/sre-agent.sqlite3"),
        auth_token_ttl_hours=int(os.getenv("AUTH_TOKEN_TTL_HOURS", "24")),
        # 本地实验环境提供开箱即用账号；部署到共享环境前必须通过环境变量修改。
        initial_username=os.getenv("SRE_INITIAL_USERNAME", "admin"),
        initial_password=os.getenv("SRE_INITIAL_PASSWORD", "admin123"),
    )
