// Package repository owns atomic inventory and durable idempotency records.
package repository

import (
	"encoding/json"
	"errors"
	"local/sre-lab/inventory-service/internal/domain"
	"os"
	"path/filepath"
	"strconv"
	"sync"
	"sync/atomic"
	"time"
)

var LockWaitNanos atomic.Int64

// A single writer is required. Kubernetes uses one replica with a persistent volume.
type MemoryRepository struct {
	mu           sync.RWMutex
	stocks       map[string]domain.Stock
	reservations map[string]domain.Reservation
	file         string
}
type snapshot struct {
	Stocks       map[string]domain.Stock
	Reservations map[string]domain.Reservation
}

func NewMemoryRepository() *MemoryRepository {
	r := &MemoryRepository{stocks: map[string]domain.Stock{}, reservations: map[string]domain.Reservation{}}
	for id := int64(1); id <= 500; id++ {
		sku := "SKU-" + strconv.FormatInt(id, 10)
		r.stocks[sku] = domain.Stock{SKU: sku, ProductID: id, Available: 100 + int(id%50), Version: 1}
	}
	return r
}
func (r *MemoryRepository) Open(file string) error {
	if file == "" {
		return nil
	}
	r.file = file
	data, err := os.ReadFile(file)
	if os.IsNotExist(err) {
		return r.persist()
	}
	if err != nil {
		return err
	}
	var s snapshot
	if err = json.Unmarshal(data, &s); err != nil {
		return err
	}
	if s.Stocks == nil || s.Reservations == nil {
		return errors.New("invalid inventory snapshot")
	}
	r.stocks = s.Stocks
	r.reservations = s.Reservations
	return nil
}
func (r *MemoryRepository) persist() error {
	if r.file == "" {
		return nil
	}
	if err := os.MkdirAll(filepath.Dir(r.file), 0750); err != nil {
		return err
	}
	data, err := json.Marshal(snapshot{r.stocks, r.reservations})
	if err != nil {
		return err
	}
	f, err := os.CreateTemp(filepath.Dir(r.file), "inventory-*.tmp")
	if err != nil {
		return err
	}
	defer os.Remove(f.Name())
	if _, err = f.Write(data); err == nil {
		err = f.Sync()
	}
	closeErr := f.Close()
	if err != nil {
		return err
	}
	if closeErr != nil {
		return closeErr
	}
	return os.Rename(f.Name(), r.file)
}
func (r *MemoryRepository) Get(sku string) (domain.Stock, bool) {
	r.mu.RLock()
	defer r.mu.RUnlock()
	s, ok := r.stocks[sku]
	return s, ok
}
func (r *MemoryRepository) Reserve(v domain.Reservation) (domain.Stock, error) {
	started := time.Now()
	r.mu.Lock()
	LockWaitNanos.Add(time.Since(started).Nanoseconds())
	defer r.mu.Unlock()
	if v.ID == "" || len(v.ID) > 128 || v.Quantity <= 0 {
		return domain.Stock{}, errors.New("invalid reservation")
	}
	if prior, ok := r.reservations[v.ID]; ok {
		if prior.SKU != v.SKU || prior.Quantity != v.Quantity || prior.State == "RELEASED" {
			return domain.Stock{}, errors.New("reservation conflict")
		}
		return r.stocks[v.SKU], nil
	}
	old, ok := r.stocks[v.SKU]
	if !ok {
		return domain.Stock{}, errors.New("sku not found")
	}
	if old.Available < v.Quantity {
		return domain.Stock{}, errors.New("insufficient inventory")
	}
	s := old
	s.Available -= v.Quantity
	s.Reserved += v.Quantity
	s.Version++
	v.State = "RESERVED"
	r.stocks[v.SKU] = s
	r.reservations[v.ID] = v
	if err := r.persist(); err != nil {
		r.stocks[v.SKU] = old
		delete(r.reservations, v.ID)
		return domain.Stock{}, err
	}
	return s, nil
}
func (r *MemoryRepository) finish(id, target string) (domain.Stock, error) {
	r.mu.Lock()
	defer r.mu.Unlock()
	v, ok := r.reservations[id]
	if !ok {
		return domain.Stock{}, errors.New("reservation not found")
	}
	old := r.stocks[v.SKU]
	if v.State == target {
		return old, nil
	}
	if v.State != "RESERVED" {
		return domain.Stock{}, errors.New("reservation already finalized")
	}
	s := old
	s.Reserved -= v.Quantity
	if target == "RELEASED" {
		s.Available += v.Quantity
	}
	s.Version++
	prior := v
	v.State = target
	r.stocks[v.SKU] = s
	r.reservations[id] = v
	if err := r.persist(); err != nil {
		r.stocks[v.SKU] = old
		r.reservations[id] = prior
		return domain.Stock{}, err
	}
	return s, nil
}
func (r *MemoryRepository) Release(id string) (domain.Stock, error) { return r.finish(id, "RELEASED") }
func (r *MemoryRepository) Commit(id string) (domain.Stock, error)  { return r.finish(id, "COMMITTED") }
