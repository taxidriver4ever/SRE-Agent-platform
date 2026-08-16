// Package handler implements the notification HTTP transport and validation boundary.
package handler

import("crypto/rand";"encoding/hex";"encoding/json";"net/http";"strings";"local/sre-lab/notification-service/internal/domain";"local/sre-lab/notification-service/internal/observability";"local/sre-lab/notification-service/internal/service")

type Handler struct{service *service.NotificationService;metrics *observability.Metrics;version,pod string}
func New(service *service.NotificationService,metrics *observability.Metrics,version,pod string)*Handler{return &Handler{service,metrics,version,pod}}
func(h *Handler)Routes()http.Handler{mux:=http.NewServeMux();mux.HandleFunc("/health",h.health);mux.Handle("/metrics",h.metrics);mux.HandleFunc("/notifications",h.create);mux.HandleFunc("/notifications/",h.get);mux.HandleFunc("/debug/fault",h.fault);return mux}
func(h *Handler)health(w http.ResponseWriter,_ *http.Request){h.write(w,200,map[string]any{"status":"ok","service":"notification-service","version":h.version,"pod":h.pod,"fault_mode":h.service.Fault()})}
func(h *Handler)create(w http.ResponseWriter,r *http.Request){if r.Method!=http.MethodPost{h.write(w,405,map[string]string{"error":"method not allowed"});return};var input domain.Notification;if json.NewDecoder(r.Body).Decode(&input)!=nil{h.write(w,400,map[string]string{"error":"invalid JSON"});return};input.ID=randomID();input.Traceparent=r.Header.Get("traceparent");if err:=h.service.Submit(input);err!=nil{h.write(w,503,map[string]string{"error":err.Error()});return};h.write(w,202,input)}
func(h *Handler)get(w http.ResponseWriter,r *http.Request){job,ok:=h.service.Get(strings.TrimPrefix(r.URL.Path,"/notifications/"));if !ok{h.write(w,404,map[string]string{"error":"not found"});return};h.write(w,200,job)}
func(h *Handler)fault(w http.ResponseWriter,r *http.Request){if r.Method==http.MethodPost&&!h.service.SetFault(r.URL.Query().Get("mode")){h.write(w,400,map[string]string{"error":"unsupported mode"});return};h.write(w,200,map[string]string{"fault_mode":h.service.Fault()})}
func(h *Handler)write(w http.ResponseWriter,status int,value any){w.Header().Set("Content-Type","application/json");w.WriteHeader(status);_=json.NewEncoder(w).Encode(value)}
func randomID()string{buffer:=make([]byte,12);_,_=rand.Read(buffer);return hex.EncodeToString(buffer)}
