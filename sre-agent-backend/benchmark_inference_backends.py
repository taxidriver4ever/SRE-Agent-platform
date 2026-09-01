"""用相同提示和生成参数对比 Ollama 与 vLLM 的流式推理性能。"""

from __future__ import annotations

import argparse
import json
import statistics
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

import httpx


PROMPTS = [
    "用不超过100字说明 Kubernetes Pod 出现 OOMKilled 时的三步排查顺序。",
    "返回 JSON，字段为 service、symptom、next_action。故障：order-service P95 延迟突然升高。",
    "简要解释数据库连接池耗尽为什么会造成请求排队，并给出两个只读验证方法。",
    "返回 JSON 数组，列出检查 CPU 饱和问题时最重要的三个 Prometheus 指标名。",
    "说明一次发布后只有单个 Pod 变慢时，如何区分代码、节点和流量因素。",
    "给出排查下游超时与重试风暴的最小证据链，不超过120字。",
]


@dataclass(slots=True)
class Sample:
    ok: bool
    latency_seconds: float
    ttft_seconds: float
    output_tokens: int
    generation_tps: float
    error: str | None = None


def _ollama_request(base_url: str, model: str, prompt: str, timeout: float) -> Sample:
    started = time.perf_counter()
    first_token_at: float | None = None
    final: dict = {}
    try:
        with httpx.stream(
            "POST",
            f"{base_url.rstrip('/')}/api/chat",
            timeout=timeout,
            json={
                "model": model,
                "messages": [{"role": "user", "content": prompt}],
                "stream": True,
                "think": False,
                "keep_alive": "10m",
                "options": {"temperature": 0, "num_predict": 128},
            },
        ) as response:
            response.raise_for_status()
            for line in response.iter_lines():
                if not line:
                    continue
                event = json.loads(line)
                content = (event.get("message") or {}).get("content") or ""
                if content and first_token_at is None:
                    first_token_at = time.perf_counter()
                if event.get("done"):
                    final = event
        finished = time.perf_counter()
        output_tokens = int(final.get("eval_count", 0))
        eval_seconds = int(final.get("eval_duration", 0)) / 1_000_000_000
        tps = output_tokens / eval_seconds if output_tokens and eval_seconds else 0.0
        return Sample(True, finished - started, (first_token_at or finished) - started,
                      output_tokens, tps)
    except Exception as exc:  # noqa: BLE001
        finished = time.perf_counter()
        return Sample(False, finished - started, finished - started, 0, 0.0, str(exc)[:300])


def _vllm_request(base_url: str, api_key: str, model: str, prompt: str, timeout: float) -> Sample:
    started = time.perf_counter()
    first_token_at: float | None = None
    usage: dict = {}
    try:
        with httpx.stream(
            "POST",
            f"{base_url.rstrip('/')}/chat/completions",
            timeout=timeout,
            headers={"Authorization": f"Bearer {api_key}"},
            json={
                "model": model,
                "messages": [{"role": "user", "content": prompt}],
                "stream": True,
                "stream_options": {"include_usage": True},
                "temperature": 0,
                "max_tokens": 128,
                "chat_template_kwargs": {"enable_thinking": False},
            },
        ) as response:
            response.raise_for_status()
            for line in response.iter_lines():
                if not line.startswith("data: ") or line == "data: [DONE]":
                    continue
                event = json.loads(line[6:])
                choices = event.get("choices") or []
                content = ((choices[0].get("delta") or {}).get("content") or "") if choices else ""
                if content and first_token_at is None:
                    first_token_at = time.perf_counter()
                if event.get("usage"):
                    usage = event["usage"]
        finished = time.perf_counter()
        output_tokens = int(usage.get("completion_tokens", 0))
        generation_seconds = max(0.000001, finished - (first_token_at or finished))
        return Sample(True, finished - started, (first_token_at or finished) - started,
                      output_tokens, output_tokens / generation_seconds if output_tokens else 0.0)
    except Exception as exc:  # noqa: BLE001
        finished = time.perf_counter()
        return Sample(False, finished - started, finished - started, 0, 0.0, str(exc)[:300])


def percentile(values: list[float], quantile: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, round((len(ordered) - 1) * quantile)))
    return ordered[index]


def summarize(samples: list[Sample], wall_seconds: float) -> dict:
    successful = [sample for sample in samples if sample.ok]
    total_tokens = sum(sample.output_tokens for sample in successful)
    return {
        "requests": len(samples), "successes": len(successful),
        "wall_seconds": round(wall_seconds, 4),
        "latency_p50_seconds": round(percentile([s.latency_seconds for s in successful], 0.50), 4),
        "latency_p95_seconds": round(percentile([s.latency_seconds for s in successful], 0.95), 4),
        "ttft_p50_seconds": round(percentile([s.ttft_seconds for s in successful], 0.50), 4),
        "ttft_p95_seconds": round(percentile([s.ttft_seconds for s in successful], 0.95), 4),
        "mean_generation_tps": round(statistics.fmean(s.generation_tps for s in successful), 2)
        if successful else 0.0,
        "aggregate_output_tps": round(total_tokens / wall_seconds, 2) if wall_seconds else 0.0,
        "total_output_tokens": total_tokens,
        "errors": [sample.error for sample in samples if sample.error],
        "samples": [asdict(sample) for sample in samples],
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--provider", choices=("ollama", "vllm"), required=True)
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--api-key", default="EMPTY")
    parser.add_argument("--warmups", type=int, default=1)
    parser.add_argument("--runs", type=int, default=12)
    parser.add_argument("--concurrency", type=int, default=1)
    parser.add_argument("--timeout", type=float, default=300)
    parser.add_argument("--output", help="可选：把同一份 JSON 结果写入文件")
    args = parser.parse_args()
    request = (
        (lambda prompt: _ollama_request(args.base_url, args.model, prompt, args.timeout))
        if args.provider == "ollama"
        else (lambda prompt: _vllm_request(args.base_url, args.api_key, args.model, prompt, args.timeout))
    )
    for index in range(args.warmups):
        warmup = request(PROMPTS[index % len(PROMPTS)])
        if not warmup.ok:
            raise SystemExit(f"warmup failed: {warmup.error}")
    prompts = [PROMPTS[index % len(PROMPTS)] for index in range(args.runs)]
    started = time.perf_counter()
    with ThreadPoolExecutor(max_workers=args.concurrency) as executor:
        samples = list(executor.map(request, prompts))
    wall_seconds = time.perf_counter() - started
    result = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "provider": args.provider,
        "model": args.model,
        "concurrency": args.concurrency,
        "warmups": args.warmups,
        "max_output_tokens": 128,
        "temperature": 0,
        **summarize(samples, wall_seconds),
    }
    serialized = json.dumps(result, ensure_ascii=False, indent=2)
    if args.output:
        output_path = Path(args.output)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_text(serialized + "\n", encoding="utf-8")
    print(serialized)


if __name__ == "__main__":
    main()
