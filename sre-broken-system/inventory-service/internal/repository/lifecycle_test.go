package repository

import (
	"local/sre-lab/inventory-service/internal/domain"
	"path/filepath"
	"testing"
)

func TestLifecycleAndPersistence(t *testing.T) {
	r := NewMemoryRepository()
	file := filepath.Join(t.TempDir(), "stock.json")
	if e := r.Open(file); e != nil {
		t.Fatal(e)
	}
	a := domain.Reservation{ID: "order-1-SKU-1", SKU: "SKU-1", Quantity: 2}
	s, e := r.Reserve(a)
	if e != nil {
		t.Fatal(e)
	}
	s2, e := r.Reserve(a)
	if e != nil || s2 != s {
		t.Fatal("not idempotent")
	}
	b := a
	b.SKU = "SKU-2"
	if _, e = r.Reserve(b); e == nil {
		t.Fatal("key reused for a different sku")
	}
	if _, e = r.Commit(a.ID); e != nil {
		t.Fatal(e)
	}
	if _, e = r.Commit(a.ID); e != nil {
		t.Fatal(e)
	}
	if _, e = r.Release(a.ID); e == nil {
		t.Fatal("released committed stock")
	}
	restored := NewMemoryRepository()
	if e = restored.Open(file); e != nil {
		t.Fatal(e)
	}
	final, e := restored.Commit(a.ID)
	if e != nil || final.Reserved != 0 || final.Available != 99 {
		t.Fatal(final, e)
	}
	a.ID = "order-2-SKU-1"
	if _, e = r.Reserve(a); e != nil {
		t.Fatal(e)
	}
	one, e := r.Release(a.ID)
	if e != nil {
		t.Fatal(e)
	}
	two, e := r.Release(a.ID)
	if e != nil || one != two {
		t.Fatal("release not idempotent")
	}
}
