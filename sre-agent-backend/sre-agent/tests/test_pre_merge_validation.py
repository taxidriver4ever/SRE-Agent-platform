"""Pre-Merge Validation 的安全边界、分类矩阵与结果归一化测试。"""

from datetime import datetime, timezone
import json
from pathlib import Path
import subprocess
from uuid import uuid4

import pytest
from pydantic import ValidationError

from app.repositories.registry import RepositoryRegistry
from app.validation.ai_tests import AITestGenerator, TestFileValidationError as FileValidationError, TestFileValidator
from app.validation.comparator import ComparisonError, ValidationComparator
from app.validation.interfaces import InterfaceDiscovery
from app.validation.models import (
    ComparisonConfidence, EnvironmentSpec, ExecutionSide, ExecutionStatus,
    GeneratedTestCase, InterfaceChangeType, InterfaceTarget, InterfaceTestGenerationRequest,
    ManagedTestSuiteCreateRequest, ProjectType,
    RegressionClassification, RegressionResult, StageStatus, TestCaseResult as CaseResult,
    TestSource as CaseSource, TestStatus as CaseStatus,
    UploadedTestFile, ValidationCreateRequest, ValidationExecution,
)
from app.validation.runner import normalize_junit
from app.validation.repository import ValidationRepository
from app.validation.project import AdapterRegistry, ProjectDetector
from app.validation.runner import DockerValidationRunner, RunnerLimits, RunnerResult
from app.validation.service import ValidationService
from app.llm.base import LLMResponse
from app.conversation import ConversationService
from app.diagnosis import DiagnosisRepository, DiagnosisService
from tests.mysql_support import mysql_test_database


def environment(*, suite="suite", fingerprint="environment"):
    return EnvironmentSpec(runner_image="fixed:1", cpu_limit=1, memory_mb=512, pids_limit=64,
        project_type=ProjectType.PYTHON, runtime_version="3.12", dependency_mode="image",
        network_policy="none", test_suite_hash=suite, adapter_version="v1",
        environment_fingerprint=fingerprint)


def execution(side, tests=(), *, build=StageStatus.PASSED, test=StageStatus.PASSED,
              status=ExecutionStatus.COMPLETED, suite="suite", fingerprint="environment"):
    return ValidationExecution(id=side.value.lower(), validation_id="v", side=side,
        commit_sha=("a" if side is ExecutionSide.BASE else "b") * 40,
        idempotency_key=side.value, status=status, build_status=build,
        test_status=test, environment=environment(suite=suite, fingerprint=fingerprint),
        tests=list(tests))


def test_create_request_rejects_identical_branch():
    with pytest.raises(ValidationError):
        ValidationCreateRequest(repository="repo", base_ref="main", candidate_ref="main")


def test_create_request_requires_at_least_one_test_mode():
    with pytest.raises(ValidationError):
        ValidationCreateRequest(repository="repo", base_ref="main", candidate_ref="feature",
            run_existing_tests=False)


def test_uploaded_mode_requires_files():
    with pytest.raises(ValidationError):
        ValidationCreateRequest(repository="repo", base_ref="main", candidate_ref="feature",
            run_uploaded_tests=True)


def test_managed_suite_is_a_valid_test_source_and_ai_samples_require_ai_mode():
    request = ValidationCreateRequest(repository="repo", base_ref="main", candidate_ref="feature",
        run_existing_tests=False, test_suite_version_ids=["version-1"])
    assert request.test_suite_version_ids == ["version-1"]
    with pytest.raises(ValidationError):
        ValidationCreateRequest(repository="repo", base_ref="main", candidate_ref="feature",
            ai_reference_test_paths=["tests/test_example.py"])


def test_branch_guard_accepts_normal_feature_branch():
    assert RepositoryRegistry._safe_branch("feature/pre-merge-validation")
    assert RepositoryRegistry._safe_branch("fix/payment-timeout")


@pytest.mark.parametrize("branch", ["../main", "bad branch", "-danger", "main.lock", "x@{y", "x\\y"])
def test_branch_guard_rejects_ref_injection(branch):
    assert not RepositoryRegistry._safe_branch(branch)


