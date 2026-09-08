"""Base/Candidate 结构化差分比较。"""

from app.validation.models import (
    ComparisonConfidence, RegressionClassification, RegressionResult,
    StageStatus, TestCaseResult, TestSource, TestStatus, ValidationExecution,
)


class ComparisonError(ValueError):
    pass


class ValidationComparator:
    """只根据真实 Runner 结果分类，不接受模型提供最终 Regression 结论。"""

    def compare(self, base: ValidationExecution, candidate: ValidationExecution,
                changed_files: list[str] | None = None) -> tuple[list[RegressionResult], ComparisonConfidence]:
        if base.environment.test_suite_hash != candidate.environment.test_suite_hash:
            raise ComparisonError("base and candidate test_suite_hash mismatch")
        same_environment = base.environment.environment_fingerprint == candidate.environment.environment_fingerprint
        if base.status.value == "TIMEOUT" or candidate.status.value == "TIMEOUT":
            return [self._inconclusive(base, candidate)], ComparisonConfidence.INCONCLUSIVE
        if base.status.value == "FAILED" or candidate.status.value == "FAILED":
            return [self._inconclusive(base, candidate)], ComparisonConfidence.INCONCLUSIVE
        confidence = ComparisonConfidence.HIGH if same_environment else ComparisonConfidence.LOW
        if base.build_status is StageStatus.PASSED and candidate.build_status is StageStatus.FAILED:
            return [RegressionResult(
                test_identity="BUILD", suite="BUILD", name="Project build", source=TestSource.REPOSITORY,
                classification=RegressionClassification.BUILD_REGRESSION, confidence=confidence,
                message=candidate.stderr_summary or candidate.stdout_summary,
                related_changes=changed_files or [],
            )], confidence
        if base.build_status is StageStatus.FAILED and candidate.build_status is StageStatus.PASSED:
            return [RegressionResult(
                test_identity="BUILD_FIX", suite="BUILD", name="Project build",
                source=TestSource.REPOSITORY,
                classification=RegressionClassification.POSSIBLE_FIX, confidence=confidence,
                message="base build failed while candidate build passed",
                related_changes=changed_files or [],
            )], confidence
        if base.build_status in {StageStatus.FAILED, StageStatus.TIMEOUT}:
            return [self._inconclusive(base, candidate)], ComparisonConfidence.INCONCLUSIVE

        # pytest/Surefire 只要存在失败用例，测试进程就会返回非零。因此不能在
        # Base test_status=FAILED 时提前判定 inconclusive，否则真实的
        # Base Fail -> Candidate Pass 永远无法进入逐用例 POSSIBLE_FIX。
        base_by_id = {item.identity: item for item in base.tests}
        candidate_by_id = {item.identity: item for item in candidate.tests}
        if base_by_id and candidate_by_id:
            results: list[RegressionResult] = []
            for identity in sorted(base_by_id.keys() | candidate_by_id.keys()):
                left, right = base_by_id.get(identity), candidate_by_id.get(identity)
                classification = self._classify(left, right)
                current = right or left
                assert current is not None
                result_confidence = confidence
                if classification in {RegressionClassification.AI_TEST_INVALID, RegressionClassification.INVALID_OR_EXISTING_BEHAVIOR}:
                    result_confidence = ComparisonConfidence.LOW
                results.append(RegressionResult(
                    test_identity=identity, suite=current.suite, name=current.name, source=current.source,
                    classification=classification, confidence=result_confidence,
                    base_status=left.status if left else None, candidate_status=right.status if right else None,
                    failure_type=(right or left).failure_type, message=(right or left).message,
                    stack_trace_summary=(right or left).stack_trace_summary,
                    related_changes=changed_files or [],
                ))
            return results, confidence

        if base.test_status is StageStatus.PASSED and candidate.test_status is StageStatus.FAILED:
            return [RegressionResult(
                test_identity="TEST_EXECUTION", suite="TEST_EXECUTION", name="Test process",
                source=TestSource.REPOSITORY,
                classification=RegressionClassification.NEW_REGRESSION, confidence=confidence,
                message=candidate.stderr_summary or candidate.stdout_summary,
                related_changes=changed_files or [],
            )], confidence
        if base.test_status is StageStatus.FAILED and candidate.test_status is StageStatus.PASSED:
            return [RegressionResult(
                test_identity="TEST_EXECUTION_FIX", suite="TEST_EXECUTION", name="Test process",
                source=TestSource.REPOSITORY,
                classification=RegressionClassification.POSSIBLE_FIX, confidence=confidence,
                message="base test process failed while candidate passed",
                related_changes=changed_files or [],
            )], confidence
        if base.test_status in {StageStatus.FAILED, StageStatus.TIMEOUT}:
            return [self._inconclusive(base, candidate)], ComparisonConfidence.INCONCLUSIVE
        return [], confidence

    @staticmethod
    def _classify(base: TestCaseResult | None, candidate: TestCaseResult | None) -> RegressionClassification:
        if base is None or candidate is None:
            return RegressionClassification.NEW_TEST
        base_pass = base.status in {TestStatus.PASSED, TestStatus.SKIPPED}
        candidate_pass = candidate.status in {TestStatus.PASSED, TestStatus.SKIPPED}
        if base.source is TestSource.AI_GENERATED:
            if base_pass and not candidate_pass:
                return RegressionClassification.AI_CONFIRMED_REGRESSION
            if not base_pass and not candidate_pass:
                return (RegressionClassification.AI_TEST_INVALID if base.status is TestStatus.ERROR and candidate.status is TestStatus.ERROR else RegressionClassification.INVALID_OR_EXISTING_BEHAVIOR)
            if not base_pass and candidate_pass:
                return RegressionClassification.POSSIBLE_FIX
            return RegressionClassification.NO_REGRESSION_DETECTED
        if base_pass and not candidate_pass:
            return RegressionClassification.NEW_REGRESSION
        if not base_pass and not candidate_pass:
            return RegressionClassification.EXISTING_FAILURE
        if not base_pass and candidate_pass:
            return RegressionClassification.POSSIBLE_FIX
        return RegressionClassification.UNCHANGED_PASS

    @staticmethod
    def _inconclusive(base: ValidationExecution, candidate: ValidationExecution) -> RegressionResult:
        return RegressionResult(
            test_identity="COMPARISON", suite="COMPARISON", name="Execution comparison",
            source=TestSource.REPOSITORY, classification=RegressionClassification.COMPARISON_INCONCLUSIVE,
            confidence=ComparisonConfidence.INCONCLUSIVE,
            message=f"base={base.status.value}, candidate={candidate.status.value}",
        )
