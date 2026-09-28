// Package handler maps HTTP requests to inventory use cases and consistent responses.
package handler

import (
	"encoding/json"
	"errors"
	"local/sre-lab/inventory-service/internal/domain"
	"local/sre-lab/inventory-service/internal/faults"
	"local/sre-lab/inventory-service/internal/observability"
	"local/sre-lab/inventory-service/internal/service"
	"net/http"
	"strconv"
	"strings"
)

// Handler owns transport concerns while service and repository remain HTTP-independent.
type Handler struct {
	service   *service.InventoryService
	telemetry *observability.Telemetry
}

func New(service *service.InventoryService, telemetry *observability.Telemetry) *Handler {
	return &Handler{service, telemetry}
}

// Routes registers health, metrics, business and controlled fault endpoints.
func (h *Handler) Routes() http.Handler {
	mux := http.NewServeMux()
	mux.HandleFunc("/health", h.health)
	mux.HandleFunc("/ready", h.health)
	mux.HandleFunc("/inventory/reserve", h.reserve)
	mux.HandleFunc("/inventory/release", h.finish)
	mux.HandleFunc("/inventory/commit", h.finish)
	mux.HandleFunc("/metrics", h.telemetry.Metrics)
	mux.HandleFunc("/inventory/reservations", h.reserve)
	mux.HandleFunc("/inventory/reservations/", h.release)
	mux.HandleFunc("/inventory/", h.stock)
	mux.HandleFunc("/debug/fault", h.fault)
	mux.HandleFunc("/internal/faults", faults.API)
	mux.HandleFunc("DELETE /internal/faults/{fault}", faults.API)
	return observability.Middleware("inventory-service", mux)
}
func (h *Handler) health(w http.ResponseWriter, _ *http.Request) {
	h.write(w, 200, map[string]any{"status": "ok", "service": "inventory-service", "fault_mode": h.service.FaultMode(), "version": h.telemetry.Version})
}
func (h *Handler) stock(w http.ResponseWriter, r *http.Request) {
	sku := strings.TrimPrefix(r.URL.Path, "/inventory/")
	if _, err := strconv.ParseInt(sku, 10, 64); err == nil {
		sku = "SKU-" + sku
	}
	stock, err := h.service.Stock(r.Context(), sku, r.Header.Get("traceparent"))
	if err != nil {
		h.write(w, inventoryErrorStatus(err, 404), map[string]string{"error": err.Error()})
		return
	}
	h.telemetry.Log("INFO", r.Header.Get("traceparent"), "inventory stock read", map[string]any{"sku": sku, "fault_mode": h.service.FaultMode()})
	h.write(w, 200, stock)
}
func (h *Handler) reserve(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodPost {
		h.write(w, 405, map[string]string{"error": "method not allowed"})
		return
	}
	var input struct {
		SKU      string `json:"sku"`
		Quantity int    `json:"quantity"`
		ID       string `json:"reservation_id"`
	}
	if json.NewDecoder(r.Body).Decode(&input) != nil {
		h.write(w, 400, map[string]string{"error": "invalid JSON"})
		return
	}
	stock, err := h.service.Reserve(domain.Reservation{ID: input.ID, SKU: input.SKU, Quantity: input.Quantity})
	if err != nil {
		h.write(w, inventoryErrorStatus(err, 409), map[string]string{"error": err.Error()})
		return
	}
	h.write(w, 201, stock)
}
func (h *Handler) release(w http.ResponseWriter, r *http.Request) {
	if r.Method != http.MethodDelete {
		h.write(w, 405, map[string]string{"error": "method not allowed"})
		return
	}
	stock, err := h.service.Release(strings.TrimPrefix(r.URL.Path, "/inventory/reservations/"))
	if err != nil {
		h.write(w, 404, map[string]string{"error": err.Error()})
		return
	}
	h.write(w, 200, stock)
}
func inventoryErrorStatus(err error, businessStatus int) int {
	if errors.Is(err, service.ErrInjectedFailure) {
		return http.StatusServiceUnavailable
	}
	return businessStatus
}
func (h *Handler) fault(w http.ResponseWriter, r *http.Request) {
	if r.Method == http.MethodPost && !h.service.SetFault(r.URL.Query().Get("mode")) {
		h.write(w, 400, map[string]string{"error": "unsupported mode"})
		return
	}
	h.write(w, 200, map[string]string{"fault_mode": h.service.FaultMode(), "version": h.telemetry.Version})
}
func (h *Handler) write(w http.ResponseWriter, status int, value any) {
	w.Header().Set("Content-Type", "application/json")
	if status >= 400 {
		message := "request failed"
		if m, ok := value.(map[string]string); ok && m["error"] != "" {
			message = m["error"]
		}
		value = map[string]string{"code": "REQUEST_FAILED", "message": message, "request_id": w.Header().Get("X-Request-ID")}
	}
	w.WriteHeader(status)
	_ = json.NewEncoder(w).Encode(value)
}

func (h *Handler) finish(w http.ResponseWriter, r *http.Request) {
	if r.Method != "POST" {
		h.write(w, 405, map[string]string{"error": "method not allowed"})
		return
	}
	var input struct {
		ID string `json:"reservation_id"`
	}
	if json.NewDecoder(r.Body).Decode(&input) != nil || input.ID == "" {
		h.write(w, 400, map[string]string{"error": "reservation_id required"})
		return
	}
	var stock domain.Stock
	var err error
	if r.URL.Path == "/inventory/commit" {
		stock, err = h.service.Commit(input.ID)
	} else {
		stock, err = h.service.Release(input.ID)
	}
	if err != nil {
		h.write(w, 409, map[string]string{"error": err.Error()})
		return
	}
	h.write(w, 200, stock)
}