def test_uploaded_test_rejects_path_traversal():
    with pytest.raises(FileValidationError):
        TestFileValidator().validate_uploaded(ProjectType.PYTHON,
            [UploadedTestFile(path="tests/../../app/main.py", content="pass")])


def test_uploaded_test_rejects_production_source_path():
    with pytest.raises(FileValidationError):
        TestFileValidator().validate_uploaded(ProjectType.MAVEN,
            [UploadedTestFile(path="src/main/java/Evil.java", content="class Evil {}")])


def test_uploaded_test_rejects_binary_and_size_limit():
    with pytest.raises(FileValidationError):
        TestFileValidator().validate_uploaded(ProjectType.PYTHON,
            [UploadedTestFile(path="tests/test_bad.py", content="x\x00y")])


def test_generated_test_rejects_process_and_network_api():
    from app.validation.models import GeneratedTestCase
    candidate = GeneratedTestCase(target_file="tests/test_bad.py", test_name="test_bad",
        test_code="import subprocess\ndef test_bad(): subprocess.run(['x'])",
        reason="bad", covered_change="x")
    with pytest.raises(FileValidationError):
        TestFileValidator().validate_generated(ProjectType.PYTHON, candidate)


@pytest.mark.asyncio
async def test_ai_generator_passes_all_ten_user_selected_reference_samples():
    class CapturingLLM:
        payload = None
        async def complete(self,messages):
            self.payload = json.loads(messages[1].content)
            return LLMResponse(content='{"tests":[]}',model="test")
    llm = CapturingLLM()
    await AITestGenerator(llm).generate(project_type=ProjectType.PYTHON,diff="",
        changed_files=[],code_state=[],
        test_samples={f"tests/test_{index}.py":f"def test_{index}(): pass" for index in range(10)})
    assert len(llm.payload["representative_tests"]) == 10


def test_comparator_detects_new_regression():
    passed = CaseResult(suite="s", name="case", status=CaseStatus.PASSED)
    failed = CaseResult(suite="s", name="case", status=CaseStatus.FAILED, message="assertion")
    results, confidence = ValidationComparator().compare(
        execution(ExecutionSide.BASE, [passed]), execution(ExecutionSide.CANDIDATE, [failed]))
    assert results[0].classification is RegressionClassification.NEW_REGRESSION
    assert confidence is ComparisonConfidence.HIGH


def test_comparator_separates_existing_failure_and_possible_fix():
    failed = CaseResult(suite="s", name="old", status=CaseStatus.FAILED)
    fixed = CaseResult(suite="s", name="old", status=CaseStatus.PASSED)
    results, _ = ValidationComparator().compare(
        execution(ExecutionSide.BASE, [failed]), execution(ExecutionSide.CANDIDATE, [failed]))
    assert results[0].classification is RegressionClassification.EXISTING_FAILURE
    results, _ = ValidationComparator().compare(
        execution(ExecutionSide.BASE, [failed]), execution(ExecutionSide.CANDIDATE, [fixed]))
    assert results[0].classification is RegressionClassification.POSSIBLE_FIX


def test_comparator_detects_real_possible_fix_when_base_process_exit_is_failed():
    failed = CaseResult(suite="s", name="bug", status=CaseStatus.FAILED)
    fixed = CaseResult(suite="s", name="bug", status=CaseStatus.PASSED)
    results, confidence = ValidationComparator().compare(
        execution(ExecutionSide.BASE, [failed], test=StageStatus.FAILED),
        execution(ExecutionSide.CANDIDATE, [fixed], test=StageStatus.PASSED),
    )
    assert results[0].classification is RegressionClassification.POSSIBLE_FIX
    assert confidence is ComparisonConfidence.HIGH


def test_comparator_detects_build_regression():
    results, _ = ValidationComparator().compare(execution(ExecutionSide.BASE),
        execution(ExecutionSide.CANDIDATE, build=StageStatus.FAILED))
    assert results[0].classification is RegressionClassification.BUILD_REGRESSION


def test_comparator_marks_timeout_inconclusive():
    results, confidence = ValidationComparator().compare(execution(ExecutionSide.BASE),
        execution(ExecutionSide.CANDIDATE, status=ExecutionStatus.TIMEOUT))
    assert results[0].classification is RegressionClassification.COMPARISON_INCONCLUSIVE
    assert confidence is ComparisonConfidence.INCONCLUSIVE


