"""调用真实 Agent API 评测 SRE-001～SRE-010 的服务定位与证据质量。"""

import argparse
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any


def request_report(base_url: str, question: str, timeout: int) -> dict[str, Any]:
    """用标准库发送 UTF-8 JSON，避免评测脚本额外依赖 HTTP SDK。"""
    payload = json.dumps({"message": question}, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        f"{base_url.rstrip('/')}/api/agent/chat",
        data=payload,
        headers={"Content-Type": "application/json; charset=utf-8"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"Agent API 调用失败: {exc}") from exc


def score_case(case: dict[str, Any], report: dict[str, Any]) -> dict[str, Any]:
    """对明确字段评分；关键词采用“至少命中一个”，证据类型要求全部出现。"""
    root_text = " ".join([
        str(report.get("root_cause", "")),
        str(report.get("conclusion", "")),
        " ".join(report.get("root_cause_chain", [])),
    ]).lower()
    evidence_sources = {str(item.get("source")) for item in report.get("evidence", []) if item.get("supports_conclusion", True)}
    checks = {
        "service": report.get("service") == case["expected_service"],
        "root_cause": any(keyword.lower() in root_text for keyword in case["expected_root_cause"]),
        "evidence": set(case["expected_evidence_types"]).issubset(evidence_sources),
        "forbidden": not any(keyword.lower() in root_text for keyword in case["forbidden_wrong_causes"]),
        "confidence": float(report.get("confidence", 0)) >= float(case["minimum_confidence"]),
    }
    return {"passed": all(checks.values()), "checks": checks, "confidence": report.get("confidence", 0)}


def main() -> int:
    """按文件名稳定顺序运行，可用 --case 只评测一个已触发的故障。"""
    parser = argparse.ArgumentParser(description="SRE Agent evidence evaluation runner")
    parser.add_argument("--base-url", default="http://127.0.0.1:8001")
    parser.add_argument("--case", help="例如 SRE-001；省略则运行全部")
    parser.add_argument("--timeout", type=int, default=240)
    args = parser.parse_args()

    directory = Path(__file__).resolve().parent
    files = [directory / f"{args.case}.json"] if args.case else sorted(directory.glob("SRE-*.json"))
    results: dict[str, Any] = {}
    for path in files:
        case = json.loads(path.read_text(encoding="utf-8"))
        try:
            report = request_report(args.base_url, case["question"], args.timeout)
            results[path.stem] = score_case(case, report)
        except RuntimeError as exc:
            results[path.stem] = {"passed": False, "error": str(exc)}

    # stdout 是机器可读 JSON；不自动触发故障，也不修改集群状态。
    print(json.dumps(results, ensure_ascii=False, indent=2))
    return 0 if results and all(item.get("passed") for item in results.values()) else 1


if __name__ == "__main__":
    sys.exit(main())
