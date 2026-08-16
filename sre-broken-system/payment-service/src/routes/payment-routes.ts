import{Router}from"express";import{config}from"../config/config.js";import{PaymentRepository}from"../repositories/payment-repository.js";import{NotificationClient}from"../clients/notification-client.js";import{PaymentService}from"../services/payment-service.js";

/** Shared service instance keeps idempotency and per-Pod fault state consistent. */
export const paymentService=new PaymentService(new PaymentRepository(),new NotificationClient(config.notificationBaseUrl));export const paymentRouter=Router();
paymentRouter.post("/payments",async(request,response,next)=>{try{const body=request.body as{order_id?:number;amount?:number;idempotency_key?:string};const payment=await paymentService.authorize({orderId:Number(body.order_id),amount:Number(body.amount),idempotencyKey:body.idempotency_key??`order-${body.order_id}`},request.traceId,request.header("traceparent"));response.status(201).json(payment);}catch(error){next(error);}});
paymentRouter.get("/payments/:id",(request,response,next)=>{try{response.json(paymentService.status(request.params.id));}catch(error){next(error);}});paymentRouter.post("/payments/:id/refund",(request,response,next)=>{try{response.json(paymentService.refund(request.params.id));}catch(error){next(error);}});
paymentRouter.all("/debug/fault",(request,response)=>{const mode=request.query.mode;if(typeof mode==="string"&&!paymentService.setFault(mode))return response.status(400).json({error:"unsupported mode"});return response.json({service:"payment-service",version:config.version,pod:config.podName,fault_mode:paymentService.getFault()});});
/** Express declaration extension makes middleware-provided trace identity type-safe. */
declare global{namespace Express{interface Request{traceId:string}}}
