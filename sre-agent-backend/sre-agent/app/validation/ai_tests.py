"""上传测试与 AI Generated Test 的路径、内容和语法安全边界。"""

from __future__ import annotations

import ast
import json
import re
from pathlib import PurePosixPath
from typing import Any

from app.llm.base import LLM, LLMMessage
from app.llm.structured_output import validate_structured_output
from app.validation.models import (
    GeneratedTestCase, GeneratedTestSuite, InterfaceTarget, ProjectType, UploadedTestFile,
)


class TestFileValidationError(ValueError):
    pass


class TestFileValidator:
    MAX_FILES = 20
    MAX_FILE_BYTES = 256 * 1024
    MAX_TOTAL_BYTES = 1024 * 1024
    _RULES = {
        ProjectType.MAVEN: (("src/test/java/", "src/test/resources/"), {".java", ".xml", ".json", ".txt"}),
        ProjectType.PYTHON: (("tests/",), {".py", ".json", ".txt"}),
    }

    def validate_uploaded(self, project_type: ProjectType, files: list[UploadedTestFile]) -> dict[str,str]:
        if not files or len(files) > self.MAX_FILES:
            raise TestFileValidationError("uploaded test file count must be between 1 and 20")
        output: dict[str,str] = {}; total=0
        for item in files:
            path=self._path(project_type,item.path,ai_generated=False)
            data=item.content.encode("utf-8")
            if b"\x00" in data or len(data)>self.MAX_FILE_BYTES:
                raise TestFileValidationError(f"invalid or oversized text file: {path}")
            total += len(data)
            if total>self.MAX_TOTAL_BYTES:
                raise TestFileValidationError("uploaded test suite exceeds 1 MiB")
            output[path]=item.content
        return output

    def validate_generated(self, project_type: ProjectType, test: GeneratedTestCase) -> str:
        path=self._path(project_type,test.target_file,ai_generated=True)
        if len(test.test_code.encode("utf-8"))>50_000 or "\x00" in test.test_code:
            raise TestFileValidationError("AI generated test is oversized or binary")
        forbidden=("Runtime.getRuntime", "ProcessBuilder", "subprocess", "os.system", "socket.", "requests.", "httpx.")
        if any(token in test.test_code for token in forbidden):
            raise TestFileValidationError("AI generated test uses a forbidden system or network API")
        if project_type is ProjectType.PYTHON:
            try: ast.parse(test.test_code)
            except SyntaxError as exc: raise TestFileValidationError(f"Python syntax error: {exc.msg}") from exc
        elif project_type is ProjectType.MAVEN:
            if not re.search(r"\bclass\s+\w+",test.test_code) or test.test_code.count("{")!=test.test_code.count("}"):
                raise TestFileValidationError("Java test failed basic class/brace validation")
        return path

    def _path(self, project_type: ProjectType, raw: str, *, ai_generated: bool) -> str:
        if "\\" in raw or ":" in raw or raw.startswith(("/","~")):
            raise TestFileValidationError("test path must be a POSIX relative path")
        path=PurePosixPath(raw)
        if path.is_absolute() or ".." in path.parts or any(part in ("", ".") for part in path.parts):
            raise TestFileValidationError("test path traversal is forbidden")
        normalized=str(path)
        rules=self._RULES.get(project_type)
        if rules is None:
            raise TestFileValidationError("uploaded or AI tests are unsupported for this project type")
        prefixes,extensions=rules
        if not normalized.startswith(prefixes) or path.suffix.lower() not in extensions:
            raise TestFileValidationError("test path or extension is outside the project allow-list")
        if ai_generated:
            strict_prefix="src/test/" if project_type is ProjectType.MAVEN else "tests/"
            if not normalized.startswith(strict_prefix) or path.suffix.lower() not in {".java",".py"}:
                raise TestFileValidationError("AI may only add test source files")
        return normalized


class AITestGenerator:
    """模型只生成候选测试；真实 Docker 结果由 Comparator 决定最终分类。"""

    def __init__(self, llm: LLM) -> None:
        self.llm=llm

    async def generate(self, *, project_type: ProjectType, diff: str,
                       changed_files: list[str], code_state: list[dict[str,Any]],
                       test_samples: dict[str,str]) -> list[GeneratedTestCase]:
        response=await self.llm.complete([
            LLMMessage("system",
                "你是受限的回归测试生成器。只能新增 test source，不能修改生产源码、构建文件、"
                "Dockerfile、脚本或 CI 配置；不能使用网络、进程、文件系统危险 API。"
                "输出严格 JSON：{\"tests\":[{target_file,test_name,test_code,reason,covered_change}]}。/no_think"),
            LLMMessage("user",json.dumps({
                "project_type":project_type.value,"changed_files":changed_files,
                "bounded_diff":diff[:16000],"code_state":code_state[:20],
                # 自动选择时调用方只给 3 个；用户显式选择时必须完整保留最多 10 个。
                "representative_tests":{key:value[:4000] for key,value in list(test_samples.items())[:10]},
            },ensure_ascii=False)),
        ])
        return validate_structured_output(response.content,GeneratedTestSuite).tests

    async def generate_interface_tests(self, *, project_type: ProjectType,
                                       target: InterfaceTarget, diff: str,
                                       candidate_source: str,
                                       existing_tests: dict[str,str]) -> list[GeneratedTestCase]:
        """为一个已冻结接口生成测试；写入由 TestFileValidator 再次把关。"""
        response = await self.llm.complete([
            LLMMessage("system",
                "你是接口级回归测试生成器。只输出测试源码文件，不能修改生产源码、构建文件、"
                "Dockerfile、脚本或 CI 配置，也不能使用网络、进程或危险文件系统 API。"
                "若提供既有测试，请针对 Candidate 接口变化给出需要新增或更新的测试文件；"
                "系统会把结果保存为新的不可变 Test Suite Version，绝不覆盖历史版本。"
                "输出严格 JSON：{\"tests\":[{target_file,test_name,test_code,reason,covered_change}]}。/no_think"),
            LLMMessage("user",json.dumps({
                "project_type":project_type.value,
                "target_interface":target.model_dump(mode="json"),
                "bounded_candidate_source":candidate_source[:12000],
                "bounded_diff":diff[:16000],
                "existing_suite_files":{
                    key:value[:6000] for key,value in list(existing_tests.items())[:20]
                },
            },ensure_ascii=False)),
        ])
        return validate_structured_output(response.content,GeneratedTestSuite).tests
