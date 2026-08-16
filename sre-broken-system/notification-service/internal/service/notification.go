// Package service implements asynchronous delivery, retry and controlled queue failures.
package service

import("context";"errors";"strings";"sync";"time";"local/sre-lab/notification-service/internal/domain";"local/sre-lab/notification-service/internal/observability";"local/sre-lab/notification-service/internal/queue")

// NotificationService owns workers and a per-Pod fault mode.
type NotificationService struct{queue *queue.MemoryQueue;metrics *observability.Metrics;mu sync.RWMutex;fault string;leaked []chan struct{}}
func New(queue *queue.MemoryQueue,metrics *observability.Metrics)*NotificationService{return &NotificationService{queue:queue,metrics:metrics,fault:"normal"}}

// Start launches workers that stop with the process context.
func(s *NotificationService)Start(ctx context.Context,workers int){for index:=0;index<workers;index++{go s.worker(ctx)}}
func(s *NotificationService)Submit(job domain.Notification)error{if strings.TrimSpace(job.Type)==""{return errors.New("notification type is required")};job.Status="QUEUED";job.CreatedAt=time.Now().UTC();if err:=s.queue.Enqueue(job);err!=nil{return err};s.metrics.Accepted.Add(1);return nil}
func(s *NotificationService)Get(id string)(domain.Notification,bool){return s.queue.Get(id)}

// worker simulates an external provider while preserving real queue and retry behavior.
func(s *NotificationService)worker(ctx context.Context){for{select{case<-ctx.Done():return;default:id,ok:=s.queue.Next();if !ok{return};job,_:=s.queue.Get(id);job.Attempts++;mode:=s.Fault();if mode=="queue_backlog"{time.Sleep(2*time.Second)};if mode=="external_unstable"&&job.Attempts<3{job.Status="RETRYING";s.metrics.Failed.Add(1);s.queue.Update(job);time.AfterFunc(200*time.Millisecond,func(){_=s.queue.Enqueue(job)});continue};if mode=="goroutine_leak"{leak:=make(chan struct{});s.mu.Lock();s.leaked=append(s.leaked,leak);s.mu.Unlock();go func(){<-leak}()};job.Status="DELIVERED";s.queue.Update(job);s.metrics.Delivered.Add(1);s.metrics.Log("INFO",traceID(job.Traceparent),"notification delivered",map[string]any{"notification_id":job.ID,"attempts":job.Attempts})}}}

func(s *NotificationService)SetFault(mode string)bool{if mode!="normal"&&mode!="queue_backlog"&&mode!="external_unstable"&&mode!="goroutine_leak"{return false};s.mu.Lock();defer s.mu.Unlock();s.fault=mode;if mode=="normal"{for _,leak:=range s.leaked{close(leak)};s.leaked=nil};return true}
func(s *NotificationService)Fault()string{s.mu.RLock();defer s.mu.RUnlock();return s.fault}
func traceID(traceparent string)string{parts:=strings.Split(traceparent,"-");if len(parts)==4{return parts[1]};return ""}
