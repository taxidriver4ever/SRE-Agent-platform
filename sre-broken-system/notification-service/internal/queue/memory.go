// Package queue implements a bounded in-process work queue without requiring Kafka in version one.
package queue

import("errors";"sync";"local/sre-lab/notification-service/internal/domain")

// MemoryQueue holds pending jobs and status snapshots under one lock.
type MemoryQueue struct{mu sync.RWMutex;pending chan string;jobs map[string]domain.Notification}
func New(capacity int)*MemoryQueue{return &MemoryQueue{pending:make(chan string,capacity),jobs:make(map[string]domain.Notification)}}

// Enqueue stores the job first and rejects when the bounded queue is full.
func(q *MemoryQueue)Enqueue(job domain.Notification)error{q.mu.Lock();q.jobs[job.ID]=job;q.mu.Unlock();select{case q.pending<-job.ID:return nil;default:q.mu.Lock();delete(q.jobs,job.ID);q.mu.Unlock();return errors.New("notification queue is full")}}
func(q *MemoryQueue)Next()(string,bool){id,ok:=<-q.pending;return id,ok}
func(q *MemoryQueue)Get(id string)(domain.Notification,bool){q.mu.RLock();defer q.mu.RUnlock();job,ok:=q.jobs[id];return job,ok}
func(q *MemoryQueue)Update(job domain.Notification){q.mu.Lock();defer q.mu.Unlock();q.jobs[job.ID]=job}
func(q *MemoryQueue)Depth()int{return len(q.pending)}