def test_comparator_rejects_different_test_suite_hash():
    with pytest.raises(ComparisonError):
        ValidationComparator().compare(execution(ExecutionSide.BASE, suite="left"),
            execution(ExecutionSide.CANDIDATE, suite="right"))


def test_ai_test_pass_on_base_fail_on_candidate_is_confirmed_regression():
    base = CaseResult(suite="ai", name="edge", status=CaseStatus.PASSED,
                      source=CaseSource.AI_GENERATED)
    candidate = CaseResult(suite="ai", name="edge", status=CaseStatus.FAILED,
                           source=CaseSource.AI_GENERATED)
    results, _ = ValidationComparator().compare(
        execution(ExecutionSide.BASE, [base]), execution(ExecutionSide.CANDIDATE, [candidate]))
    assert results[0].classification is RegressionClassification.AI_CONFIRMED_REGRESSION


def test_junit_normalization_preserves_failure(tmp_path: Path):
    report = tmp_path / "validation-junit.xml"
    report.write_text('<testsuite><testcase classname="payments" name="timeout" time="0.12"><failure type="AssertionError" message="slow">trace</failure></testcase></testsuite>', encoding="utf-8")
    results = normalize_junit(tmp_path, ("validation-junit.xml",))
    assert results[0].status is CaseStatus.FAILED
    assert results[0].duration_ms == 120
    assert results[0].failure_type == "AssertionError"


def validation_owner():
    database = mysql_test_database(reset=False)
    user_id = uuid4().hex
    with database.connect() as connection:
        connection.execute("INSERT INTO users(id,username,password_hash,created_at) VALUES (?,?,?,?)",
            (user_id, f"validation-{uuid4().hex[:8]}", "test", datetime.now(timezone.utc).isoformat()))
        connection.commit()
    return database, user_id


def test_validation_repository_is_user_scoped_and_creation_is_idempotent():
    database, user_id = validation_owner()
    repository = ValidationRepository(database)
    arguments = dict(user_id=user_id, project_id="sre-lab", idempotency_key="stable-key",
        repository="order-service", repository_url=None, base_ref="main", base_sha="a" * 40,
        candidate_ref="feature", candidate_sha="b" * 40, project_type="PYTHON",
        test_suite_hash="c" * 64, run_existing_tests=True, run_uploaded_tests=False,
        generate_ai_tests=False)
    first = repository.create(**arguments)
    second = repository.create(**arguments)
    assert first.id == second.id
    assert repository.get(uuid4().hex, first.id) is None
    assert repository.get(user_id, first.id).candidate_commit_sha == "b" * 40


def test_managed_test_suite_versions_are_immutable_user_scoped_and_selectable():
    database, user_id = validation_owner()
    repository = ValidationRepository(database)
    first = repository.create_test_suite(user_id=user_id, repository="demo", name="regression",
        description="bug contract", project_type=ProjectType.PYTHON, change_note="initial",
        files={"tests/test_bug.py":"def test_bug():\n    assert False\n"})
    assert first.latest_version == 1 and first.versions[0].version == 1
    updated = repository.add_test_suite_version(user_id=user_id, suite_id=first.id,
        change_note="expect fix", files={"tests/test_bug.py":"def test_bug():\n    assert True\n"})
    assert updated.latest_version == 2
    assert [item.version for item in updated.versions] == [2, 1]
    assert updated.versions[1].files[0].content.endswith("assert False\n")
    selected = repository.resolve_test_suite_versions(
        user_id,"demo",ProjectType.PYTHON,[updated.versions[0].id,updated.versions[1].id])
    assert [item.version for item in selected] == [2, 1]
    assert repository.get_test_suite(uuid4().hex, first.id) is None
    assert repository.archive_test_suite(user_id, first.id)
    with pytest.raises(ValueError):
        repository.resolve_test_suite_versions(
            user_id,"demo",ProjectType.PYTHON,[updated.versions[0].id])


