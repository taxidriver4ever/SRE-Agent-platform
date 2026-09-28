package handler

import (
	"encoding/json"
	"local/sre-lab/notification-service/internal/observability"
	"local/sre-lab/notification-service/internal/queue"
	"local/sre-lab/notification-service/internal/service"
	"net/http/httptest"
	"strings"
	"testing"
)

func TestHTTPHealthAndFaults(t *testing.T) {
	q := queue.New(10)
	m := &observability.Metrics{QueueDepth: q.Depth}
	h := New(service.New(q, m), m, "test", "test")
	routes := h.Routes()
	for _, path := range []string{"/health", "/ready", "/metrics", "/internal/faults"} {
		r := httptest.NewRequest("GET", path, nil)
		r.Header.Set("x-request-id", "contract-123")
		w := httptest.NewRecorder()
		routes.ServeHTTP(w, r)
		if w.Code != 200 || w.Header().Get("X-Request-ID") != "contract-123" {
			t.Fatal(path, w.Code, w.Body.String())
		}
	}
	r := httptest.NewRequest("POST", "/internal/faults", strings.NewReader(`{"fault":"unknown","duration_seconds":900}`))
	w := httptest.NewRecorder()
	routes.ServeHTTP(w, r)
	if w.Code != 400 {
		t.Fatal(w.Code)
	}
	var body map[string]any
	if json.Unmarshal(w.Body.Bytes(), &body) != nil || body["request_id"] == "" {
		t.Fatal("missing error contract")
	}
}
