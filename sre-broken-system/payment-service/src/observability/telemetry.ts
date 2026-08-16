import{monitorEventLoopDelay}from"node:perf_hooks";import client from"prom-client";import{config}from"../config/config.js";

/** Every metric includes version and Pod identity for mixed ReplicaSet comparisons. */
const labels={service:"payment-service",version:config.version,pod:config.podName};client.collectDefaultMetrics({prefix:"payment_",labels});
export const requests=new client.Counter({name:"sre_http_requests_total",help:"HTTP requests",labelNames:["service","version","pod","path","status"]});
export const latency=new client.Histogram({name:"sre_http_request_duration_seconds",help:"HTTP latency",labelNames:["service","version","pod","path"],buckets:[.01,.05,.1,.5,1,5]});
export const leakedBytes=new client.Gauge({name:"sre_payment_leaked_bytes",help:"Bytes retained by controlled memory leak",labelNames:["service","version","pod"]});
export const eventLoopDelay=new client.Gauge({name:"sre_nodejs_event_loop_delay_seconds",help:"Mean event-loop delay",labelNames:["service","version","pod"]});
const monitor=monitorEventLoopDelay({resolution:20});monitor.enable();setInterval(()=>eventLoopDelay.labels(labels.service,labels.version,labels.pod).set(monitor.mean/1e9),1000).unref();
/** Structured logs use the common cross-language fields. */
export function log(level:string,message:string,traceId="",fields:Record<string,unknown>={}):void{console.log(JSON.stringify({timestamp:new Date().toISOString(),...labels,level,trace_id:traceId,message,...fields}));}
export async function metricsText():Promise<string>{return client.register.metrics();}export const metricsContentType=client.register.contentType;
