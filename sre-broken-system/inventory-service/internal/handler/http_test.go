package handler

import (
	"encoding/json"
	"local/sre-lab/inventory-service/internal/client"
	"local/sre-lab/inventory-service/internal/observability"
	"local/sre-lab/inventory-service/internal/repository"
	"local/sre-lab/inventory-service/internal/service"
	"net/http/httptest"
	"strings"
	"testing"
)

func TestHTTPHealthAndFaults(t *testing.T) {
	telemetry := &observability.Telemetry{Version: "test"}
	h := New(service.New(repository.NewMemoryRepository(), client.NewRecommendationClient("http://unused")), telemetry)
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
func TestInjectedFailureIsNotBusinessConflict(t *testing.T) {
	svc := service.New(repository.NewMemoryRepository(), client.NewRecommendationClient("http://unused"))
	svc.SetFault("random_error")
	defer svc.SetFault("normal")
	routes := New(svc, &observability.Telemetry{Version: "test"}).Routes()
	for _, request := range []*struct{ method, path, body string }{
		{"GET", "/inventory/SKU-1", ""},
		{"POST", "/inventory/reserve", `{"sku":"SKU-1","quantity":1,"reservation_id":"test"}`},
	} {
		w := httptest.NewRecorder()
		routes.ServeHTTP(w, httptest.NewRequest(request.method, request.path, strings.NewReader(request.body)))
		if w.Code != 503 {
			t.Fatal(request.path, w.Code, w.Body.String())
		}
	}
}
