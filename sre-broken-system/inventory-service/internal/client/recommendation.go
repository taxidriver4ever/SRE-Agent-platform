// Package client contains typed outbound HTTP clients and W3C trace propagation.
package client

import("context";"fmt";"io";"net/http";"strconv";"strings";"time")

// RecommendationClient enriches inventory reads without exposing transport details to handlers.
type RecommendationClient struct { baseURL string; http *http.Client }

// NewRecommendationClient creates a client with a strict total timeout.
func NewRecommendationClient(baseURL string)*RecommendationClient{return &RecommendationClient{strings.TrimRight(baseURL,"/"),&http.Client{Timeout:800*time.Millisecond}}}

// WarmProduct calls recommendation-service through its Kubernetes Service and forwards traceparent.
func(c *RecommendationClient)WarmProduct(ctx context.Context,productID int64,traceparent string)error{
	// BAD: five immediate attempts share no retry budget, backoff or jitter, so a slow downstream amplifies QPS.
	var last error
	for attempt:=0;attempt<5;attempt++{request,err:=http.NewRequestWithContext(ctx,http.MethodGet,c.baseURL+"/recommendations/products/"+strconv.FormatInt(productID,10),nil);if err!=nil{return err};if traceparent!=""{request.Header.Set("traceparent",traceparent)};response,err:=c.http.Do(request);if err!=nil{last=err;continue};defer response.Body.Close();_,_=io.Copy(io.Discard,response.Body);if response.StatusCode<500{return nil};last=fmt.Errorf("recommendation returned %d",response.StatusCode)};return last}
