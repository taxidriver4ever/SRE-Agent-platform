"""ValidationRun、Execution、Test、Evidence 与 Event 的 MySQL 持久化。"""

from __future__ import annotations

import hashlib
import json
from contextlib import closing
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from app.core.database import ApplicationDatabase
from app.validation.models import (
    ChangedFile, EnvironmentSpec, ExecutionSide, ExecutionStatus, GeneratedTestRecord, InterfaceTarget,
    ManagedTestFile, ManagedTestSuite, ManagedTestSuiteVersion, ProjectType,
    RegressionResult, TestCaseResult, ValidationEvidence, ValidationExecution, ValidationRun,
)


class ValidationRepository:
    def __init__(self, database: ApplicationDatabase) -> None:
        self.database = database

    def create(
        self, *, user_id: str, project_id: str, idempotency_key: str,
        repository: str, repository_url: str | None, base_ref: str, base_sha: str,
        candidate_ref: str, candidate_sha: str, project_type: str,
        test_suite_hash: str, run_existing_tests: bool, run_uploaded_tests: bool,
        generate_ai_tests: bool,
    ) -> ValidationRun:
        now = self._now()
        validation_id = uuid4().hex
        with closing(self.database.connect()) as connection:
            connection.execute(
                """
                INSERT INTO validation_runs(
                    id,user_id,project_id,idempotency_key,repository,repository_url,
                    base_ref,base_commit_sha,candidate_ref,candidate_commit_sha,status,
                    runner_type,project_type,test_suite_hash,run_existing_tests,
                    run_uploaded_tests,generate_ai_tests,comparison_confidence,
                    regression_count,existing_failure_count,possible_fix_count,
                    changed_files_json,created_at,updated_at
                ) VALUES (?,?,?,?,?,?,?,?,?,?,'PENDING','EPHEMERAL_DOCKER',?,?,?,?,?,
                          'INCONCLUSIVE',0,0,0,'[]',?,?)
                ON DUPLICATE KEY UPDATE id = id
                """,
                (
                    validation_id,user_id,project_id,idempotency_key,repository,repository_url,
                    base_ref,base_sha,candidate_ref,candidate_sha,project_type,test_suite_hash,
                    run_existing_tests,run_uploaded_tests,generate_ai_tests,now,now,
                ),
            )
            row = connection.execute(
                "SELECT * FROM validation_runs WHERE user_id=? AND idempotency_key=?",
                (user_id, idempotency_key),
            ).fetchone()
            connection.commit()
        return self._run(row)

    def get(self, user_id: str, validation_id: str) -> ValidationRun | None:
        with closing(self.database.connect()) as connection:
            row = connection.execute(
                "SELECT * FROM validation_runs WHERE id=? AND user_id=?", (validation_id,user_id)
            ).fetchone()
        return self._run(row) if row else None

    def get_unscoped(self, validation_id: str) -> ValidationRun | None:
        with closing(self.database.connect()) as connection:
            row = connection.execute("SELECT * FROM validation_runs WHERE id=?",(validation_id,)).fetchone()
        return self._run(row) if row else None

    def detail(self, user_id: str, validation_id: str) -> ValidationRun | None:
        run = self.get(user_id, validation_id)
        if run is None:
            return None
        run.executions = self.list_executions(validation_id)
        run.regressions = self.list_regressions(validation_id)
        run.evidence = self.list_evidence(validation_id)
        run.test_suite_versions = self.run_test_suite_versions(user_id, validation_id)
        run.ai_reference_test_paths = self.ai_reference_paths(validation_id)
        run.generated_tests = [self._generated_test(item) for item in self.generated_tests(validation_id)]
        return run

    def list_for_user(self, user_id: str, limit: int = 50) -> list[ValidationRun]:
        with closing(self.database.connect()) as connection:
            rows = connection.execute(
                "SELECT * FROM validation_runs WHERE user_id=? ORDER BY updated_at DESC LIMIT ?",
                (user_id,max(1,min(limit,100))),
            ).fetchall()
        return [self._run(row) for row in rows]

    def create_test_suite(self, *, user_id: str, repository: str, name: str,
                          description: str, project_type: ProjectType,
                          change_note: str, files: dict[str, str],
                          target: InterfaceTarget | None = None) -> ManagedTestSuite:
        now = self._now()
        suite_id, version_id = uuid4().hex, uuid4().hex
        content_hash = self._files_hash(files)
        with closing(self.database.connect()) as connection:
            connection.execute(
                """INSERT INTO validation_test_suites(
                   id,user_id,repository,name,description,project_type,latest_version,
                   archived,created_at,updated_at) VALUES (?,?,?,?,?,?,1,FALSE,?,?)""",
                (suite_id,user_id,repository,name,description,project_type.value,now,now),
            )
            self._insert_test_suite_version(
                connection, version_id, suite_id, 1, change_note, content_hash, files, now,
            )
            resolved_target = target or InterfaceTarget()
            connection.execute(
                """INSERT INTO validation_test_suite_targets(
                   suite_id,interface_id,module_name,module_path,interface_name,http_method,
                   route_path,source_file,symbol,created_at) VALUES (?,?,?,?,?,?,?,?,?,?)""",
                (suite_id,resolved_target.id or hashlib.sha256(
                    f"{repository}:unclassified:{suite_id}".encode()).hexdigest(),
                 resolved_target.module_name,resolved_target.module_path,
                 resolved_target.interface_name,resolved_target.http_method,
                 resolved_target.route_path,resolved_target.source_file,
                 resolved_target.symbol,now),
            )
            connection.commit()
        suite = self.get_test_suite(user_id, suite_id)
        assert suite is not None
        return suite

    def add_test_suite_version(self, *, user_id: str, suite_id: str,
                               change_note: str, files: dict[str, str]) -> ManagedTestSuite:
        now = self._now()
        with closing(self.database.connect()) as connection:
            row = connection.execute(
                "SELECT * FROM validation_test_suites WHERE id=? AND user_id=? FOR UPDATE",
                (suite_id,user_id),
            ).fetchone()
            if row is None:
                raise KeyError("test suite not found")
            if bool(row["archived"]):
                raise ValueError("archived test suite cannot receive a new version")
            version_number = int(row["latest_version"]) + 1
            self._insert_test_suite_version(
                connection, uuid4().hex, suite_id, version_number, change_note,
                self._files_hash(files), files, now,
            )
            connection.execute(
                "UPDATE validation_test_suites SET latest_version=?,updated_at=? WHERE id=?",
                (version_number,now,suite_id),
            )
            connection.commit()
        suite = self.get_test_suite(user_id, suite_id)
        assert suite is not None
        return suite

    def update_test_suite_metadata(self, *, user_id: str, suite_id: str,
                                   name: str, description: str) -> ManagedTestSuite:
        with closing(self.database.connect()) as connection:
            result = connection.execute(
                """UPDATE validation_test_suites SET name=?,description=?,updated_at=?
                   WHERE id=? AND user_id=? AND archived=FALSE""",
                (name,description,self._now(),suite_id,user_id),
            )
            connection.commit()
        if result.rowcount != 1:
            raise KeyError("active test suite not found")
        suite = self.get_test_suite(user_id, suite_id)
        assert suite is not None
        return suite

    def archive_test_suite(self, user_id: str, suite_id: str) -> bool:
        with closing(self.database.connect()) as connection:
            result = connection.execute(
                """UPDATE validation_test_suites SET archived=TRUE,updated_at=?
                   WHERE id=? AND user_id=? AND archived=FALSE""",
                (self._now(),suite_id,user_id),
            )
            connection.commit()
        return result.rowcount == 1

    def list_test_suites(self, user_id: str, repository: str | None = None,
                         include_archived: bool = False,
                         limit: int = 100) -> list[ManagedTestSuite]:
        where = ["user_id=?"]
        values: list[Any] = [user_id]
        if repository:
            where.append("repository=?")
            values.append(repository)
        if not include_archived:
            where.append("archived=FALSE")
        values.append(max(1,min(limit,100)))
        with closing(self.database.connect()) as connection:
            rows = connection.execute(
                f"SELECT * FROM validation_test_suites WHERE {' AND '.join(where)} ORDER BY updated_at DESC LIMIT ?",
                tuple(values),
            ).fetchall()
        return [self._suite(row, latest_only=False) for row in rows]

    def get_test_suite(self, user_id: str, suite_id: str) -> ManagedTestSuite | None:
        with closing(self.database.connect()) as connection:
            row = connection.execute(
                "SELECT * FROM validation_test_suites WHERE id=? AND user_id=?",
                (suite_id,user_id),
            ).fetchone()
        return self._suite(row, latest_only=False) if row else None

    def find_test_suite_by_interface(self, user_id: str, repository: str,
                                     interface_id: str) -> ManagedTestSuite | None:
        with closing(self.database.connect()) as connection:
            row = connection.execute(
                """SELECT s.* FROM validation_test_suites s
                   JOIN validation_test_suite_targets t ON t.suite_id=s.id
                   WHERE s.user_id=? AND s.repository=? AND t.interface_id=? AND s.archived=FALSE
                   ORDER BY s.updated_at DESC LIMIT 1""",
                (user_id,repository,interface_id),
            ).fetchone()
        return self._suite(row,latest_only=False) if row else None

    def begin_interface_generation(self, user_id: str, idempotency_key: str,
                                   request_fingerprint: str) -> dict[str, Any]:
        now = self._now()
        operation_id = uuid4().hex
        with closing(self.database.connect()) as connection:
            connection.execute(
                """INSERT INTO validation_interface_generation_requests(
                   id,user_id,idempotency_key,request_fingerprint,status,created_at,updated_at)
                   VALUES (?,?,?,?,'PROCESSING',?,?)
                   ON DUPLICATE KEY UPDATE id=id""",
                (operation_id,user_id,idempotency_key,request_fingerprint,now,now),
            )
            row = connection.execute(
                """SELECT * FROM validation_interface_generation_requests
                   WHERE user_id=? AND idempotency_key=?""",
                (user_id,idempotency_key),
            ).fetchone()
            connection.commit()
        output = dict(row)
        output["_created"] = str(row["id"]) == operation_id
        return output

    def complete_interface_generation(self, operation_id: str, *, suite_id: str,
                                      version_id: str, validation_id: str,
                                      action: str) -> None:
        with closing(self.database.connect()) as connection:
            connection.execute(
                """UPDATE validation_interface_generation_requests
                   SET status='COMPLETED',suite_id=?,version_id=?,validation_id=?,
                       action=?,error_message=NULL,updated_at=? WHERE id=?""",
                (suite_id,version_id,validation_id,action,self._now(),operation_id),
            )
            connection.commit()

    def fail_interface_generation(self, operation_id: str, error_message: str) -> None:
        with closing(self.database.connect()) as connection:
            connection.execute(
                """UPDATE validation_interface_generation_requests
                   SET status='FAILED',error_message=?,updated_at=? WHERE id=?""",
                (error_message[:2000],self._now(),operation_id),
            )
            connection.commit()

    def resolve_test_suite_versions(self, user_id: str, repository: str,
                                    project_type: ProjectType,
                                    version_ids: list[str]) -> list[ManagedTestSuiteVersion]:
        output: list[ManagedTestSuiteVersion] = []
        with closing(self.database.connect()) as connection:
            for version_id in version_ids:
                row = connection.execute(
                    """SELECT v.*,s.name AS suite_name,s.repository,s.project_type,s.archived
                       FROM validation_test_suite_versions v
                       JOIN validation_test_suites s ON s.id=v.suite_id
                       WHERE v.id=? AND s.user_id=?""",
                    (version_id,user_id),
                ).fetchone()
                if row is None:
                    raise KeyError(f"test suite version not found: {version_id}")
                if bool(row["archived"]):
                    raise ValueError("archived test suite cannot be selected for a new validation")
                if str(row["repository"]) != repository:
                    raise ValueError("test suite repository does not match validation repository")
                if str(row["project_type"]) != project_type.value:
                    raise ValueError("test suite project type does not match detected project")
                output.append(self._suite_version(row))
        return output

    def bind_test_suite_versions(self, validation_id: str,
                                 versions: list[ManagedTestSuiteVersion]) -> None:
        with closing(self.database.connect()) as connection:
            for sequence, version in enumerate(versions):
                connection.execute(
                    """INSERT INTO validation_run_test_suite_versions(validation_id,version_id,sequence_no)
                       VALUES (?,?,?) ON DUPLICATE KEY UPDATE sequence_no=VALUES(sequence_no)""",
                    (validation_id,version.id,sequence),
                )
            connection.commit()

    def run_test_suite_versions(self, user_id: str,
                                validation_id: str) -> list[ManagedTestSuiteVersion]:
        with closing(self.database.connect()) as connection:
            rows = connection.execute(
                """SELECT v.*,s.name AS suite_name,s.repository
                   FROM validation_run_test_suite_versions r
                   JOIN validation_test_suite_versions v ON v.id=r.version_id
                   JOIN validation_test_suites s ON s.id=v.suite_id
                   JOIN validation_runs vr ON vr.id=r.validation_id
                   WHERE r.validation_id=? AND vr.user_id=? ORDER BY r.sequence_no""",
                (validation_id,user_id),
            ).fetchall()
        return [self._suite_version(row) for row in rows]

    def bind_ai_reference_paths(self, validation_id: str, paths: list[str]) -> None:
        with closing(self.database.connect()) as connection:
            for sequence, path in enumerate(paths):
                connection.execute(
                    """INSERT INTO validation_ai_reference_samples(validation_id,path,sequence_no)
                       VALUES (?,?,?) ON DUPLICATE KEY UPDATE sequence_no=VALUES(sequence_no)""",
                    (validation_id,path,sequence),
                )
            connection.commit()

    def ai_reference_paths(self, validation_id: str) -> list[str]:
        with closing(self.database.connect()) as connection:
            rows = connection.execute(
                "SELECT path FROM validation_ai_reference_samples WHERE validation_id=? ORDER BY sequence_no",
                (validation_id,),
            ).fetchall()
        return [str(row["path"]) for row in rows]

    def ensure_request_fingerprint(self, validation_id: str, fingerprint: str) -> bool:
        """首次保存完整配置；幂等重试必须携带完全相同的配置。"""
        with closing(self.database.connect()) as connection:
            connection.execute(
                """INSERT INTO validation_request_configs(validation_id,request_fingerprint,created_at)
                   VALUES (?,?,?) ON DUPLICATE KEY UPDATE validation_id=validation_id""",
                (validation_id,fingerprint,self._now()),
            )
            row = connection.execute(
                "SELECT request_fingerprint FROM validation_request_configs WHERE validation_id=?",
                (validation_id,),
            ).fetchone()
            connection.commit()
        return row is not None and str(row["request_fingerprint"]) == fingerprint

    def begin(self, validation_id: str) -> bool:
        now = self._now()
        with closing(self.database.connect()) as connection:
            result = connection.execute(
                """UPDATE validation_runs SET status='PREPARING',started_at=COALESCE(started_at,?),
                   heartbeat_at=?,updated_at=? WHERE id=? AND status='PENDING'""",
                (now,now,now,validation_id),
            )
            if result.rowcount == 1:
                self._event(connection,validation_id,"validation.preparing",{"status":"PREPARING"},"validation.preparing")
            connection.commit()
        return result.rowcount == 1

    def update_run(self, validation_id: str, *, status: str | None = None,
                   summary: str | None = None, changed_files: list[ChangedFile] | None = None,
                   environment_fingerprint: str | None = None,
                   confidence: str | None = None, regression_count: int | None = None,
                   existing_failure_count: int | None = None,
                   possible_fix_count: int | None = None, error_message: str | None = None,
                   finished: bool = False) -> None:
        values: dict[str, Any] = {"updated_at": self._now(), "heartbeat_at": self._now()}
        if status is not None: values["status"] = status
        if summary is not None: values["summary"] = summary[:8000]
        if changed_files is not None: values["changed_files_json"] = self._json([x.model_dump(mode="json") for x in changed_files])
        if environment_fingerprint is not None: values["environment_fingerprint"] = environment_fingerprint
        if confidence is not None: values["comparison_confidence"] = confidence
        if regression_count is not None: values["regression_count"] = regression_count
        if existing_failure_count is not None: values["existing_failure_count"] = existing_failure_count
        if possible_fix_count is not None: values["possible_fix_count"] = possible_fix_count
        if error_message is not None: values["error_message"] = error_message[:8000]
        if finished: values["finished_at"] = self._now()
        assignments = ",".join(f"{key}=?" for key in values)
        with closing(self.database.connect()) as connection:
            connection.execute(f"UPDATE validation_runs SET {assignments} WHERE id=?",(*values.values(),validation_id))
            if status:
                self._event(connection,validation_id,f"validation.{status.lower()}",{"status":status},f"validation.{status.lower()}")
            connection.commit()

    def heartbeat(self, validation_id: str) -> None:
        now = self._now()
        with closing(self.database.connect()) as connection:
            connection.execute(
                "UPDATE validation_runs SET heartbeat_at=?,updated_at=? WHERE id=? AND status IN ('PREPARING','RUNNING','COMPARING')",
                (now,now,validation_id),
            )
            connection.commit()

    def set_test_suite_hash(self, validation_id: str, test_suite_hash: str) -> None:
        """在 AI 测试被冻结后一次性写入最终 Suite Hash。"""
        with closing(self.database.connect()) as connection:
            connection.execute(
                "UPDATE validation_runs SET test_suite_hash=?,updated_at=? WHERE id=? AND status='PREPARING'",
                (test_suite_hash, self._now(), validation_id),
            )
            connection.commit()

    def fail_incomplete_on_startup(self) -> int:
        now=self._now()
        with closing(self.database.connect()) as connection:
            rows=connection.execute("SELECT id FROM validation_runs WHERE status IN ('PREPARING','RUNNING','COMPARING')").fetchall()
            for row in rows:
                connection.execute(
                    "UPDATE validation_runs SET status='FAILED',error_message=?,finished_at=?,updated_at=? WHERE id=?",
                    ("runner interrupted by process restart; safe retry creates no duplicate execution",now,now,row["id"]),
                )
                self._event(connection,str(row["id"]),"validation.failed",{"reason":"process_restart"},"validation.failed")
            connection.commit()
        return len(rows)

    def cancel(self, user_id: str, validation_id: str) -> bool:
        now = self._now()
        with closing(self.database.connect()) as connection:
            result = connection.execute(
                """UPDATE validation_runs SET status='CANCELLED',summary='VALIDATION CANCELLED',
                   finished_at=?,updated_at=?,heartbeat_at=?
                   WHERE id=? AND user_id=? AND status IN ('PENDING','PREPARING','RUNNING','COMPARING')""",
                (now, now, now, validation_id, user_id),
            )
            if result.rowcount == 1:
                self._event(connection, validation_id, "validation.cancelled",
                            {"status": "CANCELLED"}, "validation.cancelled")
            connection.commit()
        return result.rowcount == 1

    def begin_execution(self, validation_id: str, side: ExecutionSide, commit_sha: str,
                        key: str, environment: EnvironmentSpec) -> ValidationExecution:
        now=self._now(); execution_id=uuid4().hex
        with closing(self.database.connect()) as connection:
            connection.execute(
                """INSERT INTO validation_executions(
                   id,validation_id,side,commit_sha,idempotency_key,test_suite_hash,
                   environment_fingerprint,environment_json,status,build_status,test_status,
                   started_at,duration_ms,stdout_summary,stderr_summary,artifact_metadata_json,created_at,updated_at
                   ) VALUES (?,?,?,?,?,?,?,?,'RUNNING','NOT_RUN','NOT_RUN',?,0,'','','{}',?,?)
                   ON DUPLICATE KEY UPDATE id=id""",
                (execution_id,validation_id,side.value,commit_sha,key,environment.test_suite_hash,
                 environment.environment_fingerprint,self._json(environment.model_dump(mode="json")),now,now,now),
            )
            row=connection.execute(
                "SELECT * FROM validation_executions WHERE validation_id=? AND idempotency_key=?",
                (validation_id,key),
            ).fetchone()
            self._event(connection, validation_id, "execution.running", {
                "execution_id": str(row["id"]), "side": side.value,
                "commit_sha": commit_sha, "status": "RUNNING",
            }, f"execution.running:{side.value}:{commit_sha}:{environment.test_suite_hash}")
            connection.commit()
        return self._execution(row)

    def complete_execution(self, execution_id: str, *, status: ExecutionStatus,
                           build_status: str, test_status: str, exit_code: int | None,
                           duration_ms: int, stdout: str, stderr: str,
                           artifacts: dict[str,str], tests: list[TestCaseResult]) -> None:
        now=self._now()
        with closing(self.database.connect()) as connection:
            connection.execute(
                """UPDATE validation_executions SET status=?,build_status=?,test_status=?,
                   finished_at=?,exit_code=?,duration_ms=?,stdout_summary=?,stderr_summary=?,
                   artifact_metadata_json=?,updated_at=? WHERE id=?""",
                (status.value,build_status,test_status,now,exit_code,duration_ms,
                 stdout[-8000:],stderr[-8000:],self._json(artifacts),now,execution_id),
            )
            connection.execute("DELETE FROM validation_test_results WHERE execution_id=?",(execution_id,))
            for test in tests:
                connection.execute(
                    """INSERT INTO validation_test_results(
                       id,execution_id,test_identity,suite,name,source,status,duration_ms,
                       failure_type,message,stack_trace_summary) VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                    (uuid4().hex,execution_id,test.identity,test.suite,test.name,test.source.value,
                     test.status.value,test.duration_ms,test.failure_type,test.message,test.stack_trace_summary),
                )
            execution = connection.execute(
                "SELECT validation_id,side,commit_sha FROM validation_executions WHERE id=?", (execution_id,)
            ).fetchone()
            self._event(connection, str(execution["validation_id"]), "execution.completed", {
                "execution_id": execution_id, "side": str(execution["side"]),
                "commit_sha": str(execution["commit_sha"]), "status": status.value,
                "build_status": build_status, "test_status": test_status,
                "duration_ms": duration_ms, "test_count": len(tests),
            }, f"execution.completed:{execution_id}")
            connection.commit()

    def list_executions(self, validation_id: str) -> list[ValidationExecution]:
        with closing(self.database.connect()) as connection:
            rows=connection.execute("SELECT * FROM validation_executions WHERE validation_id=? ORDER BY side",(validation_id,)).fetchall()
            output=[]
            for row in rows:
                tests=connection.execute("SELECT * FROM validation_test_results WHERE execution_id=? ORDER BY test_identity",(row["id"],)).fetchall()
                execution=self._execution(row)
                execution.tests=[self._test(item) for item in tests]
                output.append(execution)
        return output

    def replace_regressions(self, validation_id: str, results: list[RegressionResult]) -> None:
        with closing(self.database.connect()) as connection:
            connection.execute("DELETE FROM validation_regressions WHERE validation_id=?",(validation_id,))
            for item in results:
                item.id=item.id or uuid4().hex; item.validation_id=validation_id
                connection.execute(
                    """INSERT INTO validation_regressions(
                       id,validation_id,test_identity,suite,name,source,classification,confidence,
                       base_status,candidate_status,failure_type,message,stack_trace_summary,related_changes_json
                       ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (item.id,validation_id,item.test_identity,item.suite,item.name,item.source.value,
                     item.classification.value,item.confidence.value,
                     item.base_status.value if item.base_status else None,
                     item.candidate_status.value if item.candidate_status else None,
                     item.failure_type,item.message,item.stack_trace_summary,self._json(item.related_changes)),
                )
            connection.commit()

    def list_regressions(self, validation_id: str) -> list[RegressionResult]:
        with closing(self.database.connect()) as connection:
            rows=connection.execute("SELECT * FROM validation_regressions WHERE validation_id=? ORDER BY classification,name",(validation_id,)).fetchall()
        return [RegressionResult(
            id=str(row["id"]),validation_id=validation_id,test_identity=str(row["test_identity"]),
            suite=str(row["suite"]),name=str(row["name"]),source=str(row["source"]),
            classification=str(row["classification"]),confidence=str(row["confidence"]),
            base_status=str(row["base_status"]) if row["base_status"] else None,
            candidate_status=str(row["candidate_status"]) if row["candidate_status"] else None,
            failure_type=str(row["failure_type"]) if row["failure_type"] else None,
            message=str(row["message"]) if row["message"] else None,
            stack_trace_summary=str(row["stack_trace_summary"]) if row["stack_trace_summary"] else None,
            related_changes=self._loads(row["related_changes_json"],[]),
        ) for row in rows]

    def upsert_evidence(self, evidence: ValidationEvidence) -> None:
        with closing(self.database.connect()) as connection:
            connection.execute(
                """INSERT INTO validation_evidence(id,validation_id,source_type,title,summary,
                   structured_data_json,raw_artifact_reference,timestamp) VALUES (?,?,?,?,?,?,?,?)
                   ON DUPLICATE KEY UPDATE title=VALUES(title),summary=VALUES(summary),
                   structured_data_json=VALUES(structured_data_json),raw_artifact_reference=VALUES(raw_artifact_reference)""",
                (evidence.id,evidence.validation_id,evidence.source_type,evidence.title,evidence.summary,
                 self._json(evidence.structured_data),evidence.raw_artifact_reference,evidence.timestamp),
            ); connection.commit()

    def list_evidence(self, validation_id: str) -> list[ValidationEvidence]:
        with closing(self.database.connect()) as connection:
            rows=connection.execute("SELECT * FROM validation_evidence WHERE validation_id=? ORDER BY timestamp,id",(validation_id,)).fetchall()
        return [ValidationEvidence(
            id=str(row["id"]),validation_id=validation_id,source_type=str(row["source_type"]),
            title=str(row["title"]),summary=str(row["summary"]),
            structured_data=self._loads(row["structured_data_json"],{}),
            raw_artifact_reference=str(row["raw_artifact_reference"]) if row["raw_artifact_reference"] else None,
            timestamp=str(row["timestamp"]),
        ) for row in rows]

    def store_uploaded_tests(self, user_id: str, validation_id: str, files: dict[str,str]) -> None:
        now=self._now()
        with closing(self.database.connect()) as connection:
            owned=connection.execute("SELECT 1 FROM validation_runs WHERE id=? AND user_id=? AND status='PENDING'",(validation_id,user_id)).fetchone()
            if owned is None: raise KeyError("pending validation not found")
            for path,content in files.items():
                data=content.encode("utf-8")
                connection.execute(
                    """INSERT INTO validation_uploaded_tests(id,validation_id,user_id,path,content,content_sha256,size_bytes,created_at)
                       VALUES (?,?,?,?,?,?,?,?) ON DUPLICATE KEY UPDATE content=VALUES(content),content_sha256=VALUES(content_sha256),size_bytes=VALUES(size_bytes)""",
                    (uuid4().hex,validation_id,user_id,path,content,hashlib.sha256(data).hexdigest(),len(data),now),
                )
            connection.commit()

    def uploaded_tests(self, validation_id: str) -> dict[str,str]:
        with closing(self.database.connect()) as connection:
            rows=connection.execute("SELECT path,content FROM validation_uploaded_tests WHERE validation_id=? ORDER BY path",(validation_id,)).fetchall()
        return {str(row["path"]):str(row["content"]) for row in rows}

    def store_generated_test(self, validation_id: str, *, path: str, name: str, code: str,
                             reason: str, covered_change: str, valid: bool, error: str | None) -> None:
        with closing(self.database.connect()) as connection:
            connection.execute(
                """INSERT INTO validation_generated_tests(id,validation_id,target_file,test_name,test_code,reason,covered_change,valid,validation_error,created_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?) ON DUPLICATE KEY UPDATE test_name=VALUES(test_name),test_code=VALUES(test_code),reason=VALUES(reason),covered_change=VALUES(covered_change),valid=VALUES(valid),validation_error=VALUES(validation_error)""",
                (uuid4().hex,validation_id,path,name,code,reason,covered_change,valid,error,self._now()),
            ); connection.commit()

    def generated_tests(self, validation_id: str) -> list[dict[str,Any]]:
        with closing(self.database.connect()) as connection:
            return list(connection.execute("SELECT * FROM validation_generated_tests WHERE validation_id=? ORDER BY target_file",(validation_id,)).fetchall())

    def set_diagnosis(self, user_id: str, validation_id: str, diagnosis_id: str) -> bool:
        with closing(self.database.connect()) as connection:
            result=connection.execute("UPDATE validation_runs SET diagnosis_id=?,updated_at=? WHERE id=? AND user_id=? AND diagnosis_id IS NULL",(diagnosis_id,self._now(),validation_id,user_id))
            connection.commit()
        return result.rowcount == 1

    def append_event(self, validation_id: str, event_type: str, data: dict[str,Any], key: str | None=None) -> int:
        with closing(self.database.connect()) as connection:
            event_id=self._event(connection,validation_id,event_type,data,key); connection.commit(); return event_id

    def list_events(self, user_id: str, validation_id: str, after: int=0) -> list[dict[str,Any]]:
        if self.get(user_id,validation_id) is None: raise KeyError("validation not found")
        with closing(self.database.connect()) as connection:
            rows=connection.execute("SELECT * FROM validation_events WHERE validation_id=? AND id>? ORDER BY id LIMIT 200",(validation_id,after)).fetchall()
        return [{"id":int(row["id"]),"type":str(row["event_type"]),"data":self._loads(row["data_json"],{}),"created_at":str(row["created_at"])} for row in rows]

    def _event(self, connection: Any, validation_id: str, event_type: str, data: dict[str,Any], key: str | None) -> int:
        connection.execute("INSERT INTO validation_events(validation_id,event_type,event_key,data_json,created_at) VALUES (?,?,?,?,?) ON DUPLICATE KEY UPDATE id=LAST_INSERT_ID(id)",(validation_id,event_type,key,self._json(data),self._now()))
        row=connection.execute("SELECT LAST_INSERT_ID() AS id").fetchone(); return int(row["id"])

    def _suite(self, row: dict[str, Any], *, latest_only: bool) -> ManagedTestSuite:
        suite_id = str(row["id"])
        with closing(self.database.connect()) as connection:
            target_row = connection.execute(
                "SELECT * FROM validation_test_suite_targets WHERE suite_id=?",
                (suite_id,),
            ).fetchone()
            if latest_only:
                version_rows = connection.execute(
                    """SELECT v.*,s.name AS suite_name,s.repository
                       FROM validation_test_suite_versions v
                       JOIN validation_test_suites s ON s.id=v.suite_id
                       WHERE v.suite_id=? AND v.version_number=?""",
                    (suite_id,int(row["latest_version"])),
                ).fetchall()
            else:
                version_rows = connection.execute(
                    """SELECT v.*,s.name AS suite_name,s.repository
                       FROM validation_test_suite_versions v
                       JOIN validation_test_suites s ON s.id=v.suite_id
                       WHERE v.suite_id=? ORDER BY v.version_number DESC""",
                    (suite_id,),
                ).fetchall()
        return ManagedTestSuite(
            id=suite_id,repository=str(row["repository"]),name=str(row["name"]),
            description=str(row["description"]),project_type=str(row["project_type"]),
            latest_version=int(row["latest_version"]),archived=bool(row["archived"]),
            created_at=str(row["created_at"]),updated_at=str(row["updated_at"]),
            target=self._interface_target(target_row) if target_row else InterfaceTarget(),
            versions=[self._suite_version(item) for item in version_rows],
        )

    def _suite_version(self, row: dict[str, Any]) -> ManagedTestSuiteVersion:
        version_id = str(row["id"])
        with closing(self.database.connect()) as connection:
            files = connection.execute(
                """SELECT path,content,content_sha256,size_bytes
                   FROM validation_test_suite_files WHERE version_id=? ORDER BY path""",
                (version_id,),
            ).fetchall()
            target_row = connection.execute(
                "SELECT * FROM validation_test_suite_targets WHERE suite_id=?",
                (str(row["suite_id"]),),
            ).fetchone()
        return ManagedTestSuiteVersion(
            id=version_id,suite_id=str(row["suite_id"]),
            suite_name=str(row.get("suite_name") or ""),repository=str(row.get("repository") or ""),
            version=int(row["version_number"]),change_note=str(row["change_note"]),
            content_hash=str(row["content_hash"]),created_at=str(row["created_at"]),
            target=self._interface_target(target_row) if target_row else InterfaceTarget(),
            files=[ManagedTestFile(
                path=str(item["path"]),content=str(item["content"]),
                content_sha256=str(item["content_sha256"]),size_bytes=int(item["size_bytes"]),
            ) for item in files],
        )

    @staticmethod
    def _insert_test_suite_version(connection: Any, version_id: str, suite_id: str,
                                   version_number: int, change_note: str,
                                   content_hash: str, files: dict[str, str], now: str) -> None:
        connection.execute(
            """INSERT INTO validation_test_suite_versions(
               id,suite_id,version_number,change_note,content_hash,created_at)
               VALUES (?,?,?,?,?,?)""",
            (version_id,suite_id,version_number,change_note,content_hash,now),
        )
        for path, content in files.items():
            data = content.encode("utf-8")
            connection.execute(
                """INSERT INTO validation_test_suite_files(
                   id,version_id,path,content,content_sha256,size_bytes) VALUES (?,?,?,?,?,?)""",
                (uuid4().hex,version_id,path,content,hashlib.sha256(data).hexdigest(),len(data)),
            )

    @staticmethod
    def _files_hash(files: dict[str, str]) -> str:
        serialized = json.dumps(files,ensure_ascii=False,sort_keys=True,separators=(",",":"))
        return hashlib.sha256(serialized.encode("utf-8")).hexdigest()

    @staticmethod
    def _generated_test(row: dict[str, Any]) -> GeneratedTestRecord:
        return GeneratedTestRecord(
            target_file=str(row["target_file"]),test_name=str(row["test_name"]),
            test_code=str(row["test_code"]),reason=str(row["reason"]),
            covered_change=str(row["covered_change"]),valid=bool(row["valid"]),
            validation_error=str(row["validation_error"]) if row["validation_error"] else None,
        )

    @staticmethod
    def _interface_target(row: dict[str, Any]) -> InterfaceTarget:
        return InterfaceTarget(
            id=str(row["interface_id"]),module_name=str(row["module_name"]),
            module_path=str(row["module_path"]),interface_name=str(row["interface_name"]),
            http_method=str(row["http_method"]),route_path=str(row["route_path"]),
            source_file=str(row["source_file"]),symbol=str(row["symbol"]),
        )

    @classmethod
    def _run(cls,row:dict[str,Any]) -> ValidationRun:
        return ValidationRun(
            id=str(row["id"]),user_id=str(row["user_id"]),repository=str(row["repository"]),
            repository_url=str(row["repository_url"]) if row["repository_url"] else None,
            base_ref=str(row["base_ref"]),base_commit_sha=str(row["base_commit_sha"]),
            candidate_ref=str(row["candidate_ref"]),candidate_commit_sha=str(row["candidate_commit_sha"]),
            status=str(row["status"]),runner_type=str(row["runner_type"]),project_type=str(row["project_type"]),
            test_suite_hash=str(row["test_suite_hash"]),environment_fingerprint=str(row["environment_fingerprint"]) if row["environment_fingerprint"] else None,
            run_existing_tests=bool(row["run_existing_tests"]),run_uploaded_tests=bool(row["run_uploaded_tests"]),generate_ai_tests=bool(row["generate_ai_tests"]),
            started_at=str(row["started_at"]) if row["started_at"] else None,finished_at=str(row["finished_at"]) if row["finished_at"] else None,
            heartbeat_at=str(row["heartbeat_at"]) if row["heartbeat_at"] else None,summary=str(row["summary"]) if row["summary"] else None,
            regression_count=int(row["regression_count"]),existing_failure_count=int(row["existing_failure_count"]),possible_fix_count=int(row["possible_fix_count"]),
            comparison_confidence=str(row["comparison_confidence"]),diagnosis_id=str(row["diagnosis_id"]) if row["diagnosis_id"] else None,
            error_message=str(row["error_message"]) if row["error_message"] else None,created_at=str(row["created_at"]),updated_at=str(row["updated_at"]),
            changed_files=[ChangedFile.model_validate(item) for item in cls._loads(row["changed_files_json"],[])],
        )

    @classmethod
    def _execution(cls,row:dict[str,Any]) -> ValidationExecution:
        return ValidationExecution(
            id=str(row["id"]),validation_id=str(row["validation_id"]),side=str(row["side"]),commit_sha=str(row["commit_sha"]),
            idempotency_key=str(row["idempotency_key"]),status=str(row["status"]),build_status=str(row["build_status"]),test_status=str(row["test_status"]),
            started_at=str(row["started_at"]) if row["started_at"] else None,finished_at=str(row["finished_at"]) if row["finished_at"] else None,
            exit_code=int(row["exit_code"]) if row["exit_code"] is not None else None,duration_ms=int(row["duration_ms"]),
            stdout_summary=str(row["stdout_summary"]),stderr_summary=str(row["stderr_summary"]),artifact_metadata=cls._loads(row["artifact_metadata_json"],{}),
            environment=EnvironmentSpec.model_validate(cls._loads(row["environment_json"],{})),
        )

    @staticmethod
    def _test(row:dict[str,Any]) -> TestCaseResult:
        return TestCaseResult(suite=str(row["suite"]),name=str(row["name"]),source=str(row["source"]),status=str(row["status"]),duration_ms=int(row["duration_ms"]),failure_type=str(row["failure_type"]) if row["failure_type"] else None,message=str(row["message"]) if row["message"] else None,stack_trace_summary=str(row["stack_trace_summary"]) if row["stack_trace_summary"] else None)

    @staticmethod
    def _json(value:Any)->str: return json.dumps(value,ensure_ascii=False,default=str)
    @staticmethod
    def _loads(value:Any,default:Any)->Any:
        try: return json.loads(str(value)) if value not in (None,"") else default
        except (ValueError,TypeError): return default
    @staticmethod
    def _now()->str: return datetime.now(timezone.utc).isoformat()
