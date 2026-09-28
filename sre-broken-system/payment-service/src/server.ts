import {requestContext} from "./observability/request-context.js";
import {randomUUID} from "node:crypto";
import express from "express";
import {context, trace} from "@opentelemetry/api";
import {config} from "./config/config.js";
import {activeRequests,latency,log,metricsContentType,metricsText,requests} from "./observability/telemetry.js";
import {paymentRouter,paymentService} from "./routes/payment-routes.js";
import {ApiError} from "./models/error.js";
export const app=express();
app.use((req,res,next)=>{
 const started=process.hrtime.bigint(), sc=trace.getSpan(context.active())?.spanContext();
 req.requestId=/^[a-zA-Z0-9._-]{1,128}$/.test(req.header("x-request-id")??"")?req.header("x-request-id")!:randomUUID();
 req.traceId=sc?.traceId??"";
 res.setHeader("X-Request-ID",req.requestId);res.setHeader("X-Service-Version",config.version);res.setHeader("X-Pod-Name",config.podName);
 activeRequests.inc();
 res.once("finish",()=>{activeRequests.dec();const seconds=Number(process.hrtime.bigint()-started)/1e9;const path=req.route?.path??"unmatched";
 requests.labels("payment-service",config.version,config.podName,path,String(res.statusCode)).inc();latency.labels("payment-service",config.version,config.podName,path).observe(seconds);
 log(res.statusCode>=500?"ERROR":"INFO","http request",req.traceId,{span_id:sc?.spanId??"",request_id:req.requestId,endpoint:path,latency_ms:seconds*1000,status:res.statusCode});});requestContext.run({requestId:req.requestId},next);
});
app.use(express.json({limit:"64kb"}));
app.get("/health",(_req,res)=>res.json({status:"ok",service:"payment-service",fault_mode:paymentService.getFault()}));
app.get("/metrics",async(_req,res)=>res.type(metricsContentType).send(await metricsText()));
app.use(paymentRouter);
app.use((error:Error,req:express.Request,res:express.Response,_next:express.NextFunction)=>{
 const known=error instanceof ApiError;const invalid=error instanceof SyntaxError;
 log("ERROR","payment request failed",req.traceId,{request_id:req.requestId,error_type:error.name});
 res.status(known?error.status:invalid?400:503).json({code:known?error.code:invalid?"INVALID_JSON":"PAYMENT_UNAVAILABLE",message:known?error.message:invalid?"invalid JSON":"payment temporarily unavailable",request_id:req.requestId});
});
app.listen(config.port,"0.0.0.0",()=>log("INFO","payment-service started","",{port:config.port}));
