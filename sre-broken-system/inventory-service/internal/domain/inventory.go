// Package domain contains inventory entities independent of HTTP and storage details.
package domain

import "time"

// Stock represents sellable and reserved quantities for one SKU.
type Stock struct { SKU string `json:"sku"`; ProductID int64 `json:"product_id"`; Available int `json:"available"`; Reserved int `json:"reserved"`; Version int64 `json:"version"` }

// Reservation records an idempotent stock hold made by order-service.
type Reservation struct { ID string `json:"reservation_id"`; SKU string `json:"sku"`; Quantity int `json:"quantity"`; CreatedAt time.Time `json:"created_at"` }
