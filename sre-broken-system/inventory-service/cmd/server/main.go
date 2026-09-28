// Command server wires inventory components without placing business logic in main.
package main

import (
	"local/sre-lab/inventory-service/internal/client"
	"local/sre-lab/inventory-service/internal/config"
	"local/sre-lab/inventory-service/internal/handler"
	"local/sre-lab/inventory-service/internal/observability"
	"local/sre-lab/inventory-service/internal/repository"
	"local/sre-lab/inventory-service/internal/service"
	"log"
	"net/http"
	"os"
	"time"
)

func main() {
	cfg := config.Load()
	telemetry := &observability.Telemetry{Version: cfg.Version, PodName: cfg.PodName, OTLP: cfg.OTLPEndpoint}
	repo := repository.NewMemoryRepository()
	if err := repo.Open(os.Getenv("INVENTORY_DATA_FILE")); err != nil {
		log.Fatal(err)
	}
	application := service.New(repo, client.NewRecommendationClient(cfg.RecommendationBaseURL))
	server := &http.Server{Addr: cfg.Address, Handler: handler.New(application, telemetry).Routes(), ReadHeaderTimeout: 5 * time.Second}
	telemetry.Log("INFO", "", "inventory-service started", map[string]any{"address": cfg.Address})
	log.Fatal(server.ListenAndServe())
}
