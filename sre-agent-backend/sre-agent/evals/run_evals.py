"""通过真实认证 API 运行固定 SRE-001～SRE-010，并生成统一评测报告。"""

from __future__ import annotations

import argparse
import json
import os
import statistics
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


def score_case(case: dict[str, Any], report: dict[str, Any], elapsed_ms: int) -> dict[str, Any]:
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
    passed = service_correct and root_cause_correct and evidence_complete and final_status == "confirmed"
    failures: list[str] = []
    if not service_correct:
        failures.append(f"service expected={case['expected_service']} actual={report.get('service')}")
    if not root_cause_correct:
        failures.append("root cause keyword mismatch")
    if not evidence_complete:
        failures.append(f"evidence {evidence_status}: missing={sorted(required_sources - actual_sources)}")
    if final_status != "confirmed":
        failures.append(f"final status={final_status}")

    timeline = report.get("investigation_timeline", [])
    tool_calls = sum(1 for item in timeline if not str(item.get("tool_name", "")).startswith("llm_"))
    return {
        "case_id": case["case_id"], "passed": passed,
        "service_correct": service_correct, "root_cause_correct": root_cause_correct,
        "evidence_status": evidence_status,
        "required_evidence": sorted(required_sources), "actual_evidence": sorted(actual_sources),
        "tool_calls": tool_calls, "diagnosis_time_ms": elapsed_ms,
        "token_usage": int(report.get("token_usage") or 0), "final_status": final_status,
        "confidence": float(report.get("confidence") or 0),
        "failure_reason": "; ".join(failures) or None,
    }


def aggregate(results: list[dict[str, Any]]) -> dict[str, Any]:
    count = len(results)
    if not count:
        return {"cases": 0}
    return {
        "cases": count, "passed": sum(bool(item.get("passed")) for item in results),
        "service_accuracy": round(sum(bool(item.get("service_correct")) for item in results) / count, 4),
        "root_cause_accuracy": round(sum(bool(item.get("root_cause_correct")) for item in results) / count, 4),
        "evidence_completion_rate": round(sum(item.get("evidence_status") == "COMPLETE" for item in results) / count, 4),
        "average_tool_calls": round(statistics.fmean(item.get("tool_calls", 0) for item in results), 2),
        "average_diagnosis_time_ms": round(statistics.fmean(item.get("diagnosis_time_ms", 0) for item in results), 2),
        "average_token_usage": round(statistics.fmean(item.get("token_usage", 0) for item in results), 2),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="SRE Agent fixed 10-case evidence evaluation")
    parser.add_argument("--base-url", default="http://127.0.0.1:8001")
    parser.add_argument("--project-id", default="sre-lab")
    parser.add_argument("--case", help="例如 SRE-001；省略则运行固定的全部 10 Case")
    parser.add_argument("--timeout", type=int, default=300)
    parser.add_argument("--token", default=os.getenv("SRE_EVAL_TOKEN"))
    parser.add_argument("--username", default=os.getenv("SRE_INITIAL_USERNAME"))
    parser.add_argument("--password", default=os.getenv("SRE_INITIAL_PASSWORD"))
    parser.add_argument("--output", help="结果文件；默认全量写 latest.json，单 Case 写 <case>.json")
    args = parser.parse_args()

    directory = Path(__file__).resolve().parent
    files = [directory / f"{args.case}.json"] if args.case else [directory / f"SRE-{index:03d}.json" for index in range(1, 11)]
    cases: list[dict[str, Any]] = []
    for path in files:
        case = json.loads(path.read_text(encoding="utf-8"))
        validate_case(case, path)
        cases.append(case)

    token = args.token
    if not token:
        if not args.username or not args.password:
            raise RuntimeError("请通过 SRE_EVAL_TOKEN 或 username/password 环境变量提供评测认证")
        token = authenticate(args.base_url, args.username, args.password, args.timeout)

    results: list[dict[str, Any]] = []
    for case in cases:
        started = time.perf_counter()
        try:
            report = request_report(args.base_url, case["symptom"], args.project_id, token, args.timeout)
            results.append(score_case(case, report, int((time.perf_counter() - started) * 1000)))
        except RuntimeError as exc:
            results.append({
                "case_id": case["case_id"], "passed": False,
                "service_correct": False, "root_cause_correct": False,
                "evidence_status": "MISSING", "tool_calls": 0,
                "diagnosis_time_ms": int((time.perf_counter() - started) * 1000),
                "token_usage": 0, "final_status": "error", "failure_reason": str(exc),
            })

    output = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "agent_input_contract": "symptom_only; expected values remain evaluator-only",
        "metrics": aggregate(results), "cases": results,
    }
    default_output = f"evals/results/{args.case}.json" if args.case else "evals/results/latest.json"
    output_path = Path(args.output or default_output)
    if not output_path.is_absolute():
        output_path = Path.cwd() / output_path
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(output, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(output, ensure_ascii=False, indent=2))
    return 0 if results and all(item.get("passed") for item in results) else 1


if __name__ == "__main__":
    sys.exit(main())
