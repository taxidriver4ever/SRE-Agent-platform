# Workflows

## 硬性状态机

`START → TRIAGE → BASELINE_OBSERVATION → ANALYZE → INVESTIGATE → VERIFY → REPORT → END`

TRIAGE 使用 Service Catalog；无时间默认最近 30 分钟。BASELINE 硬性查询 health、P95、5xx、CPU/memory、异常日志。ANALYZE 只生成候选。INVESTIGATE 选择专项工具。VERIFY 过滤空结果并要求至少两个独立证据源。REPORT 输出统一结构。

## 专项策略

- latency：Trace → Slow Query → EXPLAIN → runtime image/SHA → source。
- 5xx：错误日志 → Hikari/slow query → K8s 状态。
- pod restart：Pod/Event/lastState → memory → image/SHA → source。
- dependency timeout：Trace → downstream metrics/logs → retry/source。
- deployment regression：image/SHA → commit → GOOD..BAD files/diff → runtime evidence。

Ollama 只通过 Gateway 生成基于既有事实的短 decision summary，不参与绕过证据门槛，也不向前端输出隐藏 Chain-of-Thought。
