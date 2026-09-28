// Package queue implements a bounded in-process work queue without requiring Kafka in version one.
package queue

import (
	"errors"
	"local/sre-lab/notification-service/internal/domain"
	"sync"
)

// MemoryQueue holds pending jobs and status snapshots under one lock.
type MemoryQueue struct {
	mu      sync.RWMutex
	pending chan string
	jobs    map[string]domain.Notification
}

func New(capacity int) *MemoryQueue {
	return &MemoryQueue{pending: make(chan string, capacity), jobs: make(map[string]domain.Notification)}
}

// Enqueue stores the job first and rejects when the bounded queue is full.
func (q *MemoryQueue) Enqueue(job domain.Notification) error {

	q.mu.Lock()
	defer q.mu.Unlock()
	prior, existed := q.jobs[job.ID]
	if !existed && len(q.jobs) >= 10000 {
		for id, old := range q.jobs {
			if old.Status == "DELIVERED" || old.Status == "FAILED" {
				delete(q.jobs, id)
				break
			}
		}
		if len(q.jobs) >= 10000 {
			return errors.New("notification history is full")
		}
	}
	q.jobs[job.ID] = job
	select {
	case q.pending <- job.ID:
		return nil
	default:
		if existed {
			q.jobs[job.ID] = prior
		} else {
			delete(q.jobs, job.ID)
		}
		return errors.New("notification queue is full")
	}
}

func (q *MemoryQueue) Next() (string, bool) { id, ok := <-q.pending; return id, ok }
func (q *MemoryQueue) Get(id string) (domain.Notification, bool) {
	q.mu.RLock()
	defer q.mu.RUnlock()
	job, ok := q.jobs[id]
	return job, ok
}
func (q *MemoryQueue) Update(job domain.Notification) {
	q.mu.Lock()
	defer q.mu.Unlock()
	q.jobs[job.ID] = job
}
func (q *MemoryQueue) Depth() int { return len(q.pending) }
