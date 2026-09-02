"""FastAPI 应用组装入口。

该模块只负责读取配置、组装依赖、管理资源生命周期和注册路由。Agent 推理、
网关协议和工具逻辑分别保留在各自模块中，避免入口文件演变成业务逻辑集合。
"""

import asyncio
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastmcp.utilities.lifespan import combine_lifespans

from app.agent import ToolAgent
from app.audit import ToolAuditRepository, initialize_audit_schema
from app.api.router import chat_router, router as agent_router
from app.auth import AuthService, auth_router, initialize_auth_schema
from app.conversation import ConversationService, conversation_router, initialize_conversation_schema
from app.conversation_memory import (
    ConversationCompactionService, ConversationMemoryRepository,
    initialize_conversation_memory_schema,
)
from app.code_state import CodeStateRepository, CodeStateService, initialize_code_state_schema
from app.core.config import get_settings
from app.core.database import ApplicationDatabase
from app.diagnosis import (
    DiagnosisOrchestrator, DiagnosisRepository, DiagnosisService,
    initialize_diagnosis_schema,
)
from app.diagnosis.router import router as diagnosis_router
from app.llm import GatewayLLM
from app.intent import IntentRouter, IntentWorkflowRouter
from app.security import ToolPolicy
from app.sandbox import DockerSandboxManager
from app.mcp_clients import FastMCPToolClient, KubernetesMCPAdapter
from app.mcp_servers import build_fastmcp_server
from app.repositories import RepositoryRegistry
from app.resources import router as resources_router
from app.workflow import DiagnosisWorkflow


