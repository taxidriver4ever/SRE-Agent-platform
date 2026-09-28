// W3C middleware exports OTLP spans through a bounded non-blocking queue.
package observability

import (
	"bytes"
	"context"
	"crypto/rand"
	"encoding/hex"
	"encoding/json"
	"fmt"
	"net"
	"net/http"
	"os"
	"regexp"
	"runtime"
	"strings"
	"sync/atomic"
	"time"
)

type ctxKey string

var spanQueue = make(chan []byte, 256)
var reqCount, errCount, activeCount, latencyNano atomic.Int64
var buckets [7]atomic.Int64
var validID = regexp.MustCompile(`^[a-zA-Z0-9._-]{1,128}$`)
var validTrace = regexp.MustCompile(`^00-[0-9a-f]{32}-[0-9a-f]{16}-[0-9a-f]{2}$`)

func hexID(n int) string { b := make([]byte, n); _, _ = rand.Read(b); return hex.EncodeToString(b) }
func init() {
	go func() {
		client := &http.Client{Timeout: time.Second, Transport: &http.Transport{DialContext: (&net.Dialer{Timeout: 200 * time.Millisecond}).DialContext}}
		for body := range spanQueue {
			url := strings.TrimRight(os.Getenv("OTEL_EXPORTER_OTLP_ENDPOINT"), "/")
			if url == "" || os.Getenv("OTEL_SDK_DISABLED") == "true" {
				continue
			}
			r, e := http.NewRequest("POST", url+"/v1/traces", bytes.NewReader(body))
			if e != nil {
				continue
			}
			r.Header.Set("Content-Type", "application/json")
			if res, e := client.Do(r); e == nil {
				res.Body.Close()
			}
		}
	}()
}
func emit(service, trace, parent, span, name string, kind int, start time.Time, status int) {
	attrs := []any{map[string]any{"key": "http.status_code", "value": map[string]any{"intValue": fmt.Sprint(status)}}}
	payload := map[string]any{"resourceSpans": []any{map[string]any{"resource": map[string]any{"attributes": []any{map[string]any{"key": "service.name", "value": map[string]any{"stringValue": service}}}}, "scopeSpans": []any{map[string]any{"scope": map[string]string{"name": "sre-lab-http"}, "spans": []any{map[string]any{"traceId": trace, "parentSpanId": parent, "spanId": span, "name": name, "kind": kind, "startTimeUnixNano": fmt.Sprint(start.UnixNano()), "endTimeUnixNano": fmt.Sprint(time.Now().UnixNano()), "attributes": attrs, "status": map[string]int{"code": func() int {
		if status >= 500 {
			return 2
		}
		return 1
	}()}}}}}}}}
	b, _ := json.Marshal(payload)
	select {
	case spanQueue <- b:
	default:
	}
}

type statusWriter struct {
	http.ResponseWriter
	status int
}

func (w *statusWriter) WriteHeader(status int) {
	w.status = status
	w.ResponseWriter.WriteHeader(status)
}
func Middleware(service string, next http.Handler) http.Handler {
	return http.HandlerFunc(func(w http.ResponseWriter, r *http.Request) {
		started := time.Now()
		rid := r.Header.Get("x-request-id")
		if !validID.MatchString(rid) {
			rid = hexID(16)
		}
		traceID, parent, span := hexID(16), "", hexID(8)
		tp := r.Header.Get("traceparent")
		if validTrace.MatchString(tp) {
			parts := strings.Split(tp, "-")
			if parts[1] != strings.Repeat("0", 32) && parts[2] != strings.Repeat("0", 16) {
				traceID = parts[1]
				parent = parts[2]
			}
		}
		ctx := context.WithValue(r.Context(), ctxKey("traceparent"), "00-"+traceID+"-"+span+"-01")
		ctx = context.WithValue(ctx, ctxKey("request_id"), rid)
		ctx = context.WithValue(ctx, ctxKey("tracestate"), r.Header.Get("tracestate"))
		r = r.WithContext(ctx)
		r.Header.Set("traceparent", "00-"+traceID+"-"+span+"-01")
		r.Header.Set("x-request-id", rid)
		w.Header().Set("X-Request-ID", rid)
		sw := &statusWriter{w, 200}
		activeCount.Add(1)
		defer func() {
			activeCount.Add(-1)
			reqCount.Add(1)
			elapsed := time.Since(started)
			latencyNano.Add(elapsed.Nanoseconds())
			if sw.status >= 500 {
				errCount.Add(1)
			}
			for i, bound := range []float64{.01, .05, .1, .5, 1, 5, 1000000} {
				if elapsed.Seconds() <= bound {
					buckets[i].Add(1)
				}
			}
			route := r.Pattern
			if route == "" {
				route = "unmatched"
			}
			emit(service, traceID, parent, span, r.Method+" "+route, 2, started, sw.status)
			b, _ := json.Marshal(map[string]any{"timestamp": time.Now().UTC(), "service": service, "environment": "lab", "level": func() string {
				if sw.status >= 500 {
					return "ERROR"
				}
				return "INFO"
			}(), "trace_id": traceID, "span_id": span, "request_id": rid, "endpoint": route, "latency_ms": float64(elapsed.Microseconds()) / 1000, "status": sw.status, "event": "http_request"})
			fmt.Println(string(b))
		}()
		next.ServeHTTP(sw, r)
	})
}
func ClientSpan(ctx context.Context, service, name string) (map[string]string, func(int)) {
	tp, _ := ctx.Value(ctxKey("traceparent")).(string)
	parts := strings.Split(tp, "-")
	traceID, parent := hexID(16), ""
	if len(parts) == 4 {
		traceID = parts[1]
		parent = parts[2]
	}
	span := hexID(8)
	started := time.Now()
	rid, _ := ctx.Value(ctxKey("request_id")).(string)
	state, _ := ctx.Value(ctxKey("tracestate")).(string)
	return map[string]string{"traceparent": "00-" + traceID + "-" + span + "-01", "x-request-id": rid, "tracestate": state}, func(status int) { emit(service, traceID, parent, span, name, 3, started, status) }
}
func HTTPMetrics(w http.ResponseWriter) {
	labels := fmt.Sprintf("service=%q,version=%q,pod=%q", "inventory-service", os.Getenv("SERVICE_VERSION"), os.Getenv("POD_NAME"))
	fmt.Fprintf(w, "sre_http_requests_total{%s} %d\nsre_http_errors_total{%s} %d\nsre_http_active_requests{%s} %d\nsre_http_request_duration_seconds_sum{%s} %g\nsre_http_request_duration_seconds_count{%s} %d\n", labels, reqCount.Load(), labels, errCount.Load(), labels, activeCount.Load(), labels, float64(latencyNano.Load())/1e9, labels, reqCount.Load())
	for i, b := range []string{"0.01", "0.05", "0.1", "0.5", "1", "5", "+Inf"} {
		fmt.Fprintf(w, "sre_http_request_duration_seconds_bucket{%s,le=%q} %d\n", labels, b, buckets[i].Load())
	}
	var mem runtime.MemStats
	runtime.ReadMemStats(&mem)
	fmt.Fprintf(w, "go_goroutines %d\ngo_memstats_alloc_bytes %d\ngo_gc_cycles_total %d\n", runtime.NumGoroutine(), mem.Alloc, mem.NumGC)
}
