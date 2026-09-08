"""Pre-Merge Validation 的领域模型。"""

from __future__ import annotations

import hashlib
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator


class ValidationStatus(StrEnum):
    PENDING = "PENDING"
    PREPARING = "PREPARING"
    RUNNING = "RUNNING"
    COMPARING = "COMPARING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"


class ExecutionSide(StrEnum):
    BASE = "BASE"
    CANDIDATE = "CANDIDATE"


class ExecutionStatus(StrEnum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    TIMEOUT = "TIMEOUT"


class StageStatus(StrEnum):
    NOT_RUN = "NOT_RUN"
    PASSED = "PASSED"
    FAILED = "FAILED"
    TIMEOUT = "TIMEOUT"


class ProjectType(StrEnum):
    MAVEN = "MAVEN"
    GRADLE = "GRADLE"
    PYTHON = "PYTHON"
    NODE = "NODE"
    UNKNOWN = "UNKNOWN"


class InterfaceChangeType(StrEnum):
    ADDED = "ADDED"
    MODIFIED = "MODIFIED"
    REMOVED = "REMOVED"
    UNCHANGED = "UNCHANGED"


class TestStatus(StrEnum):
    PASSED = "PASSED"
    FAILED = "FAILED"
    SKIPPED = "SKIPPED"
    ERROR = "ERROR"


class TestSource(StrEnum):
    REPOSITORY = "REPOSITORY"
    UPLOADED = "UPLOADED"
    AI_GENERATED = "AI_GENERATED"


class RegressionClassification(StrEnum):
    NEW_REGRESSION = "NEW_REGRESSION"
    EXISTING_FAILURE = "EXISTING_FAILURE"
    POSSIBLE_FIX = "POSSIBLE_FIX"
    UNCHANGED_PASS = "UNCHANGED_PASS"
    NEW_TEST = "NEW_TEST"
    BUILD_REGRESSION = "BUILD_REGRESSION"
    AI_CONFIRMED_REGRESSION = "AI_CONFIRMED_REGRESSION"
    INVALID_OR_EXISTING_BEHAVIOR = "INVALID_OR_EXISTING_BEHAVIOR"
    NO_REGRESSION_DETECTED = "NO_REGRESSION_DETECTED"
    AI_TEST_INVALID = "AI_TEST_INVALID"
    COMPARISON_INCONCLUSIVE = "COMPARISON_INCONCLUSIVE"


class ComparisonConfidence(StrEnum):
    HIGH = "HIGH"
    MEDIUM = "MEDIUM"
    LOW = "LOW"
    INCONCLUSIVE = "INCONCLUSIVE"


class TestCaseResult(BaseModel):
    suite: str = Field(max_length=500)
    name: str = Field(max_length=500)
    status: TestStatus
    duration_ms: int = Field(default=0, ge=0)
    failure_type: str | None = Field(default=None, max_length=255)
    message: str | None = Field(default=None, max_length=4000)
    stack_trace_summary: str | None = Field(default=None, max_length=8000)
    source: TestSource = TestSource.REPOSITORY

    @property
    def identity(self) -> str:
        identity = f"{self.source.value}:{self.suite}::{self.name}"
        if len(identity) <= 680:
            return identity
        digest = hashlib.sha256(identity.encode("utf-8")).hexdigest()
        return f"{identity[:610]}:{digest}"


class EnvironmentSpec(BaseModel):
    runner_image: str
    cpu_limit: float
    memory_mb: int
    pids_limit: int
    project_type: ProjectType
    runtime_version: str
    dependency_mode: str
    network_policy: str
    test_suite_hash: str
    adapter_version: str
    environment_fingerprint: str


class ValidationExecution(BaseModel):
    id: str
    validation_id: str
    side: ExecutionSide
    commit_sha: str = Field(pattern=r"^[0-9a-f]{40}$")
    idempotency_key: str
    status: ExecutionStatus
    build_status: StageStatus = StageStatus.NOT_RUN
    test_status: StageStatus = StageStatus.NOT_RUN
    started_at: str | None = None
    finished_at: str | None = None
    exit_code: int | None = None
    duration_ms: int = 0
    stdout_summary: str = ""
    stderr_summary: str = ""
    artifact_metadata: dict[str, Any] = Field(default_factory=dict)
    environment: EnvironmentSpec
    tests: list[TestCaseResult] = Field(default_factory=list)


class ValidationEvidence(BaseModel):
    id: str
    validation_id: str
    source_type: str
    title: str
    summary: str
    structured_data: dict[str, Any] = Field(default_factory=dict)
    raw_artifact_reference: str | None = None
    timestamp: str


class RegressionResult(BaseModel):
    id: str = ""
    validation_id: str = ""
    test_identity: str
    suite: str
    name: str
    source: TestSource
    classification: RegressionClassification
    confidence: ComparisonConfidence
    base_status: TestStatus | None = None
    candidate_status: TestStatus | None = None
    failure_type: str | None = None
    message: str | None = None
    stack_trace_summary: str | None = None
    related_changes: list[str] = Field(default_factory=list)


class ChangedFile(BaseModel):
    status: str
    path: str
    old_path: str | None = None


class ManagedTestFile(BaseModel):
    path: str
    content: str
    content_sha256: str
    size_bytes: int


class InterfaceTarget(BaseModel):
    id: str = Field(default="", max_length=64)
    module_name: str = Field(default="unclassified", max_length=160)
    module_path: str = Field(default="", max_length=500)
    interface_name: str = Field(default="unclassified", max_length=240)
    http_method: str = Field(default="FUNCTION", max_length=20)
    route_path: str = Field(default="", max_length=500)
    source_file: str = Field(default="", max_length=500)
    symbol: str = Field(default="", max_length=240)
    change_type: InterfaceChangeType = InterfaceChangeType.UNCHANGED
    base_exists: bool = True
    candidate_exists: bool = True


class ManagedTestSuiteVersion(BaseModel):
    id: str
    suite_id: str
    suite_name: str = ""
    repository: str = ""
    version: int
    change_note: str = ""
    content_hash: str
    created_at: str
    files: list[ManagedTestFile] = Field(default_factory=list)
    target: InterfaceTarget = Field(default_factory=InterfaceTarget)


class ManagedTestSuite(BaseModel):
    id: str
    repository: str
    name: str
    description: str = ""
    project_type: ProjectType
    latest_version: int
    archived: bool = False
    created_at: str
    updated_at: str
    target: InterfaceTarget = Field(default_factory=InterfaceTarget)
    versions: list[ManagedTestSuiteVersion] = Field(default_factory=list)


class GeneratedTestRecord(BaseModel):
    target_file: str
    test_name: str
    test_code: str
    reason: str
    covered_change: str
    valid: bool
    validation_error: str | None = None


class ValidationRun(BaseModel):
    id: str
    user_id: str = Field(exclude=True)
    repository: str
    repository_url: str | None = None
    base_ref: str
    base_commit_sha: str = Field(pattern=r"^[0-9a-f]{40}$")
    candidate_ref: str
    candidate_commit_sha: str = Field(pattern=r"^[0-9a-f]{40}$")
    status: ValidationStatus
    runner_type: str = "EPHEMERAL_DOCKER"
    project_type: ProjectType = ProjectType.UNKNOWN
    test_suite_hash: str
    environment_fingerprint: str | None = None
    run_existing_tests: bool = True
    run_uploaded_tests: bool = False
    generate_ai_tests: bool = False
    started_at: str | None = None
    finished_at: str | None = None
    heartbeat_at: str | None = None
    summary: str | None = None
    regression_count: int = 0
    existing_failure_count: int = 0
    possible_fix_count: int = 0
    comparison_confidence: ComparisonConfidence = ComparisonConfidence.INCONCLUSIVE
    diagnosis_id: str | None = None
    error_message: str | None = None
    created_at: str
    updated_at: str
    changed_files: list[ChangedFile] = Field(default_factory=list)
    executions: list[ValidationExecution] = Field(default_factory=list)
    regressions: list[RegressionResult] = Field(default_factory=list)
    evidence: list[ValidationEvidence] = Field(default_factory=list)
    test_suite_versions: list[ManagedTestSuiteVersion] = Field(default_factory=list)
    ai_reference_test_paths: list[str] = Field(default_factory=list)
    generated_tests: list[GeneratedTestRecord] = Field(default_factory=list)


class ValidationCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    repository: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,119}$")
    base_ref: str = Field(min_length=1, max_length=240)
    candidate_ref: str = Field(min_length=1, max_length=240)
    run_existing_tests: bool = True
    run_uploaded_tests: bool = False
    generate_ai_tests: bool = False
    uploaded_tests: list["UploadedTestFile"] = Field(default_factory=list, max_length=20)
    test_suite_version_ids: list[str] = Field(default_factory=list, max_length=10)
    ai_reference_test_paths: list[str] = Field(default_factory=list, max_length=10)

    @model_validator(mode="after")
    def validate_mode(self) -> "ValidationCreateRequest":
        if not (self.run_existing_tests or self.run_uploaded_tests or self.generate_ai_tests
                or self.test_suite_version_ids):
            raise ValueError("at least one test mode must be enabled")
        if self.base_ref == self.candidate_ref:
            raise ValueError("base_ref and candidate_ref must be different")
        if self.run_uploaded_tests and not self.uploaded_tests:
            raise ValueError("uploaded_tests is required when run_uploaded_tests is enabled")
        if self.uploaded_tests and not self.run_uploaded_tests:
            raise ValueError("run_uploaded_tests must be enabled when uploaded_tests are supplied")
        if self.ai_reference_test_paths and not self.generate_ai_tests:
            raise ValueError("generate_ai_tests must be enabled when AI reference tests are supplied")
        if len(set(self.test_suite_version_ids)) != len(self.test_suite_version_ids):
            raise ValueError("test_suite_version_ids must not contain duplicates")
        if len(set(self.ai_reference_test_paths)) != len(self.ai_reference_test_paths):
            raise ValueError("ai_reference_test_paths must not contain duplicates")
        return self


