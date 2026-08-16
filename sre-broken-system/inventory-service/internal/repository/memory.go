// Package repository provides deterministic in-memory persistence for the inventory boundary.
package repository

import (
	"errors"
	"strconv"
	"sync"
	"local/sre-lab/inventory-service/internal/domain"
)

// MemoryRepository models atomic stock updates and idempotent reservations.
type MemoryRepository struct { mu sync.RWMutex; stocks map[string]domain.Stock; reservations map[string]domain.Reservation }

// NewMemoryRepository seeds enough SKUs for order and recommendation flows.
func NewMemoryRepository() *MemoryRepository {
	stocks := make(map[string]domain.Stock, 500)
	for id := int64(1); id <= 500; id++ { sku := "SKU-"+strconv.FormatInt(id,10); stocks[sku]=domain.Stock{SKU:sku,ProductID:id,Available:100+int(id%50),Version:1} }
	return &MemoryRepository{stocks:stocks,reservations:make(map[string]domain.Reservation)}
}

// Get returns a copy so callers cannot mutate repository state without the lock.
func (r *MemoryRepository) Get(sku string) (domain.Stock,bool) { r.mu.RLock(); defer r.mu.RUnlock(); stock,ok:=r.stocks[sku]; return stock,ok }

// Reserve atomically checks capacity, stores the hold and increments the version.
func (r *MemoryRepository) Reserve(reservation domain.Reservation) (domain.Stock,error) {
	r.mu.Lock(); defer r.mu.Unlock()
	if existing,ok:=r.reservations[reservation.ID]; ok { return r.stocks[existing.SKU],nil }
	stock,ok:=r.stocks[reservation.SKU]; if !ok { return domain.Stock{},errors.New("sku not found") }
	if reservation.Quantity<=0 || stock.Available<reservation.Quantity { return domain.Stock{},errors.New("insufficient inventory") }
	stock.Available-=reservation.Quantity; stock.Reserved+=reservation.Quantity; stock.Version++; r.stocks[reservation.SKU]=stock; r.reservations[reservation.ID]=reservation
	return stock,nil
}

// Release reverses an existing reservation and is safe to retry.
func (r *MemoryRepository) Release(id string) (domain.Stock,error) {
	r.mu.Lock(); defer r.mu.Unlock(); reservation,ok:=r.reservations[id]; if !ok { return domain.Stock{},errors.New("reservation not found") }
	stock:=r.stocks[reservation.SKU]; stock.Available+=reservation.Quantity; stock.Reserved-=reservation.Quantity; stock.Version++; r.stocks[reservation.SKU]=stock; delete(r.reservations,id); return stock,nil
}
