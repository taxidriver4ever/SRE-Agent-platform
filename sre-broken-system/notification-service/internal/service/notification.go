// Package service implements asynchronous delivery, retry and controlled queue failures.
package service

import (
	"context"
	"errors"
	"local/sre-lab/notification-service/internal/domain"
	"local/sre-lab/notification-service/internal/faults"
	"local/sre-lab/notification-service/internal/observability"
	"local/sre-lab/notification-service/internal/queue"
	"strings"
	"sync"
	"time"
)

// NotificationService owns workers and a per-Pod fault mode.
type NotificationService struct {
	queue   *queue.MemoryQueue
	metrics *observability.Metrics
	mu      sync.RWMutex
	fault   string
	leaked  []chan struct{}
}

func New(queue *queue.MemoryQueue, metrics *observability.Metrics) *NotificationService {
	return &NotificationService{queue: queue, metrics: metrics, fault: "normal"}
}

// Start launches workers that stop with the process context.
func (s *NotificationService) Start(ctx context.Context, workers int) {
	go func() {
		ticker := time.NewTicker(time.Second)
		defer ticker.Stop()
		for {
			select {
			case <-ctx.Done():
				return
			case <-ticker.C:
				s.Fault()
			}
		}
	}()
	for index := 0; index < workers; index++ {
		go s.worker(ctx)
	}
}
func (s *NotificationService) Submit(job domain.Notification) error {
	if strings.TrimSpace(job.Type) == "" {
		return errors.New("notification type is required")
	}
	mode := s.Fault()
	if mode == "random_error" && faults.Fail() {
		s.metrics.Failed.Add(1)
		return errors.New("simulated notification failure")
	}
	if mode == "timeout" || mode == "slow_downstream" {
		time.Sleep(faults.Delay())
	}
	job.Status = "QUEUED"
	job.CreatedAt = time.Now().UTC()
	if err := s.queue.Enqueue(job); err != nil {
		return err
	}
	s.metrics.Accepted.Add(1)
	return nil
}
func (s *NotificationService) Get(id string) (domain.Notification, bool) { return s.queue.Get(id) }

// worker simulates an external provider while preserving real queue and retry behavior.
func (s *NotificationService) worker(ctx context.Context) {
	for {
		select {
		case <-ctx.Done():
			return
		default:
			id, ok := s.queue.Next()
			if !ok {
				return
			}
			job, _ := s.queue.Get(id)
			job.Attempts++
			mode := s.Fault()
			if mode == "queue_backlog" {
				time.Sleep(2 * time.Second)
			}
			if mode == "external_unstable" && job.Attempts < 3 {
				job.Status = "RETRYING"
				s.metrics.Failed.Add(1)
				s.queue.Update(job)
				time.AfterFunc(200*time.Millisecond, func() { _ = s.queue.Enqueue(job) })
				continue
			}
			if mode == "goroutine_leak" {
				leak := make(chan struct{})
				s.mu.Lock()
				s.leaked = append(s.leaked, leak)
				s.mu.Unlock()
				go func() { <-leak }()
			}
			job.Status = "DELIVERED"
			s.queue.Update(job)
			s.metrics.Delivered.Add(1)
			s.metrics.Log("INFO", traceID(job.Traceparent), "notification delivered", map[string]any{"notification_id": job.ID, "attempts": job.Attempts})
		}
	}
}

func init() {
	faults.Allow("queue_backlog", "external_unstable", "goroutine_leak", "slow_downstream", "random_error", "timeout")
}
func (s *NotificationService) SetFault(mode string) bool { return faults.Set(mode, 120, 3000, 1) }
func (s *NotificationService) Fault() string {
	mode := faults.Get()
	if mode == "normal" {
		s.mu.Lock()
		for _, leak := range s.leaked {
			close(leak)
		}
		s.leaked = nil
		s.mu.Unlock()
	}
	return mode
}
func traceID(traceparent string) string {
	parts := strings.Split(traceparent, "-")
	if len(parts) == 4 {
		return parts[1]
	}
	return ""
}
