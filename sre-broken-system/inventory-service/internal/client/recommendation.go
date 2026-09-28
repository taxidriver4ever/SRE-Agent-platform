package client

import (
	"context"
	"fmt"
	"io"
	"local/sre-lab/inventory-service/internal/observability"
	"net"
	"net/http"
	"strconv"
	"strings"
	"time"
)

type RecommendationClient struct {
	baseURL string
	http    *http.Client
}

func NewRecommendationClient(url string) *RecommendationClient {
	return &RecommendationClient{strings.TrimRight(url, "/"), &http.Client{Timeout: 500 * time.Millisecond, Transport: &http.Transport{DialContext: (&net.Dialer{Timeout: 200 * time.Millisecond}).DialContext}}}
}
func (c *RecommendationClient) WarmProduct(ctx context.Context, id int64, _ string) error {
	headers, end := observability.ClientSpan(ctx, "inventory-service", "GET recommendation-service/recommendations/products/{id}")
	status := 502
	defer func() { end(status) }()
	r, e := http.NewRequestWithContext(ctx, "GET", c.baseURL+"/recommendations/products/"+strconv.FormatInt(id, 10), nil)
	if e != nil {
		return e
	}
	for k, v := range headers {
		r.Header.Set(k, v)
	}
	response, e := c.http.Do(r)
	if e != nil {
		return e
	}
	defer response.Body.Close()
	status = response.StatusCode
	_, _ = io.Copy(io.Discard, response.Body)
	if status >= 400 {
		return fmt.Errorf("recommendation returned %d", status)
	}
	return nil
}
