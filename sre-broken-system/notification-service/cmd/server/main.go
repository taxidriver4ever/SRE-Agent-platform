// Command server wires queue workers and the HTTP boundary.
package main

import("context";"log";"net/http";"os";"time";"local/sre-lab/notification-service/internal/handler";"local/sre-lab/notification-service/internal/observability";"local/sre-lab/notification-service/internal/queue";"local/sre-lab/notification-service/internal/service")
// BAD: a single worker and oversized in-memory buffer hide backpressure until a large queue accumulates.
func main(){version:=env("SERVICE_VERSION","dev");pod:=env("POD_NAME","local");jobs:=queue.New(50000);metrics:=&observability.Metrics{Version:version,Pod:pod,QueueDepth:jobs.Depth};application:=service.New(jobs,metrics);application.Start(context.Background(),1);server:=&http.Server{Addr:":8084",Handler:handler.New(application,metrics,version,pod).Routes(),ReadHeaderTimeout:5*time.Second};metrics.Log("INFO","","notification-service started",map[string]any{"port":8084});log.Fatal(server.ListenAndServe())}
func env(name,fallback string)string{if value:=os.Getenv(name);value!=""{return value};return fallback}
