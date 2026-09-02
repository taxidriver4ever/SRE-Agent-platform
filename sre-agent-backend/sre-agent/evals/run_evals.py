"""通过真实认证 API 运行固定 SRE-001～SRE-010，并生成统一评测报告。"""

from __future__ import annotations

import argparse
import math
import json
import os
import re
import statistics
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from dotenv import load_dotenv


load_dotenv(Path(__file__).resolve().parents[1] / ".env", override=False)


REQUIRED_CASE_FIELDS = {
    "case_id", "symptom", "expected_service", "expected_root_cause",
    "required_evidence", "forbidden_shortcuts",
}


def _json_request(url: str, payload: dict[str, Any], timeout: int, token: str | None = None) -> dict[str, Any]:
    headers = {"Content-Type": "application/json; charset=utf-8"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(
        url, data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers=headers, method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:1000]
        raise RuntimeError(f"HTTP {exc.code}: {detail}") from exc
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"API 调用失败: {exc}") from exc


def authenticate(base_url: str, username: str, password: str, timeout: int) -> str:
    response = _json_request(
        f"{base_url.rstrip('/')}/api/auth/login",
        {"username": username, "password": password}, timeout,
    )
    token = str(response.get("access_token") or "")
    if not token:
        raise RuntimeError("登录响应缺少 access_token")
    return token


def request_report(base_url: str, symptom: str, project_id: str, token: str, timeout: int) -> dict[str, Any]:
    # Expected、Case ID 和 forbidden_shortcuts 只保留在 Evaluator，不进入 Agent。
    return _json_request(
        f"{base_url.rstrip('/')}/api/agent/chat",
        {"message": symptom, "project_id": project_id}, timeout, token,
    )


def validate_case(case: dict[str, Any], path: Path) -> None:
    missing = REQUIRED_CASE_FIELDS - case.keys()
    if missing:
        raise ValueError(f"{path.name} 缺少字段: {sorted(missing)}")
    if case["case_id"] != path.stem:
        raise ValueError(f"{path.name} 的 case_id 必须等于文件名")
    if case["case_id"].lower() in str(case["symptom"]).lower():
        raise ValueError(f"{path.name} symptom 不得包含 Case ID")


def report_contract_failures(
    case: dict[str, Any],
    report: dict[str, Any],
    *,
    bad_canary_commit: str | None = None,
) -> list[str]:
    """校验跨 Case 文档契约；Expected 仍只存在于 Evaluator。"""
    case_id = str(case.get("case_id") or "")
    failures: list[str] = []
    if case_id in {"SRE-008", "SRE-009"} and not str(report.get("affected_pod") or "").strip():
        failures.append("affected_pod is required")
    if case_id == "SRE-009":
        git_sha = str(report.get("git_sha") or "").strip()
        source_location = str(report.get("source_code_location") or "").strip()
        if not re.fullmatch(r"[0-9a-fA-F]{40}", git_sha):
            failures.append("git_sha must be a full 40-character commit")
        elif bad_canary_commit and git_sha.lower() != bad_canary_commit.lower():
            failures.append(f"git_sha expected BAD canary={bad_canary_commit} actual={git_sha}")
        if "OrderRepository.java" not in source_location:
            failures.append("source_code_location must contain OrderRepository.java")
    return failures


def resolve_bad_canary_commit(repository_root: Path) -> str:
    """从真实 Lab 仓库解析 bad ref，避免在评测器中写死提交号。"""
    completed = subprocess.run(
        ["git", "-C", str(repository_root / "sre-broken-system" / "order-service"), "rev-parse", "bad"],
        check=True,
        capture_output=True,
        text=True,
    )
    commit = completed.stdout.strip()
    if not re.fullmatch(r"[0-9a-fA-F]{40}", commit):
        raise RuntimeError(f"bad ref 未解析为完整 Git SHA: {commit}")
    return commit