def test_managed_suite_persists_module_and_interface_target():
    database, user_id = validation_owner()
    repository = ValidationRepository(database)
    target = InterfaceTarget(id="f" * 64,module_name="payment.api",module_path="app/api",
        interface_name="create_payment",http_method="POST",route_path="/payments",
        source_file="app/api/payment.py",symbol="create_payment")
    suite = repository.create_test_suite(user_id=user_id,repository="demo",name="payment API",
        description="contract",project_type=ProjectType.PYTHON,change_note="initial",
        files={"tests/test_payment.py":"def test_payment():\n    assert True\n"},target=target)
    assert suite.target == target
    assert suite.versions[0].target == target
    found = repository.find_test_suite_by_interface(user_id,"demo",target.id)
    assert found is not None and found.id == suite.id


def test_manual_suite_request_requires_module_and_interface():
    with pytest.raises(ValidationError):
        ManagedTestSuiteCreateRequest(repository="demo",name="unclassified",
            project_type=ProjectType.PYTHON,
            files=[UploadedTestFile(path="tests/test_x.py",content="def test_x(): pass")])


def _git(path: Path, *arguments: str) -> str:
    return subprocess.run(
        ["git", "-C", str(path), *arguments],
        check=True,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    ).stdout.strip()


@pytest.mark.asyncio
async def test_branch_resolve_freezes_full_sha_and_rejects_unlisted_ref(tmp_path: Path):
    repository = tmp_path / "repo"; repository.mkdir()
    _git(repository, "init")
    _git(repository, "config", "user.email", "validation@example.test")
    _git(repository, "config", "user.name", "Validation Test")
    (repository / "requirements.txt").write_text("pytest==8.3.5\n", encoding="utf-8")
    _git(repository, "add", "requirements.txt"); _git(repository, "commit", "-m", "base")
    _git(repository, "branch", "-M", "main")
    base_sha = _git(repository, "rev-parse", "HEAD")
    _git(repository, "checkout", "-b", "feature/change")
    (repository / "change.py").write_text("VALUE = 1\n", encoding="utf-8")
    (repository / "tests").mkdir()
    (repository / "tests" / "test_change.py").write_text(
        "def test_change():\n    assert True\n", encoding="utf-8")
    _git(repository, "add", "change.py", "tests/test_change.py"); _git(repository, "commit", "-m", "candidate")
    candidate_sha = _git(repository, "rev-parse", "HEAD")
    catalog = tmp_path / "catalog.yaml"
    catalog.write_text("services:\n  demo:\n    repository: repo\n", encoding="utf-8")
    registry = RepositoryRegistry(str(tmp_path), str(catalog), str(tmp_path / "cache"), (), 10)
    _, resolved_base = await registry.resolve_ref("demo", "main")
    _, resolved_candidate = await registry.resolve_ref("demo", "feature/change")
    assert resolved_base == base_sha and len(resolved_base) == 40
    assert resolved_candidate == candidate_sha and len(resolved_candidate) == 40
    with pytest.raises(Exception):
        await registry.resolve_ref("demo", "../../evil")

    # 分支继续前进不会改变已经冻结并持久化在 Run 中的 SHA。
    (repository / "change.py").write_text("VALUE = 2\n", encoding="utf-8")
    _git(repository, "add", "change.py"); _git(repository, "commit", "-m", "branch moved")
    assert _git(repository, "rev-parse", "feature/change") != candidate_sha
    database, user_id = validation_owner()
    run = ValidationRepository(database).create(user_id=user_id, project_id="sre-lab",
        idempotency_key=uuid4().hex, repository="demo", repository_url=None,
        base_ref="main", base_sha=base_sha, candidate_ref="feature/change",
        candidate_sha=candidate_sha, project_type="PYTHON", test_suite_hash="d" * 64,
        run_existing_tests=True, run_uploaded_tests=False, generate_ai_tests=False)
    assert run.candidate_commit_sha == candidate_sha

    service = ValidationService(
        repository,registry,ProjectDetector(),AdapterRegistry("maven:test","python:test"),
        object(),ValidationComparator(),TestFileValidator(),StaticGenerator(),RunnerLimits(),
    )
    test_files = await service.repository_test_files("demo", "feature/change")
    assert test_files.commit_sha == _git(repository, "rev-parse", "feature/change")
    assert test_files.paths == ["tests/test_change.py"]


