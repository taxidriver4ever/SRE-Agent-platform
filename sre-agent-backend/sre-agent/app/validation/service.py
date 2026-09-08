"""Pre-Merge Validation 创建、隔离执行、比较及 Diagnosis 联动。"""

from __future__ import annotations

import asyncio
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from app.core.process import run_fixed_command
from app.diagnosis.models import DiagnosisEvidence, DiagnosisTarget, DiagnosisTargetType
from app.diagnosis.schemas import DiagnosisCreateRequest
from app.diagnosis.models import DiagnosisTriggerType
from app.validation.ai_tests import AITestGenerator, TestFileValidationError, TestFileValidator
from app.validation.comparator import ValidationComparator
from app.validation.interfaces import InterfaceDiscovery
from app.validation.models import (
    ChangedFile, ComparisonConfidence, EnvironmentSpec, ExecutionSide, ExecutionStatus,
    InterfaceChangeType, InterfaceTestGenerationRequest, InterfaceTestGenerationResponse,
    ManagedTestSuite, ManagedTestSuiteCreateRequest, ManagedTestSuiteMetadataRequest,
    ManagedTestSuiteVersionCreateRequest, ProjectType, RegressionClassification,
    RegressionResult, RepositoryInterfacesResponse, RepositoryTestFilesResponse, TestSource,
    UploadedTestFile, ValidationCreateRequest, ValidationCreatedResponse, ValidationEvidence,
    ValidationRun,
)
from app.validation.project import AdapterRegistry, ProjectDetector
from app.validation.repository import ValidationRepository
from app.validation.runner import RunnerLimits, ValidationRunner


class ValidationRequestError(ValueError):
    pass


class ValidationExecutionManager:
    def __init__(self, service: "ValidationService") -> None:
        self.service = service
        self.tasks: dict[str, asyncio.Task[None]] = {}
        self._closing = False

    def submit(self, validation_id: str) -> asyncio.Task[None] | None:
        if self._closing:
            return None
        current = self.tasks.get(validation_id)
        if current and not current.done():
            return current
        task = asyncio.create_task(self.service.execute(validation_id), name=f"validation-{validation_id}")
        self.tasks[validation_id] = task
        def forget(finished: asyncio.Task[None]) -> None:
            if self.tasks.get(validation_id) is finished:
                self.tasks.pop(validation_id, None)
        task.add_done_callback(forget)
        return task

    def cancel(self, user_id: str, validation_id: str) -> bool:
        if not self.service.repository.cancel(user_id, validation_id):
            return False
        task = self.tasks.get(validation_id)
        if task and not task.done():
            task.cancel()
        return True

    async def shutdown(self) -> None:
        self._closing = True
        tasks = list(self.tasks.values())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)


