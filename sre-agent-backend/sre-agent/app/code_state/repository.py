"""Code State 的 MySQL 持久化与固定表导航查询。"""

from __future__ import annotations

import json
from contextlib import closing
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from app.code_state.models import CodeComponent
from app.core.database import ApplicationDatabase


class CodeStateRepository:
    ALLOWED_KINDS = {"module", "manifest", "config", "controller", "service", "repository", "component"}

    def __init__(self, database: ApplicationDatabase) -> None:
        self.database = database

    def current_commit(self, repository: str) -> str | None:
        with closing(self.database.connect()) as connection:
            row = connection.execute(
                "SELECT commit_sha FROM code_state_repositories WHERE repository = ?",
                (repository,),
            ).fetchone()
        return str(row[0]) if row else None

    def replace_repository(
        self,
        repository: str,
        repository_url: str | None,
        commit_sha: str,
        directory_summary: dict[str, Any],
        components: list[CodeComponent],
    ) -> None:
        """首次扫描一次事务替换该仓库的导航状态。"""
        now = datetime.now(timezone.utc).isoformat()
        with closing(self.database.connect()) as connection:
            connection.execute("DELETE FROM code_state_components WHERE repository = ?", (repository,))
            self._upsert_components(connection, repository, commit_sha, components, now)
            connection.execute(
                """
                INSERT INTO code_state_repositories(
                    repository, repository_url, commit_sha, directory_summary_json, updated_at
                ) VALUES (?, ?, ?, ?, ?)
                ON DUPLICATE KEY UPDATE
                    repository_url = VALUES(repository_url),
                    commit_sha = VALUES(commit_sha),
                    directory_summary_json = VALUES(directory_summary_json),
                    updated_at = VALUES(updated_at)
                """,
                (repository, repository_url, commit_sha, json.dumps(directory_summary, ensure_ascii=False), now),
            )
            connection.commit()

    def apply_incremental(
        self,
        repository: str,
        repository_url: str | None,
        commit_sha: str,
        directory_summary: dict[str, Any],
        removed_paths: set[str],
        components: list[CodeComponent],
    ) -> None:
        """只删除受影响路径并写入重新分析的组件。"""
        now = datetime.now(timezone.utc).isoformat()
        updated_paths = removed_paths | {item.path for item in components}
        with closing(self.database.connect()) as connection:
            # 未变化文件在新提交中仍是同一内容，但 Reference 应统一锚定当前
            # 仓库版本，避免一次查询返回多个历史 commit。
            connection.execute(
                "UPDATE code_state_components SET commit_sha = ?, updated_at = ? WHERE repository = ?",
                (commit_sha, now, repository),
            )
            for path in updated_paths:
                connection.execute(
                    "DELETE FROM code_state_components WHERE repository = ? AND path = ?",
                    (repository, path),
                )
            self._upsert_components(connection, repository, commit_sha, components, now)
            connection.execute(
                """
                INSERT INTO code_state_repositories(
                    repository, repository_url, commit_sha, directory_summary_json, updated_at
                ) VALUES (?, ?, ?, ?, ?)
                ON DUPLICATE KEY UPDATE
                    repository_url = VALUES(repository_url),
                    commit_sha = VALUES(commit_sha),
                    directory_summary_json = VALUES(directory_summary_json),
                    updated_at = VALUES(updated_at)
                """,
                (repository, repository_url, commit_sha, json.dumps(directory_summary, ensure_ascii=False), now),
            )
            connection.commit()

    def search(
        self,
        repository: str,
        query: str,
        kinds: list[str] | None = None,
        limit: int = 12,
    ) -> list[dict[str, Any]]:
        """固定查询 code_state_components，不接受表名、字段或原始 SQL。"""
        selected = [kind for kind in (kinds or []) if kind in self.ALLOWED_KINDS]
        pattern = f"%{query[:200]}%"
        parameters: list[Any] = [repository, pattern, pattern, pattern, pattern]
        kind_clause = ""
        if selected:
            placeholders = ",".join("?" for _ in selected)
            kind_clause = f" AND kind IN ({placeholders})"
            parameters.extend(selected)
        parameters.append(max(1, min(limit, 20)))
        sql = f"""
            SELECT repository AS repo, commit_sha, module, kind, symbol, path, role,
                   relationships_json, start_line, end_line
            FROM code_state_components
            WHERE repository = ?
              AND (module LIKE ? OR symbol LIKE ? OR path LIKE ? OR role LIKE ?)
              {kind_clause}
            ORDER BY
              CASE kind WHEN 'controller' THEN 0 WHEN 'service' THEN 1
                        WHEN 'repository' THEN 2 ELSE 3 END,
              path, start_line
            LIMIT ?
        """
        with closing(self.database.connect()) as connection:
            rows = connection.execute(sql, parameters).fetchall()
        return [{
            **dict(row),
            "relationships": json.loads(str(row["relationships_json"])),
        } for row in rows]

    def paths_for_repository(self, repository: str) -> set[str]:
        with closing(self.database.connect()) as connection:
            rows = connection.execute(
                "SELECT DISTINCT path FROM code_state_components WHERE repository = ?",
                (repository,),
            ).fetchall()
        return {str(row[0]) for row in rows}

    @staticmethod
    def _upsert_components(
        connection: Any,
        repository: str,
        commit_sha: str,
        components: list[CodeComponent],
        now: str,
    ) -> None:
        for item in components:
            connection.execute(
                """
                INSERT INTO code_state_components(
                    id, repository, commit_sha, module, kind, symbol, path, role,
                    relationships_json, start_line, end_line, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON DUPLICATE KEY UPDATE
                    commit_sha = VALUES(commit_sha),
                    module = VALUES(module),
                    kind = VALUES(kind),
                    role = VALUES(role),
                    relationships_json = VALUES(relationships_json),
                    start_line = VALUES(start_line),
                    end_line = VALUES(end_line),
                    updated_at = VALUES(updated_at)
                """,
                (
                    uuid4().hex, repository, commit_sha, item.module, item.kind,
                    item.symbol, item.path, item.role,
                    json.dumps(item.relationships, ensure_ascii=False),
                    item.reference.start_line, item.reference.end_line, now,
                ),
            )