@pytest.mark.asyncio
async def test_interface_discovery_groups_fastapi_routes_and_detects_changes(tmp_path: Path):
    repository = tmp_path / "repo"; repository.mkdir()
    _git(repository,"init"); _git(repository,"config","user.email","validation@example.test")
    _git(repository,"config","user.name","Validation Test")
    (repository / "requirements.txt").write_text("fastapi\npytest\n",encoding="utf-8")
    (repository / "payment_api.py").write_text(
        "from fastapi import APIRouter\nrouter = APIRouter(prefix='/payments')\n"
        "@router.post('/{payment_id}')\nasync def payment(payment_id: str):\n    return payment_id\n",
        encoding="utf-8",
    )
    _git(repository,"add","."); _git(repository,"commit","-m","base")
    base_sha = _git(repository,"rev-parse","HEAD")
    (repository / "payment_api.py").write_text(
        "from fastapi import APIRouter\nrouter = APIRouter(prefix='/payments')\n"
        "@router.post('/{payment_id}')\nasync def payment(payment_id: str):\n    return payment_id.upper()\n\n"
        "@router.get('/{payment_id}')\nasync def payment_detail(payment_id: str):\n    return payment_id\n",
        encoding="utf-8",
    )
    _git(repository,"add","."); _git(repository,"commit","-m","candidate")
    candidate_sha = _git(repository,"rev-parse","HEAD")
    interfaces = await InterfaceDiscovery().compare(
        "demo",repository,base_sha,candidate_sha,ProjectType.PYTHON,
    )
    assert [(item.http_method,item.route_path,item.change_type) for item in interfaces] == [
        ("GET","/payments/{payment_id}",InterfaceChangeType.ADDED),
        ("POST","/payments/{payment_id}",InterfaceChangeType.MODIFIED),
    ]
    assert all(item.module_name == "payment_api" for item in interfaces)


def test_interface_discovery_parses_spring_class_and_method_mapping():
    source = '''
@RestController
@RequestMapping("/payments")
public class PaymentController {
    @RequestMapping(path = "/{id}", method = RequestMethod.GET)
    public Payment detail(String id) { return service.find(id); }

    @PostMapping
    public Payment create() { return service.create(); }
}
'''
    items = InterfaceDiscovery()._java("demo","src/main/java/api/PaymentController.java",source)
    assert [(item.target.http_method,item.target.route_path,item.target.interface_name) for item in items] == [
        ("GET","/payments/{id}","detail"),("POST","/payments","create"),
    ]


