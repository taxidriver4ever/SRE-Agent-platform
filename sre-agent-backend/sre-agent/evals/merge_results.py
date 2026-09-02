"""将逐场景真实评测结果合并为统一 latest.json。"""

from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from run_evals import aggregate, aggregate_by_case


def load_case_results(input_directory: Path) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []
    for index in range(1, 11):
        case_id = f"SRE-{index:03d}"
        path = input_directory / f"eval-{case_id}.json"
        payload = json.loads(path.read_text(encoding="utf-8"))
        case_results = payload.get("cases") or []
        if len(case_results) != 3:
            raise ValueError(f"{path} 必须包含 3 次运行，实际为 {len(case_results)}")
        if any(item.get("case_id") != case_id for item in case_results):
            raise ValueError(f"{path} 包含不匹配的 case_id")
        results.extend(case_results)
    return results


def main() -> int:
    parser = argparse.ArgumentParser(description="Merge ten three-run SRE evaluation batches")
    parser.add_argument("--input-directory", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()

    results = load_case_results(args.input_directory.resolve())
    output = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "status": "completed",
        "agent_input_contract": "symptom_only; expected values remain evaluator-only",
        "runs_per_case": 3,
        "planned_runs": 30,
        "executed_runs": len(results),
        "metrics": aggregate(results),
        "case_metrics": aggregate_by_case(results),
        "cases": results,
    }
    output_path = args.output.resolve()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(output, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(output["metrics"], ensure_ascii=False, indent=2))
    return 0 if all(item.get("passed") for item in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
