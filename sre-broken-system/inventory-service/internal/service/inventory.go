// Package service implements inventory use cases and controlled failure mechanisms.
package service

import("context";"errors";"sync";"time";"local/sre-lab/inventory-service/internal/client";"local/sre-lab/inventory-service/internal/domain";"local/sre-lab/inventory-service/internal/repository")

// InventoryService coordinates persistence, recommendation warming and fault state.
type InventoryService struct{repository *repository.MemoryRepository;recommendations *client.RecommendationClient;mu sync.RWMutex;faultMode string}
func New(repo *repository.MemoryRepository,recommendations *client.RecommendationClient)*InventoryService{return &InventoryService{repository:repo,recommendations:recommendations,faultMode:"normal"}}

// Stock reads inventory and calls recommendation-service to form a real downstream trace edge.
func(s *InventoryService)Stock(ctx context.Context,sku,traceparent string)(domain.Stock,error){if s.FaultMode()=="dependency_timeout"{time.Sleep(4*time.Second)};stock,ok:=s.repository.Get(sku);if !ok{return domain.Stock{},errors.New("sku not found")};_=s.recommendations.WarmProduct(ctx,stock.ProductID,traceparent);return stock,nil}
func(s *InventoryService)Reserve(reservation domain.Reservation)(domain.Stock,error){
	// Dependency timeout affects both stock reads and reservation writes so the real order creation chain can fail.
	if s.FaultMode()=="dependency_timeout"{time.Sleep(4*time.Second)}
	reservation.CreatedAt=time.Now().UTC();return s.repository.Reserve(reservation)
}
func(s *InventoryService)Release(id string)(domain.Stock,error){return s.repository.Release(id)}

// SetFault only accepts modes implemented by this service.
func(s *InventoryService)SetFault(mode string)bool{if mode!="normal"&&mode!="dependency_timeout"&&mode!="goroutine_leak"{return false};s.mu.Lock();defer s.mu.Unlock();s.faultMode=mode;return true}
func(s *InventoryService)FaultMode()string{s.mu.RLock();defer s.mu.RUnlock();return s.faultMode}
