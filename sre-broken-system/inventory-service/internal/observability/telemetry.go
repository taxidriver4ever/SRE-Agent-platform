// Package observability implements bounded Prometheus metrics, JSON logs and OTLP spans.
package observability

import (
	"bytes"
	"crypto/rand"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"net/http"
	"strings"
	"sync/atomic"
	"time"
)

// Telemetry stores process counters and immutable service identity.
type Telemetry struct {
	Version        string
	PodName        string
	OTLP           string
	Requests       atomic.Uint64
	Errors         atomic.Uint64
	GoroutineLeaks atomic.Uint64
	LatencyBuckets [6]atomic.Uint64
}

var bounds = [...]float64{0.01, 0.05, 0.1, 0.5, 1, 5}

// Observe records request totals and cumulative Prometheus histogram buckets.
func (t *Telemetry) Observe(duration time.Duration, failed bool) {
	t.Requests.Add(1)
	if failed {
		t.Errors.Add(1)
	}
	for index, bound := range bounds {
		if duration.Seconds() <= bound {
			t.LatencyBuckets[index].Add(1)
		}
	}
}

// Metrics writes version/pod labels so mixed-version degradation is directly queryable.
func (t *Telemetry) Metrics(w http.ResponseWriter, _ *http.Request) {
	w.Header().Set("Content-Type", "text/plain; version=0.0.4")
	labels := fmt.Sprintf("service=\"inventory-service\",version=\"%s\",pod=\"%s\"", t.Version, t.PodName)
	fmt.Fprintf(w, "# TYPE sre_http_requests_total counter\nsre_http_requests_total{%s} %d\n", labels, t.Requests.Load())
	fmt.Fprintf(w, "# TYPE sre_http_errors_total counter\nsre_http_errors_total{%s} %d\n", labels, t.Errors.Load())
	for index, bound := range bounds {
		fmt.Fprintf(w, "sre_http_request_duration_seconds_bucket{%s,le=\"%g\"} %d\n", labels, bound, t.LatencyBuckets[index].Load())
	}
	fmt.Fprintf(w, "sre_http_request_duration_seconds_bucket{%s,le=\"+Inf\"} %d\n", labels, t.Requests.Load())
	fmt.Fprintf(w, "# TYPE sre_inventory_goroutine_leaks gauge\nsre_inventory_goroutine_leaks{%s} %d\n", labels, t.GoroutineLeaks.Load())
}

// TraceContext reuses an incoming W3C trace ID and creates a new child span ID.
func TraceContext(traceparent string) (traceID, parentSpanID, spanID string) {
	parts := strings.Split(traceparent, "-")
	if len(parts) == 4 && len(parts[1]) == 32 && len(parts[2]) == 16 {
		traceID, parentSpanID = parts[1], parts[2]
	}
	if traceID == "" {
		traceID = randomHex(16)
	}
	return traceID, parentSpanID, randomHex(8)
}

// ExportSpan sends one server span over OTLP/HTTP JSON; telemetry failure never fails inventory.
func (t *Telemetry) ExportSpan(traceID, parentSpanID, spanID, name string, started time.Time) {
	if t.OTLP == "" {
		return
	}
	span := map[string]any{
		"traceId": traceID, "spanId": spanID, "name": name, "kind": 2,
		"startTimeUnixNano": fmt.Sprint(started.UnixNano()),
		"endTimeUnixNano":   fmt.Sprint(time.Now().UnixNano()),
	}
	if parentSpanID != "" {
		span["parentSpanId"] = parentSpanID
	}
	resource := map[string]any{"attributes": []any{
		map[string]any{"key": "service.name", "value": map[string]any{"stringValue": "inventory-service"}},
		map[string]any{"key": "service.version", "value": map[string]any{"stringValue": t.Version}},
	}}
	scope := map[string]any{"scope": map[string]any{"name": "inventory-service"}, "spans": []any{span}}
	payload := map[string]any{"resourceSpans": []any{map[string]any{
		"resource": resource, "scopeSpans": []any{scope},
	}}}
	body, _ := json.Marshal(payload)
	request, _ := http.NewRequest(http.MethodPost, strings.TrimRight(t.OTLP, "/")+"/v1/traces", bytes.NewReader(body))
	request.Header.Set("Content-Type", "application/json")
	if response, err := (&http.Client{Timeout: time.Second}).Do(request); err == nil {
		response.Body.Close()
	}
}

func randomHex(size int) string {
	buffer := make([]byte, size)
	_, _ = rand.Read(buffer)
	return hex.EncodeToString(buffer)
}

// Log emits the common cross-language envelope consumed by Alloy and Loki.
func (t *Telemetry) Log(level, traceID, message string, fields map[string]any) {
	record := map[string]any{"timestamp": time.Now().UTC().Format(time.RFC3339Nano), "service": "inventory-service",
		"version": t.Version, "pod": t.PodName, "level": level, "trace_id": traceID, "message": message}
	for key, value := range fields {
		record[key] = value
	}
	encoded, _ := json.Marshal(record)
	fmt.Println(string(encoded))
}