def score_case(
    case: dict[str, Any],
    report: dict[str, Any],
    elapsed_ms: int,
    *,
    bad_canary_commit: str | None = None,
) -> dict[str, Any]:
    root_text = " ".join([
        str(report.get("root_cause", "")), str(report.get("conclusion", "")),
        " ".join(report.get("root_cause_chain", [])),
    ]).lower()
    actual_sources = {
        str(item.get("source")) for item in report.get("evidence", [])
        if item.get("supports_conclusion", True)
    }
    required_sources = set(case["required_evidence"])
    matched_sources = required_sources & actual_sources
    if not required_sources or matched_sources == required_sources:
        evidence_status = "COMPLETE"
    elif matched_sources:
        evidence_status = "PARTIAL"
    else:
        evidence_status = "MISSING"

    service_correct = report.get("service") == case["expected_service"]
    root_cause_correct = any(str(keyword).lower() in root_text for keyword in case["expected_root_cause"])
    evidence_complete = evidence_status == "COMPLETE"
    final_status = str(report.get("status") or "unknown")
    failures: list[str] = []
    if not service_correct:
        failures.append(f"service expected={case['expected_service']} actual={report.get('service')}")
    if not root_cause_correct:
        failures.append("root cause keyword mismatch")
    if not evidence_complete:
        failures.append(f"evidence {evidence_status}: missing={sorted(required_sources - actual_sources)}")
    if final_status != "confirmed":
        failures.append(f"final status={final_status}")
    failures.extend(report_contract_failures(case, report, bad_canary_commit=bad_canary_commit))
    passed = not failures

    timeline = report.get("investigation_timeline", [])
    tool_calls = sum(1 for item in timeline if not str(item.get("tool_name", "")).startswith("llm_"))
    tool_failures = sum(1 for item in timeline if item.get("error"))
    return {
        "case_id": case["case_id"], "passed": passed,
        "service_correct": service_correct, "root_cause_correct": root_cause_correct,
        "evidence_status": evidence_status,
        "required_evidence": sorted(required_sources), "actual_evidence": sorted(actual_sources),
        "tool_calls": tool_calls, "diagnosis_time_ms": elapsed_ms,
        "token_usage": int(report.get("token_usage") or 0), "final_status": final_status,
        "confidence": float(report.get("confidence") or 0),
        "tool_failures": tool_failures,
        "structured_output_retry_count": int(report.get("structured_output_retry_count") or 0),
        "affected_pod": report.get("affected_pod"),
        "git_sha": report.get("git_sha"),
        "source_code_location": report.get("source_code_location"),
        "failure_category": "agent" if failures else None,
        "failure_reason": "; ".join(failures) or None,
    }


def _p95(values: list[int]) -> int:
    ordered = sorted(values)
    return ordered[max(0, math.ceil(len(ordered) * 0.95) - 1)] if ordered else 0


def classify_runtime_failure(message: str) -> str:
    """将运行环境不可用与 Agent 的证据/推理失败分开统计。"""
    value = message.lower()
    infrastructure_markers = (
        "connection refused", "winerror 10061", "timed out", "timeout",
        "http 502", "http 503", "http 504", "bad gateway", "service unavailable",
        "temporary failure", "name or service not known", "no connection could be made",
    )
    return "infrastructure" if any(marker in value for marker in infrastructure_markers) else "agent"


def aggregate(results: list[dict[str, Any]]) -> dict[str, Any]:
    count = len(results)
    if not count:
        return {"cases": 0}
    infrastructure_failures = sum(item.get("failure_category") == "infrastructure" for item in results)
    evaluable = [item for item in results if item.get("failure_category") != "infrastructure"]
    evaluable_count = len(evaluable)
    total_tool_calls = sum(int(item.get("tool_calls") or 0) for item in results)
    total_tool_failures = sum(int(item.get("tool_failures") or 0) for item in results)
    return {
        "cases": count, "passed": sum(bool(item.get("passed")) for item in results),
        "overall_pass_rate": round(sum(bool(item.get("passed")) for item in results) / count, 4),
        "agent_evaluable_runs": evaluable_count,
        "agent_pass_rate": round(sum(bool(item.get("passed")) for item in evaluable) / evaluable_count, 4) if evaluable_count else 0.0,
        "infrastructure_failure_count": infrastructure_failures,
        "infrastructure_failure_rate": round(infrastructure_failures / count, 4),
        "service_accuracy": round(sum(bool(item.get("service_correct")) for item in results) / count, 4),
        "root_cause_accuracy": round(sum(bool(item.get("root_cause_correct")) for item in results) / count, 4),
        "evidence_completion_rate": round(sum(item.get("evidence_status") == "COMPLETE" for item in results) / count, 4),
        "average_tool_calls": round(statistics.fmean(item.get("tool_calls", 0) for item in results), 2),
        "average_diagnosis_time_ms": round(statistics.fmean(item.get("diagnosis_time_ms", 0) for item in results), 2),
        "p95_diagnosis_time_ms": _p95([int(item.get("diagnosis_time_ms") or 0) for item in results]),
        "average_token_usage": round(statistics.fmean(item.get("token_usage", 0) for item in results), 2),
        "timeout_rate": round(sum("timeout" in str(item.get("failure_reason") or "").lower()
                                  or "timed out" in str(item.get("failure_reason") or "").lower()
                                  for item in results) / count, 4),
        "insufficient_evidence_rate": round(sum(item.get("final_status") == "insufficient_evidence"
                                                for item in results) / count, 4),
        "tool_failure_rate": round(total_tool_failures / total_tool_calls, 4) if total_tool_calls else 0.0,
        "structured_output_retry_count": sum(int(item.get("structured_output_retry_count") or 0)
                                               for item in results),
    }