class ValidationService:
    def __init__(
        self, repository: ValidationRepository, registry, detector: ProjectDetector,
        adapters: AdapterRegistry, runner: ValidationRunner, comparator: ValidationComparator,
        validator: TestFileValidator, ai_generator: AITestGenerator, limits: RunnerLimits,
        diagnosis_service=None, diagnosis_repository=None, diagnosis_manager=None,
        code_state_service=None, code_state_repository=None,
        interface_discovery: InterfaceDiscovery | None = None,
    ) -> None:
        self.repository = repository
        self.registry = registry
        self.detector = detector
        self.adapters = adapters
        self.runner = runner
        self.comparator = comparator
        self.validator = validator
        self.ai_generator = ai_generator
        self.limits = limits
        self.diagnosis_service = diagnosis_service
        self.diagnosis_repository = diagnosis_repository
        self.diagnosis_manager = diagnosis_manager
        self.code_state_service = code_state_service
        self.code_state_repository = code_state_repository
        self.interface_discovery = interface_discovery or InterfaceDiscovery()
        self.manager = ValidationExecutionManager(self)

    async def create(self, user_id: str, project_id: str, body: ValidationCreateRequest,
                     idempotency_key: str | None,
                     resolved_refs: tuple[tuple[Path,str],tuple[Path,str]] | None = None
                     ) -> ValidationRun:
        if resolved_refs is None:
            try:
                base, candidate = await asyncio.gather(
                    self.registry.resolve_ref(body.repository, body.base_ref),
                    self.registry.resolve_ref(body.repository, body.candidate_ref),
                )
            except Exception as exc:
                raise ValidationRequestError(str(exc)) from exc
        else:
            base, candidate = resolved_refs
        if base[0].resolve() != candidate[0].resolve():
            raise ValidationRequestError("base and candidate must belong to the same authorized repository")
        if base[1] == candidate[1]:
            raise ValidationRequestError("base and candidate resolve to the same commit")
        base_type, candidate_type = await asyncio.gather(
            self.detector.detect(base[0], base[1]), self.detector.detect(candidate[0], candidate[1]),
        )
        if base_type != candidate_type:
            raise ValidationRequestError("base and candidate project types do not match")
        if self.adapters.get(base_type) is None:
            raise ValidationRequestError(f"project type {base_type.value} is detected but not supported")
        try:
            managed_versions = self.repository.resolve_test_suite_versions(
                user_id, body.repository, base_type, body.test_suite_version_ids,
            )
        except (KeyError, ValueError) as exc:
            raise ValidationRequestError(str(exc)) from exc
        if len({item.suite_id for item in managed_versions}) != len(managed_versions):
            raise ValidationRequestError("select at most one version from each managed test suite")
        managed_files: dict[str, str] = {}
        for version in managed_versions:
            for item in version.files:
                if item.path in managed_files and managed_files[item.path] != item.content:
                    raise ValidationRequestError(f"selected test suites conflict at path: {item.path}")
                managed_files[item.path] = item.content
        uploaded: dict[str, str] = {}
        if body.run_uploaded_tests:
            try:
                uploaded = self.validator.validate_uploaded(base_type, body.uploaded_tests)
            except TestFileValidationError as exc:
                raise ValidationRequestError(str(exc)) from exc
        for path, content in uploaded.items():
            if path in managed_files and managed_files[path] != content:
                raise ValidationRequestError(f"uploaded test conflicts with managed test suite at path: {path}")
        combined_user_tests = {**managed_files, **uploaded}
        if combined_user_tests:
            try:
                combined_user_tests = self.validator.validate_uploaded(
                    base_type,
                    [UploadedTestFile(path=path,content=content)
                     for path,content in combined_user_tests.items()],
                )
            except TestFileValidationError as exc:
                raise ValidationRequestError(
                    f"combined managed/uploaded test suite is invalid: {exc}"
                ) from exc
        if body.ai_reference_test_paths:
            available = set(await self._repository_test_paths(base[0], candidate[1], base_type))
            missing = [path for path in body.ai_reference_test_paths if path not in available]
            if missing:
                raise ValidationRequestError(
                    f"AI reference tests are not present in frozen candidate commit: {', '.join(missing)}"
                )
        seed = json.dumps({
            "adapter": self.adapters.VERSION, "project_type": base_type.value,
            "existing": body.run_existing_tests, "uploaded": combined_user_tests,
            "managed_versions": body.test_suite_version_ids,
            "ai": body.generate_ai_tests, "ai_references": body.ai_reference_test_paths,
        }, ensure_ascii=False, sort_keys=True)
        suite_hash = hashlib.sha256(seed.encode()).hexdigest()
        key = (idempotency_key or uuid4().hex)[:128]
        run = self.repository.create(
            user_id=user_id, project_id=project_id, idempotency_key=key,
            repository=body.repository, repository_url=self.registry.remote_url(body.repository),
            base_ref=body.base_ref, base_sha=base[1], candidate_ref=body.candidate_ref,
            candidate_sha=candidate[1], project_type=base_type.value, test_suite_hash=suite_hash,
            run_existing_tests=body.run_existing_tests,
            run_uploaded_tests=bool(combined_user_tests),
            generate_ai_tests=body.generate_ai_tests,
        )
        if not self.repository.ensure_request_fingerprint(run.id, suite_hash):
            raise ValidationRequestError("Idempotency-Key was already used for a different validation request")
        if (
            run.repository != body.repository or run.base_commit_sha != base[1]
            or run.candidate_commit_sha != candidate[1]
            or run.run_existing_tests != body.run_existing_tests
            or run.run_uploaded_tests != bool(combined_user_tests)
            or run.generate_ai_tests != body.generate_ai_tests
        ):
            raise ValidationRequestError("Idempotency-Key was already used for a different validation request")
        existing_version_ids = [item.id for item in self.repository.run_test_suite_versions(user_id, run.id)]
        existing_references = self.repository.ai_reference_paths(run.id)
        if existing_version_ids and existing_version_ids != body.test_suite_version_ids:
            raise ValidationRequestError("Idempotency-Key was already used with different test suite versions")
        if existing_references and existing_references != body.ai_reference_test_paths:
            raise ValidationRequestError("Idempotency-Key was already used with different AI references")
        if run.status.value == "PENDING":
            if combined_user_tests:
                self.repository.store_uploaded_tests(user_id, run.id, combined_user_tests)
            self.repository.bind_test_suite_versions(run.id, managed_versions)
            self.repository.bind_ai_reference_paths(run.id, body.ai_reference_test_paths)
        self.manager.submit(run.id)
        return run

    def create_test_suite(self, user_id: str,
                          body: ManagedTestSuiteCreateRequest) -> ManagedTestSuite:
        if body.repository not in self.registry.repositories():
            raise ValidationRequestError("unknown or unauthorized repository")
        try:
            files = self.validator.validate_uploaded(body.project_type, body.files)
            target = body.target.model_copy(update={
                "id":body.target.id or hashlib.sha256(json.dumps({
                    "repository":body.repository,"module":body.target.module_name,
                    "source":body.target.source_file,"symbol":body.target.symbol,
                    "method":body.target.http_method,"route":body.target.route_path,
                    "interface":body.target.interface_name,
                },sort_keys=True).encode()).hexdigest(),
            })
            return self.repository.create_test_suite(
                user_id=user_id,repository=body.repository,name=body.name.strip(),
                description=body.description.strip(),project_type=body.project_type,
                change_note=body.change_note.strip(),files=files,target=target,
            )
        except TestFileValidationError as exc:
            raise ValidationRequestError(str(exc)) from exc
        except Exception as exc:
            if "Duplicate" in str(exc) or "uk_validation_test_suite_name" in str(exc):
                raise ValidationRequestError("test suite name already exists for this repository") from exc
            raise

    def add_test_suite_version(self, user_id: str, suite_id: str,
                               body: ManagedTestSuiteVersionCreateRequest) -> ManagedTestSuite:
        suite = self.repository.get_test_suite(user_id, suite_id)
        if suite is None:
            raise KeyError("test suite not found")
        try:
            files = self.validator.validate_uploaded(suite.project_type, body.files)
            return self.repository.add_test_suite_version(
                user_id=user_id,suite_id=suite_id,change_note=body.change_note.strip(),files=files,
            )
        except TestFileValidationError as exc:
            raise ValidationRequestError(str(exc)) from exc

    def update_test_suite_metadata(self, user_id: str, suite_id: str,
                                   body: ManagedTestSuiteMetadataRequest) -> ManagedTestSuite:
        try:
            return self.repository.update_test_suite_metadata(
                user_id=user_id,suite_id=suite_id,name=body.name.strip(),
                description=body.description.strip(),
            )
        except Exception as exc:
            if "Duplicate" in str(exc) or "uk_validation_test_suite_name" in str(exc):
                raise ValidationRequestError("test suite name already exists for this repository") from exc
            raise

    async def repository_test_files(self, repository: str,
                                    branch: str) -> RepositoryTestFilesResponse:
        try:
            path, commit = await self.registry.resolve_ref(repository, branch)
            project_type = await self.detector.detect(path, commit)
            paths = await self._repository_test_paths(path, commit, project_type)
        except Exception as exc:
            raise ValidationRequestError(str(exc)) from exc
        return RepositoryTestFilesResponse(
            repository=repository,ref=branch,commit_sha=commit,
            project_type=project_type,paths=paths,
        )

    async def repository_interfaces(self, repository: str, base_ref: str,
                                    candidate_ref: str) -> RepositoryInterfacesResponse:
        if base_ref == candidate_ref:
            raise ValidationRequestError("base_ref and candidate_ref must be different")
        try:
            base, candidate = await asyncio.gather(
                self.registry.resolve_ref(repository,base_ref),
                self.registry.resolve_ref(repository,candidate_ref),
            )
            if base[0].resolve() != candidate[0].resolve():
                raise ValidationRequestError("base and candidate must belong to the same authorized repository")
            base_type, candidate_type = await asyncio.gather(
                self.detector.detect(base[0],base[1]),
                self.detector.detect(candidate[0],candidate[1]),
            )
            if base_type != candidate_type:
                raise ValidationRequestError("base and candidate project types do not match")
            if base_type not in {ProjectType.PYTHON,ProjectType.MAVEN}:
                raise ValidationRequestError("interface discovery currently supports PYTHON and MAVEN")
            interfaces = await self.interface_discovery.compare(
                repository,base[0],base[1],candidate[1],base_type,
            )
        except ValidationRequestError:
            raise
        except Exception as exc:
            raise ValidationRequestError(str(exc)) from exc
        return RepositoryInterfacesResponse(
            repository=repository,base_ref=base_ref,candidate_ref=candidate_ref,
            base_commit_sha=base[1],candidate_commit_sha=candidate[1],
            project_type=base_type,interfaces=interfaces,
        )

    async def generate_interface_test_suite(
        self, user_id: str, project_id: str, body: InterfaceTestGenerationRequest,
        idempotency_key: str | None,
    ) -> InterfaceTestGenerationResponse:
        operation_key = (idempotency_key or uuid4().hex)[:128]
        fingerprint = hashlib.sha256(json.dumps({
            "repository":body.repository,"base_ref":body.base_ref,
            "candidate_ref":body.candidate_ref,"interface_id":body.interface_id,
            "suite_id":body.suite_id,"run_existing_tests":body.run_existing_tests,
        },sort_keys=True).encode()).hexdigest()
        operation = self.repository.begin_interface_generation(user_id,operation_key,fingerprint)
        if str(operation["request_fingerprint"]) != fingerprint:
            raise ValidationRequestError("Idempotency-Key was already used for a different interface request")
        if not operation["_created"]:
            if str(operation["status"]) == "COMPLETED":
                return self._completed_interface_generation(user_id,operation)
            if str(operation["status"]) == "FAILED":
                raise ValidationRequestError(
                    f"the idempotent interface generation already failed: {operation.get('error_message') or 'unknown error'}"
                )
            raise ValidationRequestError("the idempotent interface generation is already in progress")
        try:
            base, candidate = await asyncio.gather(
                self.registry.resolve_ref(body.repository,body.base_ref),
                self.registry.resolve_ref(body.repository,body.candidate_ref),
            )
            if base[0].resolve() != candidate[0].resolve():
                raise ValidationRequestError("base and candidate must belong to the same authorized repository")
            base_type, candidate_type = await asyncio.gather(
                self.detector.detect(base[0],base[1]),self.detector.detect(candidate[0],candidate[1]),
            )
            if base_type != candidate_type or base_type not in {ProjectType.PYTHON,ProjectType.MAVEN}:
                raise ValidationRequestError("interface test generation requires matching PYTHON or MAVEN commits")
            interfaces = await self.interface_discovery.compare(
                body.repository,base[0],base[1],candidate[1],base_type,
            )
            target = next((item for item in interfaces if item.id == body.interface_id),None)
            if target is None:
                raise ValidationRequestError("interface is not present in the frozen comparison")
            if target.change_type is InterfaceChangeType.REMOVED or not target.candidate_exists:
                raise ValidationRequestError("removed interfaces cannot receive candidate tests")
        except ValidationRequestError as exc:
            self.repository.fail_interface_generation(str(operation["id"]),str(exc))
            raise
        except Exception as exc:
            self.repository.fail_interface_generation(str(operation["id"]),str(exc))
            raise ValidationRequestError(str(exc)) from exc

        try:
            suite = (self.repository.get_test_suite(user_id,body.suite_id)
                     if body.suite_id else self.repository.find_test_suite_by_interface(
                         user_id,body.repository,target.id,
                     ))
            if body.suite_id and suite is None:
                raise ValidationRequestError("test suite not found")
            if suite is not None:
                if suite.archived or suite.repository != body.repository or suite.project_type != base_type:
                    raise ValidationRequestError("test suite is archived or does not match the repository/project")
                if suite.target.id != target.id:
                    raise ValidationRequestError("test suite belongs to a different module/interface")
            previous = ({item.path:item.content for item in suite.versions[0].files}
                        if suite and suite.versions else {})
            _, diff = await self._diff(base[0],base[1],candidate[1])
            candidate_source = await self.interface_discovery.source(candidate[0],candidate[1],target)
            generated = await self.ai_generator.generate_interface_tests(
                project_type=base_type,target=target,diff=diff,
                candidate_source=candidate_source,existing_tests=previous,
            )
            merged = dict(previous)
            accepted = []
            for item in generated:
                path = self.validator.validate_generated(base_type,item)
                merged[path] = item.test_code
                accepted.append((path,item))
            if not accepted:
                raise ValidationRequestError("AI did not return any valid interface test source")
            normalized = self.validator.validate_uploaded(
                base_type,[UploadedTestFile(path=path,content=content) for path,content in merged.items()],
            )
            if suite is None:
                label = f"{target.module_name} · {target.http_method} {target.route_path or target.interface_name}"
                suite = self.repository.create_test_suite(
                    user_id=user_id,repository=body.repository,name=label[:145] + f" [{target.id[:8]}]",
                    description=f"接口 {target.interface_name} 的 AI 辅助回归测试集",
                    project_type=base_type,change_note=f"generated from candidate {candidate[1]}",
                    files=normalized,target=target,
                )
                action = "CREATED"
            else:
                suite = self.repository.add_test_suite_version(
                    user_id=user_id,suite_id=suite.id,
                    change_note=f"regenerated for candidate {candidate[1]}",files=normalized,
                )
                action = "VERSIONED"
            version = suite.versions[0]
            validation_body = ValidationCreateRequest(
                repository=body.repository,base_ref=body.base_ref,candidate_ref=body.candidate_ref,
                run_existing_tests=body.run_existing_tests,run_uploaded_tests=False,
                generate_ai_tests=False,test_suite_version_ids=[version.id],
            )
            run = await self.create(
                user_id,project_id,validation_body,f"interface-validation:{operation_key}"[:128],
                resolved_refs=(base,candidate),
            )
            for path,item in accepted:
                self.repository.store_generated_test(
                    run.id,path=path,name=item.test_name,code=item.test_code,reason=item.reason,
                    covered_change=item.covered_change,valid=True,error=None,
                )
            self.repository.complete_interface_generation(
                str(operation["id"]),suite_id=suite.id,version_id=version.id,
                validation_id=run.id,action=action,
            )
            return InterfaceTestGenerationResponse(
                suite=suite,version=version,action=action,
                validation=ValidationCreatedResponse(
                    id=run.id,status=run.status,events_url=f"/api/validations/{run.id}/events",
                    detail_url=f"/api/validations/{run.id}",
                ),
            )
        except Exception as exc:
            self.repository.fail_interface_generation(str(operation["id"]),str(exc))
            if isinstance(exc,(ValidationRequestError,TestFileValidationError)):
                raise ValidationRequestError(str(exc)) from exc
            raise

    def _completed_interface_generation(self, user_id: str,
                                        operation: dict) -> InterfaceTestGenerationResponse:
        suite = self.repository.get_test_suite(user_id,str(operation["suite_id"]))
        run = self.repository.get(user_id,str(operation["validation_id"]))
        if suite is None or run is None:
            raise ValidationRequestError("completed interface generation references missing resources")
        version = next((item for item in suite.versions
                        if item.id == str(operation["version_id"])),None)
        if version is None:
            raise ValidationRequestError("completed interface generation version is missing")
        return InterfaceTestGenerationResponse(
            suite=suite,version=version,action=str(operation["action"]),
            validation=ValidationCreatedResponse(
                id=run.id,status=run.status,events_url=f"/api/validations/{run.id}/events",
                detail_url=f"/api/validations/{run.id}",
            ),
        )

    async def execute(self, validation_id: str) -> None:
        if not self.repository.begin(validation_id):
            return
        try:
            run = self.repository.get_unscoped(validation_id)
            if run is None:
                return
            # 分支即便在排队期间移动，也始终执行创建时冻结的 Candidate SHA。
            base_path, repo_path = await asyncio.gather(
                self.registry.resolve(run.repository, run.base_commit_sha),
                self.registry.resolve(run.repository, run.candidate_commit_sha),
            )
            if base_path.resolve() != repo_path.resolve():
                raise RuntimeError("frozen commits resolved to different repository mirrors")
            changed, diff = await self._diff(repo_path, run.base_commit_sha, run.candidate_commit_sha)
            self.repository.update_run(validation_id, changed_files=changed)
            self._evidence(validation_id, "GIT", "Frozen Git diff",
                           f"{len(changed)} changed files between immutable commits",
                           {"base": run.base_commit_sha, "candidate": run.candidate_commit_sha,
                            "changed_files": [item.model_dump(mode="json") for item in changed],
                            "diff": diff[:30000]})
            adapter = self.adapters.get(run.project_type)
            if adapter is None:
                raise RuntimeError(f"unsupported project type: {run.project_type.value}")
            extra_files = self.repository.uploaded_tests(validation_id)
            sources = self._test_sources(extra_files, TestSource.UPLOADED)
            invalid_ai: list[RegressionResult] = []
            if run.generate_ai_tests:
                reference_paths = self.repository.ai_reference_paths(validation_id)
                code_state: list[dict] = []
                if self.code_state_service and self.code_state_repository:
                    try:
                        await self.code_state_service.ensure(run.repository, run.candidate_commit_sha)
                        query = Path(changed[0].path).stem if changed else ""
                        code_state = self.code_state_repository.search(run.repository, query, limit=20)
                        self._evidence(validation_id, "CODE_STATE", "Candidate code navigation",
                            f"{len(code_state)} bounded symbols at candidate commit",
                            {"commit_sha": run.candidate_commit_sha, "components": code_state})
                    except Exception as exc:
                        self._evidence(validation_id, "CODE_STATE", "Candidate code navigation unavailable",
                            str(exc)[:1000], {"commit_sha": run.candidate_commit_sha})
                generated = await self.ai_generator.generate(
                    project_type=run.project_type, diff=diff,
                    changed_files=[item.path for item in changed], code_state=code_state,
                    test_samples=await self._test_samples(
                        repo_path, run.candidate_commit_sha, reference_paths or None,
                    ),
                )
                self._evidence(
                    validation_id,"AI_TEST","AI reference tests",
                    (f"{len(reference_paths)} user-selected samples" if reference_paths
                     else "up to 3 automatically selected candidate samples"),
                    {"commit_sha":run.candidate_commit_sha,"paths":reference_paths,
                     "selection":"USER" if reference_paths else "AUTO"},
                )
                for item in generated:
                    try:
                        path = self.validator.validate_generated(run.project_type, item)
                        if path in extra_files:
                            raise TestFileValidationError(
                                "AI generated test conflicts with an uploaded or managed test path"
                            )
                        extra_files[path] = item.test_code
                        sources[item.test_name] = TestSource.AI_GENERATED
                        sources.update(self._test_sources({path: item.test_code}, TestSource.AI_GENERATED))
                        self.repository.store_generated_test(validation_id, path=path, name=item.test_name,
                            code=item.test_code, reason=item.reason, covered_change=item.covered_change,
                            valid=True, error=None)
                    except TestFileValidationError as exc:
                        path = item.target_file[:500]
                        self.repository.store_generated_test(validation_id, path=path, name=item.test_name,
                            code=item.test_code, reason=item.reason, covered_change=item.covered_change,
                            valid=False, error=str(exc))
                        invalid_ai.append(RegressionResult(
                            test_identity=f"AI_INVALID:{hashlib.sha256((path+item.test_name).encode()).hexdigest()}",
                            suite="AI_GENERATED", name=item.test_name, source=TestSource.AI_GENERATED,
                            classification=RegressionClassification.AI_TEST_INVALID,
                            confidence=ComparisonConfidence.LOW, message=str(exc),
                            related_changes=[item.covered_change],
                        ))
            final_suite_hash = hashlib.sha256(json.dumps({
                "adapter": adapter.version, "commands": [adapter.build_command, adapter.test_command],
                "run_existing_tests": run.run_existing_tests, "files": extra_files,
            }, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
            self.repository.set_test_suite_hash(validation_id, final_suite_hash)
            environment = self._environment(adapter, final_suite_hash)
            self.repository.update_run(validation_id, status="RUNNING",
                                       environment_fingerprint=environment.environment_fingerprint)
            base_exec, candidate_exec = await asyncio.gather(
                self._run_side(run, ExecutionSide.BASE, run.base_commit_sha, repo_path, adapter,
                               environment, extra_files, sources),
                self._run_side(run, ExecutionSide.CANDIDATE, run.candidate_commit_sha, repo_path, adapter,
                               environment, extra_files, sources),
            )
            self.repository.update_run(validation_id, status="COMPARING")
            results, confidence = self.comparator.compare(
                base_exec, candidate_exec, [item.path for item in changed],
            )
            results.extend(invalid_ai)
            self.repository.replace_regressions(validation_id, results)
            regressions = sum(item.classification in {
                RegressionClassification.NEW_REGRESSION,
                RegressionClassification.BUILD_REGRESSION,
                RegressionClassification.AI_CONFIRMED_REGRESSION,
            } for item in results)
            existing = sum(item.classification is RegressionClassification.EXISTING_FAILURE for item in results)
            fixes = sum(item.classification is RegressionClassification.POSSIBLE_FIX for item in results)
            inconclusive = any(item.classification is RegressionClassification.COMPARISON_INCONCLUSIVE for item in results)
            summary = ("COMPARISON INCONCLUSIVE" if inconclusive else
                       f"REGRESSION FOUND ({regressions})" if regressions else "NO REGRESSION DETECTED")
            final_status = "FAILED" if regressions or inconclusive else "COMPLETED"
            self.repository.update_run(validation_id, status=final_status, summary=summary,
                confidence=confidence.value, regression_count=regressions,
                existing_failure_count=existing, possible_fix_count=fixes, finished=True)
            self._evidence(validation_id, "COMPARISON", "Base / Candidate comparison", summary,
                           {"confidence": confidence.value, "regressions": [x.model_dump(mode="json") for x in results]})
        except asyncio.CancelledError:
            current = self.repository.get_unscoped(validation_id)
            if current is None or current.status.value != "CANCELLED":
                self.repository.update_run(validation_id, status="FAILED",
                    error_message="validation interrupted during process shutdown", finished=True)
            raise
        except Exception as exc:
            self.repository.update_run(validation_id, status="FAILED",
                summary="VALIDATION EXECUTION FAILED", error_message=f"{type(exc).__name__}: {exc}", finished=True)

    async def _run_side(self, run: ValidationRun, side: ExecutionSide, sha: str, repo_path: Path,
                        adapter, environment: EnvironmentSpec, extra_files: dict[str, str],
                        sources: dict[str, TestSource]):
        key = hashlib.sha256(f"{run.id}:{side.value}:{sha}:{environment.test_suite_hash}".encode()).hexdigest()
        execution = self.repository.begin_execution(run.id, side, sha, key, environment)
        try:
            result = await asyncio.wait_for(
                self.runner.run(run.id, side.value, repo_path, sha, adapter, self.limits,
                                dict(extra_files), sources, not run.run_existing_tests),
                timeout=self.limits.overall_timeout_seconds,
            )
            status = ExecutionStatus.TIMEOUT if result.timed_out else ExecutionStatus.COMPLETED
            self.repository.complete_execution(execution.id, status=status,
                build_status=result.build_status.value, test_status=result.test_status.value,
                exit_code=result.exit_code, duration_ms=result.duration_ms, stdout=result.stdout,
                stderr=result.stderr, artifacts=result.artifacts, tests=result.tests)
        except TimeoutError:
            self.repository.complete_execution(execution.id, status=ExecutionStatus.TIMEOUT,
                build_status="TIMEOUT", test_status="NOT_RUN", exit_code=None,
                duration_ms=round(self.limits.overall_timeout_seconds * 1000), stdout="",
                stderr="overall validation timeout", artifacts={}, tests=[])
        except Exception as exc:
            self.repository.complete_execution(execution.id, status=ExecutionStatus.FAILED,
                build_status="NOT_RUN", test_status="NOT_RUN", exit_code=None, duration_ms=0,
                stdout="", stderr=f"{type(exc).__name__}: {exc}", artifacts={}, tests=[])
        refreshed = next(item for item in self.repository.list_executions(run.id) if item.id == execution.id)
        self._evidence(run.id, "RUNNER", f"{side.value} isolated execution",
                       f"build={refreshed.build_status.value}, test={refreshed.test_status.value}",
                       {"commit_sha": sha, "environment": environment.model_dump(mode="json"),
                        "artifacts": refreshed.artifact_metadata})
        return refreshed

    async def diagnose(self, user_id: str, project_id: str, validation_id: str) -> str:
        run = self.repository.detail(user_id, validation_id)
        if run is None:
            raise KeyError("validation not found")
        if run.diagnosis_id:
            return run.diagnosis_id
        regressions = [item for item in run.regressions if item.classification in {
            RegressionClassification.NEW_REGRESSION, RegressionClassification.BUILD_REGRESSION,
            RegressionClassification.AI_CONFIRMED_REGRESSION,
        }]
        if not regressions:
            raise ValidationRequestError("diagnosis requires at least one confirmed regression")
        if not (self.diagnosis_service and self.diagnosis_repository and self.diagnosis_manager):
            raise RuntimeError("diagnosis integration is unavailable")
        question = json.dumps({
            "task": "Diagnose pre-merge regression; prioritize CI evidence and immutable Git commits",
            "validation_id": run.id, "repository": run.repository,
            "base_commit_sha": run.base_commit_sha, "candidate_commit_sha": run.candidate_commit_sha,
            "changed_files": [item.model_dump(mode="json") for item in run.changed_files],
            "regressions": [item.model_dump(mode="json") for item in regressions],
            "rule": "Do not assume production runtime state is required; correlate runtime evidence only when available.",
        }, ensure_ascii=False)
        request = DiagnosisCreateRequest(trigger_type=DiagnosisTriggerType.SERVICE, question=question,
            initial_target=DiagnosisTarget(type=DiagnosisTargetType.SERVICE, name=run.repository),
            project_id=project_id)
        diagnosis = self.diagnosis_service.create(user_id, request)
        now = datetime.now(timezone.utc).isoformat()
        self.diagnosis_repository.upsert_evidence(DiagnosisEvidence(
            id=hashlib.sha256(f"validation:{run.id}".encode()).hexdigest(), diagnosis_id=diagnosis.id,
            source_type="VALIDATION", source_name="pre_merge_validation",
            resource_type="REPOSITORY", resource_id=run.repository,
            title="Pre-Merge Validation regression evidence", summary=run.summary or "Regression found",
            raw_data={"validation_id": run.id, "regressions": [x.model_dump(mode="json") for x in regressions]},
            metadata={"base_commit_sha": run.base_commit_sha,
                      "candidate_commit_sha": run.candidate_commit_sha,
                      "reference": f"/api/validations/{run.id}"}, timestamp=now,
        ))
        if not self.repository.set_diagnosis(user_id, validation_id, diagnosis.id):
            existing = self.repository.get(user_id, validation_id)
            return existing.diagnosis_id if existing and existing.diagnosis_id else diagnosis.id
        self.diagnosis_manager.submit(diagnosis.id, reason="validation regression diagnosis")
        return diagnosis.id

    async def _diff(self, repository: Path, base: str, candidate: str):
        status = await run_fixed_command("git", ["-C", str(repository), "diff", "--relative", "--name-status", "-M", base, candidate, "--", "."], timeout=60)
        changed: list[ChangedFile] = []
        for line in status.splitlines()[:500]:
            parts = line.split("\t")
            if len(parts) >= 3 and parts[0].startswith("R"):
                changed.append(ChangedFile(status=parts[0], old_path=parts[1], path=parts[2]))
            elif len(parts) >= 2:
                changed.append(ChangedFile(status=parts[0], path=parts[-1]))
        diff = await run_fixed_command("git", ["-C", str(repository), "diff", "--relative", "--no-ext-diff", "--unified=5", base, candidate, "--", "."], timeout=60)
        return changed, diff[:50000]

    async def _repository_test_paths(self, repository: Path, sha: str,
                                     project_type: ProjectType) -> list[str]:
        prefix = (await run_fixed_command("git", ["-C", str(repository), "rev-parse", "--show-prefix"], timeout=10)).strip().rstrip("/")
        treeish = f"{sha}:{prefix}" if prefix else sha
        tree = await run_fixed_command("git", ["-C", str(repository), "ls-tree", "-r", "--name-only", treeish], timeout=30)
        if project_type is ProjectType.MAVEN:
            return [path for path in tree.splitlines()
                    if path.startswith("src/test/") and path.endswith(".java")][:500]
        if project_type is ProjectType.PYTHON:
            return [path for path in tree.splitlines()
                    if path.startswith("tests/") and path.endswith(".py")][:500]
        return []

    async def _test_samples(self, repository: Path, sha: str,
                            selected_paths: list[str] | None = None) -> dict[str, str]:
        project_type = await self.detector.detect(repository, sha)
        available = await self._repository_test_paths(repository, sha, project_type)
        paths = selected_paths if selected_paths is not None else available[:3]
        if any(path not in available for path in paths):
            raise ValidationRequestError("AI reference test is not present in frozen candidate commit")
        prefix = (await run_fixed_command("git", ["-C", str(repository), "rev-parse", "--show-prefix"], timeout=10)).strip().rstrip("/")
        output = {}
        for path in paths:
            object_path = f"{prefix}/{path}" if prefix else path
            output[path] = (await run_fixed_command("git", ["-C", str(repository), "show", f"{sha}:{object_path}"], timeout=20))[:8000]
        return output

    def _environment(self, adapter, suite_hash: str) -> EnvironmentSpec:
        raw = {"image": adapter.runner_image, "cpus": self.limits.cpus,
               "memory_mb": self.limits.memory_mb, "pids_limit": self.limits.pids_limit,
               "project_type": adapter.project_type.value, "runtime": adapter.runtime_version,
               "network": "bridge" if self.limits.allow_network else "none",
               "suite_hash": suite_hash, "adapter": adapter.version}
        fingerprint = hashlib.sha256(json.dumps(raw, sort_keys=True).encode()).hexdigest()
        return EnvironmentSpec(runner_image=adapter.runner_image, cpu_limit=self.limits.cpus,
            memory_mb=self.limits.memory_mb, pids_limit=self.limits.pids_limit,
            project_type=adapter.project_type, runtime_version=adapter.runtime_version,
            dependency_mode="container-image", network_policy=raw["network"],
            test_suite_hash=suite_hash, adapter_version=adapter.version,
            environment_fingerprint=fingerprint)

    @staticmethod
    def _test_sources(files: dict[str, str], source: TestSource) -> dict[str, TestSource]:
        """从受控测试源码提取常见测试名，确保 JUnit 保留来源标签。"""
        import re
        names: dict[str, TestSource] = {}
        for path, content in files.items():
            if path.endswith(".py"):
                candidates = re.findall(r"(?m)^\s*(?:async\s+)?def\s+(test_[A-Za-z0-9_]+)\s*\(", content)
            elif path.endswith(".java"):
                candidates = re.findall(r"(?m)\b(?:void|public\s+void)\s+([A-Za-z_$][\w$]*)\s*\(", content)
            else:
                candidates = []
            names.update({name: source for name in candidates})
        return names

    def _evidence(self, validation_id: str, source_type: str, title: str,
                  summary: str, structured_data: dict) -> None:
        self.repository.upsert_evidence(ValidationEvidence(
            id=hashlib.sha256(f"{validation_id}:{source_type}:{title}".encode()).hexdigest(),
            validation_id=validation_id, source_type=source_type, title=title,
            summary=summary, structured_data=structured_data,
            timestamp=datetime.now(timezone.utc).isoformat(),
        ))
