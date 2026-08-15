"""FastAPI 应用入口。

本文件只负责组装基础设施、模块路由和应用生命周期。业务规则位于对应模块，
因此根目录保持整洁，也便于以后继续增加其他独立模块。
"""

from contextlib import asynccontextmanager
from pathlib import Path
import sys

# PyCharm 直接执行本文件时，sys.path 默认只有 app 目录，无法解析 ``app.*``。
# 把网关项目根目录加入模块搜索路径，使“点击运行”和 ``python app/main.py``
# 都与 ``uvicorn app.main:app`` 使用相同的包结构。
if __package__ in {None, ""}:
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.auth.repository import TokenRepository, initialize_auth_tables
from app.auth.router import router as auth_router
from app.auth.service import TokenService
from app.core.config import (
    database_path as configured_database_path,
    provider_settings,
)
from app.core.database import Database
from app.gateway.model_router import ModelRouter
from app.gateway.protocol import ProtocolParser
from app.gateway.provider import (
    ClaudeAdapter,
    DeepSeekAdapter,
    OllamaAdapter,
    OpenAIAdapter,
)
from app.gateway.repository import UsageLogRepository, initialize_gateway_tables
from app.gateway.router import router as gateway_router
from app.gateway.service import GatewayService
from app.operation_log import OperationLogRepository, initialize_operation_log_tables


def create_app(database_path: str | Path | None = None) -> FastAPI:
    """创建并配置 FastAPI 应用。

    Args:
        database_path: 可选的 SQLite 路径。生产环境默认读取配置，测试可传入
            临时数据库，从而避免污染真实数据。

    Returns:
        已注册生命周期、CORS、Auth 路由和健康检查的 FastAPI 实例。
    """
    database = Database(database_path or configured_database_path())

    @asynccontextmanager
    async def lifespan(application: FastAPI):
        """管理应用启动和关闭阶段的数据库资源。"""
        # 启动时由每个模块分别创建自己拥有的表。
        initialize_auth_tables(database)
        initialize_operation_log_tables(database)
        initialize_gateway_tables(database)

        # Auth 依赖链：Database -> Repository -> Service。
        repository = TokenRepository(database)
        operation_repository = OperationLogRepository(database)
        application.state.operation_log_repository = operation_repository
        application.state.token_service = TokenService(repository, operation_repository)

        # Gateway 依赖链：Parser + Router + Adapters + Usage Repository。
        settings = provider_settings()
        providers = {
            "openai": OpenAIAdapter(
                settings.openai_api_key,
                settings.openai_base_url,
                settings.timeout_seconds,
            ),
            "claude": ClaudeAdapter(
                settings.claude_api_key,
                settings.claude_base_url,
                settings.timeout_seconds,
            ),
            "deepseek": DeepSeekAdapter(
                settings.deepseek_api_key,
                settings.deepseek_base_url,
                settings.timeout_seconds,
            ),
            "ollama": OllamaAdapter(
                settings.ollama_base_url,
                settings.timeout_seconds,
            ),
        }
        application.state.gateway_service = GatewayService(
            ProtocolParser(),
            ModelRouter(),
            providers,
            UsageLogRepository(database),
            operation_repository,
        )
        yield
        # 应用关闭时释放 SQLite 连接，避免测试或热重载残留文件句柄。
        database.dispose()

    application = FastAPI(title="SRE Agent Backend", lifespan=lifespan)
    # 前后端在本地使用不同端口，需要明确允许 Vue 开发服务器跨域访问。
    application.add_middleware(
        CORSMiddleware,
        allow_origins=["http://127.0.0.1:3000", "http://localhost:3000"],
        allow_credentials=False,
        allow_methods=["GET", "POST"],
        allow_headers=["Authorization", "Content-Type"],
    )
    application.include_router(auth_router)
    application.include_router(gateway_router)

    @application.get("/health", tags=["health"])
    def health() -> dict[str, str]:
        """提供无需鉴权的轻量存活检查。"""
        return {"status": "ok"}

    return application


# Uvicorn 使用 ``app.main:app`` 导入该对象启动服务。
app = create_app()


if __name__ == "__main__":
    import uvicorn

    # 直接传入应用对象，避免脚本模式下再次导入本模块并创建第二套资源。
    uvicorn.run(app, host="127.0.0.1", port=8000)