@pytest.mark.asyncio
async def test_one_click_interface_generation_versions_suite_and_starts_same_input_regression(
    tmp_path: Path, monkeypatch,
):
    repository_path = tmp_path / "repo"; repository_path.mkdir()
    _git(repository_path,"init"); _git(repository_path,"config","user.email","validation@example.test")
    _git(repository_path,"config","user.name","Validation Test")
    (repository_path / "requirements.txt").write_text("fastapi\npytest\n",encoding="utf-8")
    (repository_path / "payment_api.py").write_text(
        "from fastapi import APIRouter\nrouter=APIRouter(prefix='/payments')\n",
        encoding="utf-8",
    )
    _git(repository_path,"add","."); _git(repository_path,"commit","-m","base")
    _git(repository_path,"branch","-M","main"); _git(repository_path,"checkout","-b","feature/api")
    (repository_path / "payment_api.py").write_text(
        "from fastapi import APIRouter\nrouter=APIRouter(prefix='/payments')\n"
        "@router.post('')\nasync def create_payment():\n    return {'ok': True}\n",
        encoding="utf-8",
    )
    _git(repository_path,"add","."); _git(repository_path,"commit","-m","add interface")
    catalog = tmp_path / "catalog.yaml"
    catalog.write_text("services:\n  demo:\n    repository: repo\n",encoding="utf-8")
    registry = RepositoryRegistry(str(tmp_path),str(catalog),str(tmp_path / "cache"),(),10)
    database, user_id = validation_owner(); persistence = ValidationRepository(database)
    generator = StaticGenerator([GeneratedTestCase(target_file="tests/test_create_payment.py",
        test_name="test_create_payment",test_code="def test_create_payment():\n    assert True\n",
        reason="cover new interface",covered_change="payment_api.py")])
    service = ValidationService(persistence,registry,ProjectDetector(),
        AdapterRegistry("maven:test","python:test"),StaticRunner(),ValidationComparator(),
        TestFileValidator(),generator,RunnerLimits())
    monkeypatch.setattr(service.manager,"submit",lambda validation_id: None)
    discovered = await service.repository_interfaces("demo","main","feature/api")
    target = next(item for item in discovered.interfaces if item.change_type is InterfaceChangeType.ADDED)
    first = await service.generate_interface_test_suite(user_id,"sre-lab",
        InterfaceTestGenerationRequest(repository="demo",base_ref="main",
            candidate_ref="feature/api",interface_id=target.id),"interface-create")
    assert first.action == "CREATED" and first.version.version == 1
    frozen = persistence.detail(user_id,first.validation.id)
    assert [item.id for item in frozen.test_suite_versions] == [first.version.id]
    assert frozen.base_commit_sha == discovered.base_commit_sha
    assert frozen.candidate_commit_sha == discovered.candidate_commit_sha

    (repository_path / "payment_api.py").write_text(
        "from fastapi import APIRouter\nrouter=APIRouter(prefix='/payments')\n"
        "@router.post('')\nasync def create_payment():\n    return {'ok': True, 'version': 2}\n",
        encoding="utf-8",
    )
    _git(repository_path,"add","."); _git(repository_path,"commit","-m","modify interface")
    generator.generated = [GeneratedTestCase(target_file="tests/test_create_payment.py",
        test_name="test_create_payment_v2",test_code="def test_create_payment_v2():\n    assert True\n",
        reason="cover modified interface",covered_change="payment_api.py")]
    second = await service.generate_interface_test_suite(user_id,"sre-lab",
        InterfaceTestGenerationRequest(repository="demo",base_ref="main",
            candidate_ref="feature/api",interface_id=target.id),"interface-update")
    assert second.action == "VERSIONED" and second.suite.id == first.suite.id
    assert second.version.version == 2
    assert second.suite.versions[1].files[0].content == "def test_create_payment():\n    assert True\n"
    retried = await service.generate_interface_test_suite(user_id,"sre-lab",
        InterfaceTestGenerationRequest(repository="demo",base_ref="main",
            candidate_ref="feature/api",interface_id=target.id),"interface-update")
    assert retried.version.id == second.version.id
    assert persistence.get_test_suite(user_id,first.suite.id).latest_version == 2


def test_startup_recovery_prevents_permanent_running_state():
    database, user_id = validation_owner()
    repository = ValidationRepository(database)
    run = repository.create(user_id=user_id, project_id="sre-lab", idempotency_key=uuid4().hex,
        repository="demo", repository_url=None, base_ref="main", base_sha="a" * 40,
        candidate_ref="feature", candidate_sha="b" * 40, project_type="PYTHON",
        test_suite_hash="e" * 64, run_existing_tests=True, run_uploaded_tests=False,
        generate_ai_tests=False)
    assert repository.begin(run.id)
    repository.update_run(run.id, status="RUNNING")
    assert repository.fail_incomplete_on_startup() == 1
    recovered = repository.get(user_id, run.id)
    assert recovered.status.value == "FAILED"
    assert "process restart" in recovered.error_message


class RecordingRegistry:
    def __init__(self, path: Path): self.path, self.commits = path, []
    async def resolve(self, repository, commit): self.commits.append(commit); return self.path


class StaticGenerator:
    def __init__(self, generated=()): self.generated = list(generated)
    async def generate(self, **kwargs): return self.generated
    async def generate_interface_tests(self, **kwargs): return self.generated


class StaticRunner:
    def __init__(self, results=None, delay=0): self.results, self.delay = results or {}, delay
    async def run(self, validation_id, side, repository_path, commit_sha, adapter, limits,
                  extra_files, source_by_test_name, exclude_repository_tests=False):
        import asyncio
        if self.delay: await asyncio.sleep(self.delay)
        tests = self.results.get(side, [CaseResult(suite="suite", name="stable", status=CaseStatus.PASSED)])
        failed = any(item.status in {CaseStatus.FAILED,CaseStatus.ERROR} for item in tests)
        return RunnerResult(build_status=StageStatus.PASSED,
            test_status=StageStatus.FAILED if failed else StageStatus.PASSED,
            exit_code=1 if failed else 0, duration_ms=5, tests=tests)


