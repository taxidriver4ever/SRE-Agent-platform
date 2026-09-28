// Command server wires queue workers and the HTTP boundary.
package main

import (
	"context"
	"local/sre-lab/notification-service/internal/handler"
	"local/sre-lab/notification-service/internal/observability"
	"local/sre-lab/notification-service/internal/queue"
	"local/sre-lab/notification-service/internal/service"
	"log"
	"net/http"
	"os"
	"time"
)

// Bounded queue and workers apply backpressure before process memory is exhausted.
func main() {
	version := env("SERVICE_VERSION", "dev")
	pod := env("POD_NAME", "local")
	jobs := queue.New(1000)
	metrics := &observability.Metrics{Version: version, Pod: pod, QueueDepth: jobs.Depth}
	application := service.New(jobs, metrics)
	application.Start(context.Background(), 4)
	server := &http.Server{Addr: ":8084", Handler: handler.New(application, metrics, version, pod).Routes(), ReadHeaderTimeout: 5 * time.Second}
	metrics.Log("INFO", "", "notification-service started", map[string]any{"port": 8084})
	log.Fatal(server.ListenAndServe())
}
func env(name, fallback string) string {
	if value := os.Getenv(name); value != "" {
		return value
	}
	return fallback
}
