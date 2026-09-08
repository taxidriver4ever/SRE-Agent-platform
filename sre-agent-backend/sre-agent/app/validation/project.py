"""Git Project Detection 与固定命令 Adapter。"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from app.core.process import run_fixed_command
from app.validation.models import ProjectType


@dataclass(frozen=True, slots=True)
class ProjectAdapter:
    project_type: ProjectType
    version: str
    runner_image: str
    runtime_version: str
    build_command: tuple[str, ...]
    test_command: tuple[str, ...]
    report_patterns: tuple[str, ...]


class ProjectDetector:
    async def detect(self, repository: Path, commit_sha: str) -> ProjectType:
        prefix = (await run_fixed_command(
            "git", ["-C", str(repository), "rev-parse", "--show-prefix"], timeout=10,
        )).strip().rstrip("/")
        treeish = f"{commit_sha}:{prefix}" if prefix else commit_sha
        output = await run_fixed_command(
            "git", ["-C", str(repository), "ls-tree", "-r", "--name-only", treeish],
            timeout=30,
        )
        names = {line.strip() for line in output.splitlines()}
        basenames = {Path(name).name for name in names}
        if "pom.xml" in basenames:
            return ProjectType.MAVEN
        if {"build.gradle", "build.gradle.kts"} & basenames:
            return ProjectType.GRADLE
        if {"pyproject.toml", "requirements.txt"} & basenames:
            return ProjectType.PYTHON
        if "package.json" in basenames:
            return ProjectType.NODE
        return ProjectType.UNKNOWN


class AdapterRegistry:
    VERSION = "validation-adapters-v1"

    def __init__(self, maven_image: str, python_image: str) -> None:
        self._adapters = {
            ProjectType.MAVEN: ProjectAdapter(
                ProjectType.MAVEN, self.VERSION, maven_image, "Java 21 / Maven 3.9",
                ("mvn", "-B", "-DskipTests", "compile"),
                ("mvn", "-B", "test"),
                ("target/surefire-reports/TEST-*.xml", "target/failsafe-reports/TEST-*.xml"),
            ),
            ProjectType.PYTHON: ProjectAdapter(
                ProjectType.PYTHON, self.VERSION, python_image, "Python 3.12",
                ("python", "-m", "compileall", "-q", "."),
                ("python", "-m", "pytest", "--junitxml=/workspace/validation-junit.xml"),
                ("validation-junit.xml",),
            ),
        }

    def get(self, project_type: ProjectType) -> ProjectAdapter | None:
        return self._adapters.get(project_type)
