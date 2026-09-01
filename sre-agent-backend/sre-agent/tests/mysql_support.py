"""测试使用的独立 MySQL 数据库初始化与隔离辅助。"""

import os

from app.auth.schema import initialize_auth_schema
from app.code_state.schema import initialize_code_state_schema
from app.conversation.schema import initialize_conversation_schema
from app.conversation_memory.schema import initialize_conversation_memory_schema
from app.core.config import get_settings
from app.core.database import ApplicationDatabase
from app.diagnosis.schema import initialize_diagnosis_schema


def mysql_test_database(*, reset: bool = True) -> ApplicationDatabase:
    settings = get_settings()
    test_database = os.getenv("APPLICATION_MYSQL_TEST_DATABASE", "sre_agent_test").strip()
    if not test_database.endswith("_test"):
        raise RuntimeError("tests require a dedicated APPLICATION_MYSQL_TEST_DATABASE ending in _test")
    database = ApplicationDatabase(
        settings.application_mysql_host,
        settings.application_mysql_port,
        settings.application_mysql_user,
        settings.application_mysql_password,
        test_database,
    )
    initialize_auth_schema(database)
    initialize_conversation_schema(database)
    initialize_conversation_memory_schema(database)
    initialize_code_state_schema(database)
    initialize_diagnosis_schema(database)
    if reset:
        reset_mysql_test_database(database)
    return database


def reset_mysql_test_database(database: ApplicationDatabase) -> None:
    """只清空名称以 _test 结尾的隔离测试库。"""
    if not database.database.endswith("_test"):
        raise RuntimeError("refusing to clear a non-test database")
    with database.connect() as connection:
        connection.execute("SET FOREIGN_KEY_CHECKS = 0")
        for table in (
            "diagnosis_events", "diagnosis_root_causes", "diagnosis_graph_edges",
            "diagnosis_graph_nodes", "diagnosis_evidence", "diagnosis_investigation_steps",
            "diagnosis_sessions",
            "conversation_memory_items", "conversation_compactions",
            "conversation_messages", "conversations", "auth_tokens", "users",
            "code_state_components", "code_state_repositories",
        ):
            connection.execute(f"DELETE FROM {table}")
        connection.execute("SET FOREIGN_KEY_CHECKS = 1")
        connection.commit()