def prepared_run(repository, user_id, *, ai=False):
    return repository.create(user_id=user_id, project_id="sre-lab", idempotency_key=uuid4().hex,
        repository="demo", repository_url=None, base_ref="main", base_sha="a" * 40,
        candidate_ref="feature", candidate_sha="b" * 40, project_type="PYTHON",
        test_suite_hash="f" * 64, run_existing_tests=True, run_uploaded_tests=False,
        generate_ai_tests=ai)


def orchestration_service(repository, registry, runner, generator, *, timeout=2):
    return ValidationService(repository, registry, object(), AdapterRegistry("maven:test", "python:test"),
        runner, ValidationComparator(), TestFileValidator(), generator,
        RunnerLimits(overall_timeout_seconds=timeout))


@pytest.mark.asyncio
async def test_no_regression_completes_and_frozen_candidate_sha_is_executed(tmp_path, monkeypatch):
    database, user_id = validation_owner(); repository = ValidationRepository(database)
    run = prepared_run(repository, user_id)
    registry = RecordingRegistry(tmp_path); service = orchestration_service(repository, registry, StaticRunner(), StaticGenerator())
    async def diff(*args): return [ ], ""
    async def samples(*args): return {}
    monkeypatch.setattr(service, "_diff", diff); monkeypatch.setattr(service, "_test_samples", samples)
    await service.execute(run.id)
    detail = repository.detail(user_id, run.id)
    assert detail.status.value == "COMPLETED"
    assert detail.summary == "NO REGRESSION DETECTED"
    assert detail.regression_count == 0
    assert registry.commits == ["a" * 40, "b" * 40]
    assert {item.environment.test_suite_hash for item in detail.executions} == {detail.test_suite_hash}


@pytest.mark.asyncio
async def test_orchestration_reports_main_to_fix_as_possible_fix(tmp_path, monkeypatch):
    database, user_id = validation_owner(); repository = ValidationRepository(database)
    run = prepared_run(repository, user_id)
    runner = StaticRunner({
        "BASE":[CaseResult(suite="suite",name="known_bug",status=CaseStatus.FAILED)],
        "CANDIDATE":[CaseResult(suite="suite",name="known_bug",status=CaseStatus.PASSED)],
    })
    service = orchestration_service(
        repository,RecordingRegistry(tmp_path),runner,StaticGenerator(),
    )
    async def diff(*args): return [ ], ""
    monkeypatch.setattr(service,"_diff",diff)
    await service.execute(run.id)
    detail = repository.detail(user_id,run.id)
    assert detail.status.value == "COMPLETED"
    assert detail.regression_count == 0 and detail.possible_fix_count == 1
    assert detail.regressions[0].classification is RegressionClassification.POSSIBLE_FIX


@pytest.mark.asyncio
async def test_ai_invalid_is_recorded_without_crashing_validation(tmp_path, monkeypatch):
    from app.validation.models import GeneratedTestCase
    database, user_id = validation_owner(); repository = ValidationRepository(database)
    run = prepared_run(repository, user_id, ai=True)
    invalid = GeneratedTestCase(target_file="tests/test_generated.py", test_name="test_generated",
        test_code="import subprocess\ndef test_generated(): subprocess.run(['bad'])",
        reason="cover change", covered_change="change.py")
    service = orchestration_service(repository, RecordingRegistry(tmp_path), StaticRunner(), StaticGenerator([invalid]))
    async def diff(*args): return [], "diff"
    async def samples(*args): return {}
    monkeypatch.setattr(service, "_diff", diff); monkeypatch.setattr(service, "_test_samples", samples)
    await service.execute(run.id)
    detail = repository.detail(user_id, run.id)
    assert detail.status.value == "COMPLETED"
    assert any(item.classification is RegressionClassification.AI_TEST_INVALID for item in detail.regressions)
    assert repository.generated_tests(run.id)[0]["valid"] == 0


@pytest.mark.asyncio
async def test_runner_overall_timeout_finishes_inconclusive(tmp_path, monkeypatch):
    database, user_id = validation_owner(); repository = ValidationRepository(database)
    run = prepared_run(repository, user_id)
    service = orchestration_service(repository, RecordingRegistry(tmp_path), StaticRunner(delay=.1), StaticGenerator(), timeout=.01)
    async def diff(*args): return [], ""
    monkeypatch.setattr(service, "_diff", diff)
    await service.execute(run.id)
    detail = repository.detail(user_id, run.id)
    assert detail.status.value == "FAILED"
    assert detail.summary == "COMPARISON INCONCLUSIVE"
    assert all(item.status is ExecutionStatus.TIMEOUT for item in detail.executions)