def create_app() -> FastAPI:
    """创建一个依赖完整且可独立启动的 FastAPI 应用。

    Returns:
        注册了健康检查和 Agent v1 路由的应用实例。

    工厂函数让测试可以为每个用例创建隔离应用，也为未来按环境注入不同工具集
    留出扩展点。
    """
    # 配置在应用创建时形成快照，同一进程内的一次应用生命周期保持一致。
    settings = get_settings()

    # Auth 与 Conversation 共享一份 MySQL 业务库；各模块拥有自己的建表 SQL。
    # AuthService，ConversationService 只接收已经验证过的 user_id。
    application_database = ApplicationDatabase(
        settings.application_mysql_host,
        settings.application_mysql_port,
        settings.application_mysql_user,
        settings.application_mysql_password,
        settings.application_mysql_database,
    )
    initialize_auth_schema(application_database)
    initialize_conversation_schema(application_database)
    initialize_conversation_memory_schema(application_database)
    initialize_code_state_schema(application_database)
    initialize_audit_schema(application_database)
    # Diagnosis 表依赖 users 与 conversations，必须在这两个模块之后初始化。
    initialize_diagnosis_schema(application_database)
    auth_service = AuthService(application_database, settings.auth_token_ttl_hours)
    auth_service.ensure_user(settings.initial_username, settings.initial_password)
    conversation_service = ConversationService(application_database)
    memory_repository = ConversationMemoryRepository(application_database)
    code_state_repository = CodeStateRepository(application_database)

    # GatewayLLM 是唯一连接外部模型能力的组件。Agent 不直接访问厂商 API。
    llm = GatewayLLM(
        base_url=settings.gateway_base_url,
        api_key=settings.gateway_api_key,
        model=settings.gateway_model,
        timeout=settings.gateway_timeout_seconds,
        max_tokens=settings.gateway_max_tokens,
    )
    # FastMCP 负责工具注册、Schema、参数校验和标准 MCP 调用；Agent 只持有官方
    # in-memory Client 的薄适配器，没有自研 MCP 注册中心或协议实现。
    repository_registry = RepositoryRegistry(
        repository_root=settings.repository_path,
        catalog_path=settings.service_catalog_path,
        cache_path=settings.repository_cache_path,
        allowed_hosts=settings.repository_allowed_hosts,
        timeout=settings.tool_timeout_seconds,
    )
    tool_policy = ToolPolicy(settings.tool_policy_path, repository_registry.local_paths)
    audit_repository = ToolAuditRepository(application_database)
    sandbox_manager = DockerSandboxManager(
        settings.sandbox_workspace_root,
        image=settings.sandbox_image,
        cpus=settings.sandbox_cpus,
        memory_mb=settings.sandbox_memory_mb,
        pids_limit=settings.sandbox_pids_limit,
        timeout_seconds=settings.sandbox_timeout_seconds,
    )
    code_state_service = CodeStateService(
        code_state_repository,
        repository_registry,
        llm,
        timeout=settings.tool_timeout_seconds,
    )
    mcp_server = build_fastmcp_server(
        settings,
        repository_registry,
        memory_repository,
        code_state_repository,
    )
    # 生成标准 Streamable HTTP ASGI 应用；path="/" 是因为下方会挂载到 /mcp。
    mcp_app = mcp_server.http_app(path="/")
    # Kubernetes 不再注册到项目自有 Server，而是由维护活跃的第三方 MCP
    # 以 read-only/core/single-context 模式直接访问 Kubernetes API。
    kubernetes_mcp = KubernetesMCPAdapter(settings.kubernetes_namespace)
    tools = FastMCPToolClient(
        mcp_server,
        kubernetes_mcp,
        policy=tool_policy,
        audit_repository=audit_repository,
        default_project_id=settings.default_project_id,
    )
    context_service = ConversationCompactionService(
        memory_repository,
        llm,
        model_context_window=settings.model_context_window,
        compaction_ratio=settings.context_compaction_ratio,
        reserved_output_tokens=settings.context_reserved_output_tokens,
    )
    diagnosis_workflow = DiagnosisWorkflow(
        tools=tools,
        catalog_path=settings.service_catalog_path,
        max_steps=max(8, min(settings.agent_max_iterations + 4, 12)),
        llm=llm,
        repository_registry=repository_registry,
        context_service=context_service,
        conversation_service=conversation_service,
        code_state_service=code_state_service,
        kubernetes_namespace=settings.kubernetes_namespace,
        deadline_seconds=settings.diagnosis_deadline_seconds,
    )
    diagnosis_repository = DiagnosisRepository(application_database)
    diagnosis_service = DiagnosisService(diagnosis_repository, conversation_service)
    diagnosis_orchestrator = DiagnosisOrchestrator(
        diagnosis_workflow, diagnosis_service, diagnosis_repository,
    )
    service_aliases = {
        str(alias): service_name
        for service_name, metadata in diagnosis_workflow.catalog.services.items()
        for alias in metadata.get("aliases", [])
    }
    intent_router = IntentRouter(
        llm,
        service_names=list(diagnosis_workflow.catalog.services),
        service_aliases=service_aliases,
    )
    intent_workflow_router = IntentWorkflowRouter(
        intent_router,
        diagnosis_workflow,
        conversation_service,
    )

    @asynccontextmanager
    async def lifespan(application: FastAPI):
        """在服务启动时发布共享依赖，在关闭时释放 HTTP 连接池。"""
        # state 保存进程内共享对象，避免每个请求重复建立 httpx 连接池。
        application.state.llm = llm
        application.state.mcp_server = mcp_server
        application.state.tools = tools
        application.state.agent = ToolAgent(llm, tools, settings.agent_max_iterations)
        application.state.diagnosis_workflow = diagnosis_workflow
        application.state.diagnosis_repository = diagnosis_repository
        application.state.diagnosis_service = diagnosis_service
        application.state.diagnosis_orchestrator = diagnosis_orchestrator
        application.state.diagnosis_tasks = set()
        application.state.intent_router = intent_router
        application.state.intent_workflow_router = intent_workflow_router
        application.state.memory_repository = memory_repository
        application.state.code_state_repository = code_state_repository
        application.state.code_state_service = code_state_service
        application.state.context_service = context_service
        application.state.tool_policy = tool_policy
        application.state.audit_repository = audit_repository
        application.state.sandbox_manager = sandbox_manager
        application.state.default_project_id = settings.default_project_id
        application.state.auth_service = auth_service
        application.state.conversation_service = conversation_service
        yield
        # 先取消仍在运行的 Diagnosis Session，再关闭共享 MCP/LLM 资源。
        # CancelledError 会由 Diagnosis Router 将会话持久化为 CANCELLED。
        pending_diagnoses = list(application.state.diagnosis_tasks)
        for task in pending_diagnoses:
            task.cancel()
        if pending_diagnoses:
            await asyncio.gather(*pending_diagnoses, return_exceptions=True)
        # 只有 GatewayLLM 自己创建的客户端会被关闭，注入客户端的所有权规则
        # 由 GatewayLLM.close() 内部负责判断。
        await tools.close()
        await llm.close()

    # FastMCP 的 session manager 必须进入自己的 lifespan。官方 combine_lifespans
    # 同时管理 Agent HTTP 资源与 MCP transport，避免嵌套 ASGI lifespan 被忽略。
    application = FastAPI(
        title="SRE Tool Agent",
        version="0.2.0",
        lifespan=combine_lifespans(lifespan, mcp_app.lifespan),
    )
    # Vite 默认运行在 5173；同时保留 3000 供自定义前端启动参数使用。
    # 当前 API 不使用 Cookie，因此 allow_credentials 保持关闭。
    application.add_middleware(
        CORSMiddleware,
        allow_origins=[
            "http://127.0.0.1:5173", "http://localhost:5173",
            "http://127.0.0.1:3000", "http://localhost:3000",
            "http://127.0.0.1:3001", "http://localhost:3001",
        ],
        allow_credentials=False,
        allow_methods=["GET", "POST", "OPTIONS"],
        allow_headers=["Content-Type", "Authorization"],
    )
    application.include_router(auth_router)
    application.include_router(conversation_router)
    application.include_router(agent_router)
    application.include_router(chat_router)
    application.include_router(diagnosis_router)
    application.include_router(resources_router)
    # 对外端点只暴露项目的 Git/可观测性只读工具。Kubernetes Server 保持独立，
    # 这样第三方版本、RBAC 和进程生命周期不会被伪装成项目自研工具。
    application.mount("/mcp", mcp_app)

    @application.get("/health", tags=["health"])
    async def health() -> dict[str, str]:
        """无需调用 LLM 的轻量存活检查。"""
        return {"status": "ok"}

    return application


# Uvicorn 使用 ``app.main:app`` 导入此模块级对象。
app = create_app()
