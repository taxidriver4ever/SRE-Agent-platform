"""Auth 模块的服务层、数据库与 HTTP 接口集成测试。

测试使用独立临时 SQLite 文件，覆盖明文不落库、模块化建表、Bearer 鉴权、
CORS、禁用和所有 401 失败分支，防止安全行为在重构时发生回退。
"""

import sqlite3

import pytest
from fastapi.testclient import TestClient

from app.auth.service import TOKEN_PATTERN, hash_token
from app.main import create_app


@pytest.fixture
def client(tmp_path):
    """为每个测试创建完全隔离的 FastAPI 客户端和临时数据库。"""
    database_path = tmp_path / "auth.db"
    app = create_app(database_path)
    with TestClient(app) as test_client:
        yield test_client, app.state.token_service, database_path


def test_generate_token_stores_only_hash(client):
    """服务生成的 Token 格式正确，并且 SQLite 文件不含明文。"""
    _, service, database_path = client
    generated = service.generate()

    assert TOKEN_PATTERN.fullmatch(generated.token)
    with sqlite3.connect(database_path) as connection:
        row = connection.execute(
            "SELECT token_hash FROM gateway_tokens"
        ).fetchone()

    assert row == (hash_token(generated.token),)
    assert len(row[0]) == 64
    assert generated.token not in database_path.read_bytes().decode("latin1")


def test_auth_module_creates_its_gateway_tokens_table(client):
    """应用启动时由 Auth 模块创建预期字段的 gateway_tokens 表。"""
    _, _, database_path = client

    with sqlite3.connect(database_path) as connection:
        columns = connection.execute(
            "PRAGMA table_info(gateway_tokens)"
        ).fetchall()

    assert [column[1] for column in columns] == [
        "id",
        "token_hash",
        "created_at",
        "disabled_at",
    ]


def test_generate_token_endpoint_returns_plaintext_once_and_stores_hash(client):
    """HTTP 生成接口返回一次明文，同时落库内容仍然只有 Hash。"""
    test_client, _, database_path = client

    response = test_client.post(
        "/v1/auth/tokens", headers={"Origin": "http://127.0.0.1:3000"}
    )

    assert response.status_code == 201
    assert response.headers["access-control-allow-origin"] == "http://127.0.0.1:3000"
    token = response.json()["token"]
    assert TOKEN_PATTERN.fullmatch(token)
    with sqlite3.connect(database_path) as connection:
        row = connection.execute(
            "SELECT token_hash FROM gateway_tokens"
        ).fetchone()
        operation = connection.execute(
            """
            SELECT operation, success, status_code, token_id
            FROM gateway_operation_logs
            ORDER BY id DESC
            LIMIT 1
            """
        ).fetchone()
    assert row == (hash_token(token),)
    assert operation == ("api_key.create", 1, 201, 1)
    assert token not in database_path.read_bytes().decode("latin1")


def test_valid_bearer_token_is_accepted(client):
    """有效 Bearer Token 能通过受保护接口鉴权。"""
    test_client, service, _ = client
    generated = service.generate()

    response = test_client.get(
        "/v1/auth/check",
        headers={"Authorization": f"Bearer {generated.token}"},
    )

    assert response.status_code == 200
    assert response.json() == {"authenticated": True, "token_id": 1}


def test_frontend_origin_is_allowed_by_cors(client):
    """Vue 本地开发地址能够跨域调用后端。"""
    test_client, _, _ = client

    response = test_client.get(
        "/health", headers={"Origin": "http://localhost:3000"}
    )

    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == "http://localhost:3000"


@pytest.mark.parametrize(
    "authorization",
    [
        None,
        "Basic abc",
        "Bearer",
        "Bearer bad-token",
        f"Bearer gw_sk_{'A' * 43}",
    ],
)
def test_invalid_credentials_return_401(client, authorization):
    """缺失、错误 Scheme、错误格式和未知 Token 都统一返回 401。"""
    test_client, _, _ = client
    headers = {"Authorization": authorization} if authorization else {}

    response = test_client.get("/v1/auth/check", headers=headers)

    assert response.status_code == 401
    assert response.headers["www-authenticate"] == "Bearer"
    assert response.json() == {"detail": "Unauthorized"}


def test_disabled_token_returns_401(client):
    """Token 禁用后服务校验和 HTTP 鉴权都必须立即失效。"""
    test_client, service, _ = client
    generated = service.generate()
    assert service.validate(generated.token) is not None
    assert service.disable(generated.token) is True
    assert service.validate(generated.token) is None

    response = test_client.get(
        "/v1/auth/check",
        headers={"Authorization": f"Bearer {generated.token}"},
    )

    assert response.status_code == 401
    assert response.json() == {"detail": "Unauthorized"}


def test_unknown_malformed_and_already_disabled_tokens_cannot_be_disabled(client):
    """禁用操作仅在第一次命中有效 Token 时返回成功。"""
    _, service, _ = client
    unknown = f"gw_sk_{'A' * 43}"
    generated = service.generate()

    assert service.disable("bad-token") is False
    assert service.disable(unknown) is False
    assert service.disable(generated.token) is True
    assert service.disable(generated.token) is False
