// Package config centralizes environment parsing so handlers do not read process state directly.
package config

import "os"

// Config describes ports, downstream DNS names and immutable runtime identity.
type Config struct { Address, RecommendationBaseURL, OTLPEndpoint, Version, PodName string }

// Load returns Kubernetes-friendly defaults while allowing local overrides.
func Load() Config { return Config{env("HTTP_ADDRESS", ":8081"), env("RECOMMENDATION_BASE_URL", "http://recommendation-service:8085"), os.Getenv("OTEL_EXPORTER_OTLP_ENDPOINT"), env("SERVICE_VERSION", "dev"), env("POD_NAME", "local")} }

// env applies a fallback only when the variable is absent or empty.
func env(name, fallback string) string { if value := os.Getenv(name); value != "" { return value }; return fallback }
