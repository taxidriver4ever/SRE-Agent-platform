// Package service implements inventory use cases and controlled failure mechanisms.
package service

import (
	"context"
	"errors"
	"local/sre-lab/inventory-service/internal/client"
	"local/sre-lab/inventory-service/internal/domain"
	"local/sre-lab/inventory-service/internal/faults"
	"local/sre-lab/inventory-service/internal/repository"
	"math"
	"sync"
	"time"
)

// InventoryService coordinates persistence, recommendation warming and fault state.
type InventoryService struct {
	repository      *repository.MemoryRepository
	recommendations *client.RecommendationClient
	mu              sync.RWMutex
	faultMode       string
	expires         time.Time
}

func New(repo *repository.MemoryRepository, recommendations *client.RecommendationClient) *InventoryService {
	return &InventoryService{repository: repo, recommendations: recommendations, faultMode: "normal"}
}

// Stock reads inventory and calls recommendation-service to form a real downstream trace edge.
func (s *InventoryService) Stock(ctx context.Context, sku, traceparent string) (domain.Stock, error) {
	if err := s.inject(); err != nil {
		return domain.Stock{}, err
	}
	stock, ok := s.repository.Get(sku)
	if !ok {
		return domain.Stock{}, errors.New("sku not found")
	}
	_ = s.recommendations.WarmProduct(ctx, stock.ProductID, traceparent)
	return stock, nil
}
func (s *InventoryService) Reserve(reservation domain.Reservation) (domain.Stock, error) {
	// Dependency timeout affects both stock reads and reservation writes so the real order creation chain can fail.
	if err := s.inject(); err != nil {
		return domain.Stock{}, err
	}
	reservation.CreatedAt = time.Now().UTC()
	return s.repository.Reserve(reservation)
}
func (s *InventoryService) Commit(id string) (domain.Stock, error)  { return s.repository.Commit(id) }
func (s *InventoryService) Release(id string) (domain.Stock, error) { return s.repository.Release(id) }

// SetFault only accepts modes implemented by this service.
func init() {
	faults.Allow("dependency_timeout", "goroutine_leak", "lock_contention", "inventory_timeout", "high_cpu", "random_error")
}
func (s *InventoryService) SetFault(mode string) bool { return faults.Set(mode, 120, 3000, 1) }
func (s *InventoryService) FaultMode() string         { return faults.Get() }

var contention sync.Mutex
var ErrInjectedFailure = errors.New("simulated inventory failure")

func (s *InventoryService) inject() error {
	mode := faults.Get()
	delay := faults.Delay()
	if mode == "random_error" && faults.Fail() {
		return ErrInjectedFailure
	}
	if mode == "lock_contention" {
		start := time.Now()
		contention.Lock()
		repository.LockWaitNanos.Add(time.Since(start).Nanoseconds())
		time.Sleep(delay)
		contention.Unlock()
	}
	if mode == "inventory_timeout" || mode == "dependency_timeout" {
		time.Sleep(delay)
	}
	if mode == "high_cpu" {
		until := time.Now().Add(delay)
		n := 1.0
		for time.Now().Before(until) && faults.Get() == mode {
			n = math.Sqrt(n + 3)
		}
		_ = n
	}
	if mode == "goroutine_leak" {
		for i := 0; i < 8; i++ {
			go func() {
				for faults.Get() == "goroutine_leak" {
					time.Sleep(100 * time.Millisecond)
				}
			}()
		}
	}
	return nil
}
