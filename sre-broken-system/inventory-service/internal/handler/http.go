// Package handler maps HTTP requests to inventory use cases and consistent responses.
package handler

import("encoding/json";"net/http";"strings";"time";"local/sre-lab/inventory-service/internal/domain";"local/sre-lab/inventory-service/internal/observability";"local/sre-lab/inventory-service/internal/service")

// Handler owns transport concerns while service and repository remain HTTP-independent.
type Handler struct{service *service.InventoryService;telemetry *observability.Telemetry}
func New(service *service.InventoryService,telemetry *observability.Telemetry)*Handler{return &Handler{service,telemetry}}

// Routes registers health, metrics, business and controlled fault endpoints.
func(h *Handler)Routes()http.Handler{mux:=http.NewServeMux();mux.HandleFunc("/health",h.health);mux.HandleFunc("/metrics",h.telemetry.Metrics);mux.HandleFunc("/inventory/reservations",h.reserve);mux.HandleFunc("/inventory/reservations/",h.release);mux.HandleFunc("/inventory/",h.stock);mux.HandleFunc("/debug/fault",h.fault);return mux}
func(h *Handler)health(w http.ResponseWriter,_ *http.Request){h.write(w,200,map[string]any{"status":"ok","service":"inventory-service","fault_mode":h.service.FaultMode(),"version":h.telemetry.Version})}
func(h *Handler)stock(w http.ResponseWriter,r *http.Request){started:=time.Now();traceID,parentID,spanID:=observability.TraceContext(r.Header.Get("traceparent"));failed:=false;defer func(){h.telemetry.Observe(time.Since(started),failed);h.telemetry.ExportSpan(traceID,parentID,spanID,"GET /inventory/{sku}",started)}();sku:=strings.TrimPrefix(r.URL.Path,"/inventory/");stock,err:=h.service.Stock(r.Context(),sku,r.Header.Get("traceparent"));if err!=nil{failed=true;h.write(w,404,map[string]string{"error":err.Error()});return};h.telemetry.Log("INFO",traceID,"inventory stock read",map[string]any{"sku":sku,"fault_mode":h.service.FaultMode()});h.write(w,200,stock)}
func(h *Handler)reserve(w http.ResponseWriter,r *http.Request){if r.Method!=http.MethodPost{h.write(w,405,map[string]string{"error":"method not allowed"});return};var input struct{SKU string `json:"sku"`;Quantity int `json:"quantity"`;ID string `json:"reservation_id"`};if json.NewDecoder(r.Body).Decode(&input)!=nil{h.write(w,400,map[string]string{"error":"invalid JSON"});return};stock,err:=h.service.Reserve(domain.Reservation{ID:input.ID,SKU:input.SKU,Quantity:input.Quantity});if err!=nil{h.write(w,409,map[string]string{"error":err.Error()});return};h.write(w,201,stock)}
func(h *Handler)release(w http.ResponseWriter,r *http.Request){if r.Method!=http.MethodDelete{h.write(w,405,map[string]string{"error":"method not allowed"});return};stock,err:=h.service.Release(strings.TrimPrefix(r.URL.Path,"/inventory/reservations/"));if err!=nil{h.write(w,404,map[string]string{"error":err.Error()});return};h.write(w,200,stock)}
func(h *Handler)fault(w http.ResponseWriter,r *http.Request){if r.Method==http.MethodPost&&!h.service.SetFault(r.URL.Query().Get("mode")){h.write(w,400,map[string]string{"error":"unsupported mode"});return};h.write(w,200,map[string]string{"fault_mode":h.service.FaultMode(),"version":h.telemetry.Version})}
func(h *Handler)write(w http.ResponseWriter,status int,value any){w.Header().Set("Content-Type","application/json");w.WriteHeader(status);_=json.NewEncoder(w).Encode(value)}
