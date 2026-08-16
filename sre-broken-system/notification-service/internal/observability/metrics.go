// Package observability provides version-aware metrics and common structured logs.
package observability

import("encoding/json";"fmt";"net/http";"sync/atomic";"time")

// Metrics stores queue and delivery counters without external dependencies.
type Metrics struct{Version,Pod string;Accepted,Delivered,Failed atomic.Uint64;QueueDepth func()int}
func(m *Metrics)ServeHTTP(w http.ResponseWriter,_ *http.Request){w.Header().Set("Content-Type","text/plain; version=0.0.4");labels:=fmt.Sprintf("service=\"notification-service\",version=\"%s\",pod=\"%s\"",m.Version,m.Pod);fmt.Fprintf(w,"sre_notification_accepted_total{%s} %d\n",labels,m.Accepted.Load());fmt.Fprintf(w,"sre_notification_delivered_total{%s} %d\n",labels,m.Delivered.Load());fmt.Fprintf(w,"sre_notification_failed_total{%s} %d\n",labels,m.Failed.Load());fmt.Fprintf(w,"sre_notification_queue_depth{%s} %d\n",labels,m.QueueDepth())}

// Log emits fields understood by Alloy/Loki and retains incoming cross-service trace identity.
func(m *Metrics)Log(level,traceID,message string,fields map[string]any){record:=map[string]any{"timestamp":time.Now().UTC().Format(time.RFC3339Nano),"service":"notification-service","version":m.Version,"pod":m.Pod,"level":level,"trace_id":traceID,"message":message};for key,value:=range fields{record[key]=value};encoded,_:=json.Marshal(record);fmt.Println(string(encoded))}
