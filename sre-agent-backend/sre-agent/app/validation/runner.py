"""不可信用户代码的 Ephemeral Docker Runner 与 JUnit 结果归一化。"""

from __future__ import annotations

import asyncio
import hashlib
import json
import shutil
import subprocess
import tarfile
import tempfile
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Protocol

from app.validation.models import StageStatus, TestCaseResult, TestSource, TestStatus
from app.validation.project import ProjectAdapter


class ValidationRunnerError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class RunnerLimits:
    cpus: float = 1.0
    memory_mb: int = 1024
    pids_limit: int = 128
    prepare_timeout_seconds: float = 60
    build_timeout_seconds: float = 300
    test_timeout_seconds: float = 600
    overall_timeout_seconds: float = 900
    allow_network: bool = False


@dataclass(slots=True)
class RunnerResult:
    build_status: StageStatus
    test_status: StageStatus
    exit_code: int | None
    duration_ms: int
    stdout: str = ""
    stderr: str = ""
    tests: list[TestCaseResult] = field(default_factory=list)
    artifacts: dict[str, str] = field(default_factory=dict)
    timed_out: bool = False


class ValidationRunner(Protocol):
    async def run(
        self, validation_id: str, side: str, repository_path: Path, commit_sha: str,
        adapter: ProjectAdapter, limits: RunnerLimits, extra_files: dict[str, str],
        source_by_test_name: dict[str, TestSource], exclude_repository_tests: bool = False,
    ) -> RunnerResult: ...


@dataclass(slots=True)
class _CommandResult:
    returncode: int | None
    stdout: str
    stderr: str
    timed_out: bool