def aggregate_by_case(results: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    case_ids = sorted({str(item["case_id"]) for item in results})
    return {case_id: aggregate([item for item in results if item["case_id"] == case_id])
            for case_id in case_ids}


def main() -> int:
    parser = argparse.ArgumentParser(description="SRE Agent fixed 10-case evidence evaluation")
    parser.add_argument("--base-url", default="http://127.0.0.1:8001")
    parser.add_argument("--project-id", default="sre-lab")
    parser.add_argument("--case", help="例如 SRE-001；省略则运行固定的全部 10 Case")
    parser.add_argument("--timeout", type=int, default=300)
    parser.add_argument("--runs", type=int, default=3, help="每个 Case 的重复次数，默认 3")
    parser.add_argument("--token", default=os.getenv("SRE_EVAL_TOKEN"))
    parser.add_argument("--username", default=os.getenv("SRE_INITIAL_USERNAME"))
    parser.add_argument("--password", default=os.getenv("SRE_INITIAL_PASSWORD"))
    parser.add_argument("--output", help="结果文件；默认全量写 latest.json，单 Case 写 <case>.json")
    args = parser.parse_args()
    if args.runs < 1:
        parser.error("--runs 必须大于等于 1")

    directory = Path(__file__).resolve().parent
    files = [directory / f"{args.case}.json"] if args.case else [directory / f"SRE-{index:03d}.json" for index in range(1, 11)]
    cases: list[dict[str, Any]] = []
    for path in files:
        case = json.loads(path.read_text(encoding="utf-8"))
        validate_case(case, path)
        cases.append(case)
    bad_canary_commit = resolve_bad_canary_commit(directory.parents[2]) if any(
        case["case_id"] == "SRE-009" for case in cases
    ) else None

    default_output = f"evals/results/{args.case}.json" if args.case else "evals/results/latest.json"
    output_path = Path(args.output or default_output)
    if not output_path.is_absolute():
        output_path = Path.cwd() / output_path
    output_path.parent.mkdir(parents=True, exist_ok=True)

    token = args.token
    if not token:
        if not args.username or not args.password:
            raise RuntimeError("请通过 SRE_EVAL_TOKEN 或 username/password 环境变量提供评测认证")
        try:
            token = authenticate(args.base_url, args.username, args.password, args.timeout)
        except RuntimeError as exc:
            message = str(exc)
            output = {
                "generated_at": datetime.now(timezone.utc).isoformat(),
                "status": "blocked_by_infrastructure",
                "agent_input_contract": "symptom_only; expected values remain evaluator-only",
                "runs_per_case": args.runs,
                "planned_runs": len(cases) * args.runs,
                "executed_runs": 0,
                "infrastructure_error": message,
                "failure_category": classify_runtime_failure(message),
                "metrics": aggregate([]), "case_metrics": {}, "cases": [],
            }
            output_path.write_text(json.dumps(output, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            print(json.dumps(output, ensure_ascii=False, indent=2))
            return 2

    results: list[dict[str, Any]] = []
    for case in cases:
        for run_number in range(1, args.runs + 1):
            started = time.perf_counter()
            try:
                report = request_report(args.base_url, case["symptom"], args.project_id, token, args.timeout)
                result = score_case(
                    case,
                    report,
                    int((time.perf_counter() - started) * 1000),
                    bad_canary_commit=bad_canary_commit,
                )
            except RuntimeError as exc:
                message = str(exc)
                result = {
                    "case_id": case["case_id"], "passed": False,
                    "service_correct": False, "root_cause_correct": False,
                    "evidence_status": "MISSING", "tool_calls": 0, "tool_failures": 0,
                    "diagnosis_time_ms": int((time.perf_counter() - started) * 1000),
                    "token_usage": 0, "structured_output_retry_count": 0,
                    "final_status": "error", "failure_category": classify_runtime_failure(message),
                    "failure_reason": message,
                }
            result["run"] = run_number
            results.append(result)
            print(f"[{case['case_id']}] run {run_number}/{args.runs}: "
                  f"{'PASS' if result['passed'] else 'FAIL'} ({result['diagnosis_time_ms']} ms)", flush=True)

    output = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "status": "completed",
        "agent_input_contract": "symptom_only; expected values remain evaluator-only",
        "runs_per_case": args.runs,
        "planned_runs": len(cases) * args.runs,
        "executed_runs": len(results),
        "metrics": aggregate(results), "case_metrics": aggregate_by_case(results), "cases": results,
    }
    output_path.write_text(json.dumps(output, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(output, ensure_ascii=False, indent=2))
    return 0 if results and all(item.get("passed") for item in results) else 1


if __name__ == "__main__":
    sys.exit(main())
