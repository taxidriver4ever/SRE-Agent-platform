package repository

import("testing";"local/sre-lab/inventory-service/internal/domain")

// TestReservationIsIdempotent verifies retries cannot reserve the same stock twice.
func TestReservationIsIdempotent(t *testing.T){repo:=NewMemoryRepository();request:=domain.Reservation{ID:"order-1",SKU:"SKU-1",Quantity:2};first,err:=repo.Reserve(request);if err!=nil{t.Fatal(err)};second,err:=repo.Reserve(request);if err!=nil{t.Fatal(err)};if first.Available!=second.Available{t.Fatalf("retry changed stock: %d != %d",first.Available,second.Available)}}