class DockerValidationRunner:
    """只运行 Adapter 固定 argv；请求体和模型都不能提供 Shell。"""

    MAX_ARCHIVE_FILES = 20_000
    MAX_ARCHIVE_BYTES = 256 * 1024 * 1024

    def __init__(self, workspace_root: str | Path, artifact_root: str | Path) -> None:
        self.workspace_root = Path(workspace_root).resolve()
        self.artifact_root = Path(artifact_root).resolve()

    async def run(
        self, validation_id: str, side: str, repository_path: Path, commit_sha: str,
        adapter: ProjectAdapter, limits: RunnerLimits, extra_files: dict[str, str],
        source_by_test_name: dict[str, TestSource], exclude_repository_tests: bool = False,
    ) -> RunnerResult:
        started = time.monotonic()
        workspace = (self.workspace_root / validation_id / side.lower()).resolve()
        self._inside(workspace, self.workspace_root)
        if workspace.exists():
            shutil.rmtree(workspace)
        workspace.mkdir(parents=True)
        try:
            await asyncio.wait_for(
                asyncio.to_thread(self._extract_commit, repository_path, commit_sha, workspace),
                timeout=limits.prepare_timeout_seconds,
            )
            if exclude_repository_tests:
                self._remove_repository_tests(workspace, adapter)
            self._inject_files(workspace, extra_files)
            build = await self._docker_stage(
                validation_id, side, "build", workspace, adapter.runner_image,
                adapter.build_command, limits, limits.build_timeout_seconds,
            )
            if build.timed_out:
                return self._result(started, StageStatus.TIMEOUT, StageStatus.NOT_RUN, build, [], validation_id, side)
            if build.returncode != 0:
                return self._result(started, StageStatus.FAILED, StageStatus.NOT_RUN, build, [], validation_id, side)
            test = await self._docker_stage(
                validation_id, side, "test", workspace, adapter.runner_image,
                adapter.test_command, limits, limits.test_timeout_seconds,
            )
            tests = normalize_junit(workspace, adapter.report_patterns, source_by_test_name)
            combined = _CommandResult(
                test.returncode, build.stdout + "\n" + test.stdout,
                build.stderr + "\n" + test.stderr, test.timed_out,
            )
            test_status = StageStatus.TIMEOUT if test.timed_out else (
                StageStatus.PASSED if test.returncode == 0 else StageStatus.FAILED
            )
            return self._result(started, StageStatus.PASSED, test_status, combined, tests, validation_id, side)
        except TimeoutError as exc:
            raise ValidationRunnerError("repository preparation timed out") from exc
        finally:
            if workspace.exists():
                shutil.rmtree(workspace)

    async def _docker_stage(
        self, validation_id: str, side: str, stage: str, workspace: Path,
        image: str, command: tuple[str, ...], limits: RunnerLimits, timeout: float,
    ) -> _CommandResult:
        suffix = hashlib.sha256(f"{validation_id}:{side}:{stage}".encode()).hexdigest()[:12]
        container_name = f"sre-validation-{suffix}"
        network = "bridge" if limits.allow_network else "none"
        argv = [
            "docker", "run", "--rm", "--name", container_name,
            "--network", network, "--cpus", str(limits.cpus),
            "--memory", f"{limits.memory_mb}m", "--pids-limit", str(limits.pids_limit),
            "--cap-drop", "ALL", "--security-opt", "no-new-privileges:true", "--read-only",
            "--tmpfs", "/tmp:rw,noexec,nosuid,size=128m",
            "--tmpfs", "/root/.m2:rw,nosuid,size=512m",
            "--mount", f"type=bind,source={workspace},target=/workspace",
            "--workdir", "/workspace", image, *command,
        ]
        try:
            completed = await asyncio.to_thread(
                subprocess.run, argv, capture_output=True, check=False, timeout=timeout,
            )
            return _CommandResult(
                completed.returncode,
                completed.stdout.decode("utf-8", errors="replace"),
                completed.stderr.decode("utf-8", errors="replace"), False,
            )
        except subprocess.TimeoutExpired as exc:
            await asyncio.to_thread(
                subprocess.run, ["docker", "rm", "-f", container_name],
                capture_output=True, check=False, timeout=20,
            )
            return _CommandResult(
                None,
                (exc.stdout or b"").decode("utf-8", errors="replace"),
                (exc.stderr or b"").decode("utf-8", errors="replace"), True,
            )
        except OSError as exc:
            raise ValidationRunnerError(f"Docker Runner unavailable: {exc}") from exc

    def _result(
        self, started: float, build: StageStatus, test: StageStatus,
        command: _CommandResult, tests: list[TestCaseResult], validation_id: str, side: str,
    ) -> RunnerResult:
        artifacts = self._persist_artifacts(validation_id, side, command.stdout, command.stderr, tests)
        return RunnerResult(
            build_status=build, test_status=test, exit_code=command.returncode,
            duration_ms=round((time.monotonic() - started) * 1000),
            stdout=command.stdout, stderr=command.stderr, tests=tests,
            artifacts=artifacts, timed_out=command.timed_out,
        )

    def _persist_artifacts(
        self, validation_id: str, side: str, stdout: str, stderr: str,
        tests: list[TestCaseResult],
    ) -> dict[str, str]:
        target = (self.artifact_root / validation_id / side.lower()).resolve()
        self._inside(target, self.artifact_root)
        target.mkdir(parents=True, exist_ok=True)
        (target / "stdout.log").write_text(stdout, encoding="utf-8")
        (target / "stderr.log").write_text(stderr, encoding="utf-8")
        (target / "tests.json").write_text(
            json.dumps([item.model_dump(mode="json") for item in tests], ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        return {
            "stdout": str(target / "stdout.log"), "stderr": str(target / "stderr.log"),
            "tests": str(target / "tests.json"),
        }

    def _extract_commit(self, repository: Path, commit_sha: str, workspace: Path) -> None:
        with tempfile.NamedTemporaryFile(suffix=".tar", delete=False) as handle:
            archive_path = Path(handle.name)
        try:
            prefix_result = subprocess.run(
                ["git", "-C", str(repository), "rev-parse", "--show-prefix"],
                capture_output=True, check=False, timeout=20,
            )
            if prefix_result.returncode != 0:
                raise ValidationRunnerError("unable to determine authorized repository subtree")
            prefix = prefix_result.stdout.decode("utf-8", errors="replace").strip().rstrip("/")
            treeish = f"{commit_sha}:{prefix}" if prefix else commit_sha
            with archive_path.open("wb") as output:
                completed = subprocess.run(
                    ["git", "-C", str(repository), "archive", "--format=tar", treeish],
                    stdout=output, stderr=subprocess.PIPE, check=False, timeout=60,
                )
            if completed.returncode != 0:
                detail = completed.stderr.decode("utf-8", errors="replace")[:1000]
                raise ValidationRunnerError(f"git archive failed: {detail}")
            with tarfile.open(archive_path) as archive:
                members = archive.getmembers()
                if len(members) > self.MAX_ARCHIVE_FILES:
                    raise ValidationRunnerError("repository archive contains too many files")
                total = 0
                for member in members:
                    path = PurePosixPath(member.name)
                    if path.is_absolute() or ".." in path.parts or member.issym() or member.islnk() or member.isdev():
                        raise ValidationRunnerError("unsafe repository archive entry")
                    total += max(0, member.size)
                if total > self.MAX_ARCHIVE_BYTES:
                    raise ValidationRunnerError("repository archive is too large")
                archive.extractall(workspace, members=members, filter="data")
        finally:
            archive_path.unlink(missing_ok=True)

    @staticmethod
    def _inject_files(workspace: Path, files: dict[str, str]) -> None:
        for relative, content in files.items():
            target = (workspace / relative).resolve()
            DockerValidationRunner._inside(target, workspace)
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")

    @staticmethod
    def _remove_repository_tests(workspace: Path, adapter: ProjectAdapter) -> None:
        """仅在临时副本中移除仓库测试，让 Uploaded/AI-only 模式语义真实。"""
        relative = "src/test" if adapter.project_type.value == "MAVEN" else "tests"
        target = (workspace / relative).resolve()
        DockerValidationRunner._inside(target, workspace)
        if target.exists():
            shutil.rmtree(target)

    @staticmethod
    def _inside(path: Path, root: Path) -> None:
        try:
            path.relative_to(root)
        except ValueError as exc:
            raise ValidationRunnerError("workspace path escapes configured root") from exc


def normalize_junit(
    workspace: Path, patterns: tuple[str, ...],
    source_by_test_name: dict[str, TestSource] | None = None,
) -> list[TestCaseResult]:
    """把 Maven Surefire 或 pytest JUnit XML 转成统一 TestCaseResult。"""
    sources = source_by_test_name or {}
    output: list[TestCaseResult] = []
    paths = sorted({path for pattern in patterns for path in workspace.glob(pattern)})
    for path in paths:
        try:
            root = ET.parse(path).getroot()
        except (ET.ParseError, OSError):
            continue
        for case in root.iter("testcase"):
            name = str(case.attrib.get("name") or "unknown")
            suite = str(case.attrib.get("classname") or case.attrib.get("class") or path.stem)
            failure = case.find("failure")
            error = case.find("error")
            skipped = case.find("skipped")
            node = failure if failure is not None else error
            status = TestStatus.PASSED
            if failure is not None:
                status = TestStatus.FAILED
            elif error is not None:
                status = TestStatus.ERROR
            elif skipped is not None:
                status = TestStatus.SKIPPED
            output.append(TestCaseResult(
                suite=suite[:500], name=name[:500], status=status,
                duration_ms=max(0, round(float(case.attrib.get("time") or 0) * 1000)),
                failure_type=(node.attrib.get("type") if node is not None else None),
                message=(node.attrib.get("message") or "")[:4000] if node is not None else None,
                stack_trace_summary=(node.text or "")[:8000] if node is not None else None,
                source=sources.get(name, TestSource.REPOSITORY),
            ))
    return output
