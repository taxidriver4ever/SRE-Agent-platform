// Package faults provides bounded, expiring per-Pod experiments.
package faults

import (
	"encoding/json"
	"math"
	"math/rand"
	"net/http"
	"sync"
	"time"
)

var mu sync.Mutex
var mode = "normal"
var expires time.Time
var delay = 3000
var rate = 1.0
var allowed = map[string]bool{}

func Allow(names ...string) {
	mu.Lock()
	defer mu.Unlock()
	allowed["normal"] = true
	for _, n := range names {
		allowed[n] = true
	}
}
func Get() string {
	mu.Lock()
	defer mu.Unlock()
	if time.Now().After(expires) {
		mode = "normal"
	}
	return mode
}
func Set(name string, seconds int, ms int, probability float64) bool {
	mu.Lock()
	defer mu.Unlock()
	if !allowed[name] || seconds < 1 || seconds > 300 || ms < 0 || ms > 10000 || math.IsNaN(probability) || probability < 0 || probability > 1 {
		return false
	}
	mode = name
	expires = time.Now().Add(time.Duration(seconds) * time.Second)
	delay = ms
	rate = probability
	return true
}
func Delay() time.Duration {
	mu.Lock()
	defer mu.Unlock()
	return time.Duration(delay) * time.Millisecond
}
func Fail() bool { mu.Lock(); defer mu.Unlock(); return rand.Float64() < rate }
func Snapshot() map[string]any {
	current := Get()
	mu.Lock()
	defer mu.Unlock()
	return map[string]any{"fault_mode": current, "expires_at": expires.UTC(), "parameters": map[string]any{"delay_ms": delay, "error_rate": rate}}
}
func API(w http.ResponseWriter, r *http.Request) {
	w.Header().Set("Content-Type", "application/json")
	if r.Method == "POST" {
		body := struct {
			Fault      string `json:"fault"`
			Duration   int    `json:"duration_seconds"`
			Parameters struct {
				Delay int     `json:"delay_ms"`
				Rate  float64 `json:"error_rate"`
			} `json:"parameters"`
		}{Duration: 120}
		body.Parameters.Delay = 3000
		body.Parameters.Rate = 1
		if json.NewDecoder(http.MaxBytesReader(w, r.Body, 4096)).Decode(&body) != nil || !Set(body.Fault, body.Duration, body.Parameters.Delay, body.Parameters.Rate) {
			w.WriteHeader(400)
			json.NewEncoder(w).Encode(map[string]string{"code": "INVALID_FAULT", "message": "invalid fault parameters", "request_id": w.Header().Get("X-Request-ID")})
			return
		}
	} else if r.Method == "DELETE" {
		if r.PathValue("fault") == Get() {
			Set("normal", 120, 3000, 1)
		}
	} else if r.Method != "GET" {
		w.WriteHeader(405)
		return
	}
	json.NewEncoder(w).Encode(Snapshot())
}