class RecordingDiagnosisManager:
    def __init__(self): self.ids = []
    def submit(self, diagnosis_id, **kwargs): self.ids.append(diagnosis_id)


@pytest.mark.asyncio
async def test_diagnose_regression_creates_session_with_ci_evidence(tmp_path):
    database, user_id = validation_owner(); repository = ValidationRepository(database)
    run = prepared_run(repository, user_id)
    repository.replace_regressions(run.id, [RegressionResult(test_identity="x", suite="payments",
        name="timeout", source=CaseSource.REPOSITORY,
        classification=RegressionClassification.NEW_REGRESSION,
        confidence=ComparisonConfidence.HIGH, base_status=CaseStatus.PASSED,
        candidate_status=CaseStatus.FAILED, related_changes=["payment.py"])])
    repository.update_run(run.id, status="FAILED", summary="REGRESSION FOUND (1)",
        changed_files=[], regression_count=1, finished=True)
    diagnosis_repository = DiagnosisRepository(database)
    manager = RecordingDiagnosisManager()
    service = orchestration_service(repository, RecordingRegistry(tmp_path), StaticRunner(), StaticGenerator())
    service.diagnosis_repository = diagnosis_repository
    service.diagnosis_service = DiagnosisService(diagnosis_repository, ConversationService(database))
    service.diagnosis_manager = manager
    diagnosis_id = await service.diagnose(user_id, "sre-lab", run.id)
    diagnosis = diagnosis_repository.get(user_id, diagnosis_id)
    assert run.base_commit_sha in diagnosis.question
    assert run.candidate_commit_sha in diagnosis.question
    assert "timeout" in diagnosis.question and "payment.py" in diagnosis.question
    evidence = diagnosis_repository.list_evidence(user_id, diagnosis_id)
    assert evidence[0].source_type == "VALIDATION"
    assert manager.ids == [diagnosis_id]


@pytest.mark.asyncio
async def test_docker_runner_uses_fixed_hardened_argv(tmp_path, monkeypatch):
    calls = []
    class Completed:
        returncode = 0; stdout = b"ok"; stderr = b""
    def fake_run(argv, **kwargs): calls.append((argv, kwargs)); return Completed()
    monkeypatch.setattr(subprocess, "run", fake_run)
    runner = DockerValidationRunner(tmp_path / "work", tmp_path / "artifacts")
    workspace = tmp_path / "workspace"; workspace.mkdir()
    result = await runner._docker_stage("validation", "BASE", "test", workspace,
        "python:test", ("python", "-m", "pytest"), RunnerLimits(), 5)
    argv = calls[0][0]
    assert result.returncode == 0 and calls[0][1]["timeout"] == 5
    assert argv[:2] == ["docker", "run"]
    for option in ("--network", "none", "--cpus", "--memory", "--pids-limit",
                   "--cap-drop", "ALL", "--read-only", "--tmpfs"):
        assert option in argv
    assert argv[-3:] == ["python", "-m", "pytest"]


@pytest.mark.asyncio
async def test_docker_timeout_removes_only_owned_container(tmp_path, monkeypatch):
    calls = []
    def fake_run(argv, **kwargs):
        calls.append(argv)
        if argv[:2] == ["docker", "run"]:
            raise subprocess.TimeoutExpired(argv, kwargs["timeout"], output=b"partial")
        class Completed: returncode = 0; stdout = b""; stderr = b""
        return Completed()
    monkeypatch.setattr(subprocess, "run", fake_run)
    runner = DockerValidationRunner(tmp_path / "work", tmp_path / "artifacts")
    workspace = tmp_path / "workspace"; workspace.mkdir()
    result = await runner._docker_stage("validation", "BASE", "test", workspace,
        "python:test", ("python", "-m", "pytest"), RunnerLimits(), .01)
    name = calls[0][calls[0].index("--name") + 1]
    assert result.timed_out
    assert calls[1] == ["docker", "rm", "-f", name]