class UploadedTestFile(BaseModel):
    model_config = ConfigDict(extra="forbid")
    path: str = Field(min_length=1, max_length=500)
    content: str = Field(max_length=262_144)


class UploadedTestRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    files: list[UploadedTestFile] = Field(min_length=1, max_length=20)


class ManagedTestSuiteCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    repository: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,119}$")
    name: str = Field(min_length=1, max_length=160)
    description: str = Field(default="", max_length=2000)
    project_type: ProjectType
    change_note: str = Field(default="initial version", max_length=1000)
    files: list[UploadedTestFile] = Field(min_length=1, max_length=20)
    target: InterfaceTarget = Field(default_factory=InterfaceTarget)

    @model_validator(mode="after")
    def supported_project(self) -> "ManagedTestSuiteCreateRequest":
        if self.project_type not in {ProjectType.MAVEN, ProjectType.PYTHON}:
            raise ValueError("managed test suites currently support MAVEN and PYTHON")
        if self.target.module_name.strip() in {"", "unclassified"}:
            raise ValueError("target.module_name is required")
        if self.target.interface_name.strip() in {"", "unclassified"}:
            raise ValueError("target.interface_name is required")
        return self


class ManagedTestSuiteVersionCreateRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    change_note: str = Field(min_length=1, max_length=1000)
    files: list[UploadedTestFile] = Field(min_length=1, max_length=20)


class ManagedTestSuiteMetadataRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=160)
    description: str = Field(default="", max_length=2000)


class ManagedTestSuiteListResponse(BaseModel):
    items: list[ManagedTestSuite]


class RepositoryTestFilesResponse(BaseModel):
    repository: str
    ref: str
    commit_sha: str = Field(pattern=r"^[0-9a-f]{40}$")
    project_type: ProjectType
    paths: list[str]


class RepositoryInterfacesResponse(BaseModel):
    repository: str
    base_ref: str
    candidate_ref: str
    base_commit_sha: str = Field(pattern=r"^[0-9a-f]{40}$")
    candidate_commit_sha: str = Field(pattern=r"^[0-9a-f]{40}$")
    project_type: ProjectType
    interfaces: list[InterfaceTarget]


class InterfaceTestGenerationRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    repository: str = Field(pattern=r"^[a-zA-Z0-9][a-zA-Z0-9_.-]{0,119}$")
    base_ref: str = Field(min_length=1, max_length=240)
    candidate_ref: str = Field(min_length=1, max_length=240)
    interface_id: str = Field(min_length=1, max_length=64)
    suite_id: str | None = Field(default=None, max_length=32)
    run_existing_tests: bool = True

    @model_validator(mode="after")
    def different_refs(self) -> "InterfaceTestGenerationRequest":
        if self.base_ref == self.candidate_ref:
            raise ValueError("base_ref and candidate_ref must be different")
        return self


class GeneratedTestCase(BaseModel):
    model_config = ConfigDict(extra="forbid")
    target_file: str = Field(min_length=1, max_length=500)
    test_name: str = Field(min_length=1, max_length=240)
    test_code: str = Field(min_length=1, max_length=50_000)
    reason: str = Field(min_length=1, max_length=1000)
    covered_change: str = Field(min_length=1, max_length=1000)


class GeneratedTestSuite(BaseModel):
    model_config = ConfigDict(extra="forbid")
    tests: list[GeneratedTestCase] = Field(default_factory=list, max_length=8)


class ValidationCreatedResponse(BaseModel):
    id: str
    status: ValidationStatus
    events_url: str
    detail_url: str


class InterfaceTestGenerationResponse(BaseModel):
    suite: ManagedTestSuite
    version: ManagedTestSuiteVersion
    validation: ValidationCreatedResponse
    action: str


class ValidationListResponse(BaseModel):
    items: list[ValidationRun]


class BranchListResponse(BaseModel):
    repository: str
    branches: list[str]


ValidationCreateRequest.model_rebuild()
