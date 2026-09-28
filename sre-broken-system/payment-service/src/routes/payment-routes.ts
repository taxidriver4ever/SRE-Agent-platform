import {Router} from "express";
import {config} from "../config/config.js";
import {MySQLPaymentRepository} from "../repositories/mysql-payment-repository.js";
import {NotificationClient} from "../clients/notification-client.js";
import {PaymentService} from "../services/payment-service.js";
import {faults} from "../config/faults.js";
import {ApiError} from "../models/error.js";
export const repository = new MySQLPaymentRepository();
export const paymentService = new PaymentService(repository, new NotificationClient(config.notificationBaseUrl));
export const paymentRouter = Router();
paymentRouter.post("/payments", async (req,res) => {
 const b=req.body??{};
 res.status(201).json(await paymentService.authorize({orderId:Number(b.order_id),amount:Number(b.amount),idempotencyKey:req.header("x-idempotency-key")??b.idempotency_key??`order-${b.order_id}`},req.traceId,req.header("traceparent")));
});
paymentRouter.get("/payments/:id",async(req,res)=>res.json(await paymentService.status(req.params.id)));
paymentRouter.post("/payments/:id/confirm",async(req,res)=>res.json(await paymentService.confirm(req.params.id,req.traceId,req.header("traceparent"))));
paymentRouter.post("/payments/:id/refund",async(req,res)=>res.json(await paymentService.refund(req.params.id)));
paymentRouter.get("/ready",async(_req,res)=>{await repository.ready();res.json({status:"ok"});});
paymentRouter.get("/internal/faults",(_req,res)=>res.json(faults.snapshot()));
paymentRouter.post("/internal/faults",(req,res)=>{
 const b=req.body??{}; if(!faults.set(b.fault,b.duration_seconds??120,b.parameters??{})) throw new ApiError("INVALID_FAULT","invalid fault or TTL/parameters",400);
 res.json(faults.snapshot());
});
paymentRouter.delete("/internal/faults/:fault",(req,res)=>{if(req.params.fault===faults.get()) faults.set("normal");res.json(faults.snapshot());});
paymentRouter.all("/debug/fault",(req,res)=>{const mode=req.query.mode;if(typeof mode==="string"&&!paymentService.setFault(mode))throw new ApiError("INVALID_FAULT","unsupported mode",400);res.json({service:"payment-service",version:config.version,...faults.snapshot()});});
declare global {namespace Express {interface Request {traceId:string; requestId:string;}}}
