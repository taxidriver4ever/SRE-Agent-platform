"""首次 Code State 扫描、Git Diff 增量更新与固定表查询测试。"""

from __future__ import annotations

import asyncio
import subprocess
from pathlib import Path

from fastmcp import Client

from app.code_state import CodeStateRepository, CodeStateService, register_code_state_tools
from app.mcp_servers.git.tools import GitReadBackend
from fastmcp import FastMCP
from tests.mysql_support import mysql_test_database


class LocalRegistry:
    def __init__(self, path: Path) -> None:
        self.path = path

    async def resolve(self, service: str, commit: str | None = None) -> Path:
        assert service == "order-service"
        return self.path

    def remote_url(self, service: str | None) -> str | None:
        return "https://github.com/example/order-service.git"


def git(repository: Path, *arguments: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repository), *arguments],
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def commit(repository: Path, message: str) -> str:
    git(repository, "add", "-A")
    git(repository, "commit", "-m", message)
    return git(repository, "rev-parse", "HEAD")


def build_repository(path: Path) -> str:
    path.mkdir()
    git(path, "init")
    git(path, "config", "user.email", "test@example.com")
    git(path, "config", "user.name", "Code State Test")
    (path / "pom.xml").write_text("<project><artifactId>order-service</artifactId></project>", encoding="utf-8")
    source = path / "src" / "main" / "java" / "example"
    source.mkdir(parents=True)
    (source / "OrderController.java").write_text(
        "public class OrderController {\n"
        "  public void createOrder() { new OrderService().createOrder(); }\n"
        "}\n",
        encoding="utf-8",
    )
    (source / "OrderService.java").write_text(
        "public class OrderService {\n"
        "  public void createOrder() { }\n"
        "}\n",
        encoding="utf-8",
    )
    resources = path / "src" / "main" / "resources"
    resources.mkdir(parents=True)
    (resources / "application.yml").write_text("server:\n  port: 8080\n", encoding="utf-8")
    return commit(path, "initial")


def test_initial_scan_creates_navigation_only_code_state(tmp_path: Path):
    repository_path = tmp_path / "repo"
    commit_sha = build_repository(repository_path)
    state_repository = CodeStateRepository(mysql_test_database())
    service = CodeStateService(state_repository, LocalRegistry(repository_path), llm=None)

    result = asyncio.run(service.ensure("order-service", commit_sha))
    components = state_repository.search("order-service", "Order", None, 20)

    assert result == "initialized"
    assert {item["kind"] for item in components}.issuperset({"controller", "service"})
    assert all(item["commit_sha"] == commit_sha for item in components)
    assert all("content" not in item for item in components)
    assert any(item["symbol"] == "OrderController#createOrder" for item in components)
    assert any(item["path"].endswith("OrderService.java") for item in components)


def test_new_commit_updates_only_changed_deleted_and_renamed_paths(tmp_path: Path):
    repository_path = tmp_path / "repo"
    old_commit = build_repository(repository_path)
    database = mysql_test_database()
    state_repository = CodeStateRepository(database)
    service = CodeStateService(state_repository, LocalRegistry(repository_path), llm=None)
    asyncio.run(service.ensure("order-service", old_commit))

    source = repository_path / "src" / "main" / "java" / "example"
    (source / "OrderController.java").rename(source / "OrderApiController.java")
    (source / "OrderApiController.java").write_text(
        "public class OrderApiController {\n  public void createOrder() { }\n}\n",
        encoding="utf-8",
    )
    (source / "OrderService.java").write_text(
        "public class OrderService {\n  public void createOrder() { }\n  public void cancelOrder() { }\n}\n",
        encoding="utf-8",
    )
    (source / "PaymentClient.java").write_text(
        "public class PaymentClient {\n  public void charge() { }\n}\n",
        encoding="utf-8",
    )
    (repository_path / "src" / "main" / "resources" / "application.yml").unlink()
    new_commit = commit(repository_path, "incremental")

    result = asyncio.run(service.ensure("order-service", new_commit))
    paths = state_repository.paths_for_repository("order-service")
    service_items = state_repository.search("order-service", "cancelOrder", None, 20)
    all_items = state_repository.search("order-service", "", None, 20)

    assert result == "updated"
    assert not any(path.endswith("OrderController.java") for path in paths)
    assert not any(path.endswith("application.yml") for path in paths)
    assert any(path.endswith("OrderApiController.java") for path in paths)
    assert any(path.endswith("PaymentClient.java") for path in paths)
    assert service_items[0]["commit_sha"] == new_commit
    assert all(item["commit_sha"] == new_commit for item in all_items)


def test_git_reference_reads_only_requested_symbol_lines(tmp_path: Path):
    repository_path = tmp_path / "repo"
    commit_sha = build_repository(repository_path)
    backend = GitReadBackend(
        "read_file_at_commit",
        str(tmp_path),
        timeout=5,
        output_limit=2000,
        repositories={"order-service": repository_path},
    )

    result = asyncio.run(backend.execute({
        "repository": "order-service",
        "commit": commit_sha,
        "path": "src/main/java/example/OrderService.java",
        "start_line": 2,
        "end_line": 2,
    }))

    assert result["data"]["start_line"] == 2
    assert result["data"]["end_line"] == 2
    assert result["data"]["output"].strip() == "public void createOrder() { }"


def test_code_state_tool_exposes_navigation_parameters_only(tmp_path: Path):
    async def schema() -> dict:
        server = FastMCP("code-state-test")
        register_code_state_tools(
            server,
            CodeStateRepository(mysql_test_database()),
        )
        async with Client(server) as client:
            tools = await client.list_tools()
        return tools[0].inputSchema

    properties = set(asyncio.run(schema())["properties"])
    assert properties == {"repository_name", "query", "kinds", "limit"}
    assert properties.isdisjoint({"sql", "table", "path", "commit_sha"})


def test_go_key_directories_and_symbols_are_navigation_entries():
    assert CodeStateService._is_entry("internal/handler/http.go")
    assert CodeStateService._is_entry("internal/service/notification.go")
    assert CodeStateService._is_entry("internal/repository/memory.go")
    assert not CodeStateService._is_entry("internal/domain/order.go")

    symbols = CodeStateService._symbols(
        "internal/handler/http.go",
        "package handler\nfunc (h *Handler) ServeHTTP(w Writer, r Request) {}\nfunc Health() {}\n",
    )
    assert [item[0] for item in symbols] == ["http#ServeHTTP", "http#Health"]
