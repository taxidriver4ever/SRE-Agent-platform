// Package domain defines notification jobs independently from queue and HTTP implementations.
package domain

import "time"

// Notification is the durable-looking job representation returned by the lab API.
type Notification struct{ID string `json:"id"`;Type string `json:"type"`;OrderID int64 `json:"order_id"`;UserID int64 `json:"user_id"`;PaymentID string `json:"payment_id,omitempty"`;Status string `json:"status"`;Attempts int `json:"attempts"`;CreatedAt time.Time `json:"created_at"`;Traceparent string `json:"-"`}
