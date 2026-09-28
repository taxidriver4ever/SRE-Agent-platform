import {request as httpRequest} from "node:http";
import {request as httpsRequest} from "node:https";
import {requestContext} from "../observability/request-context.js";
import {context, propagation} from "@opentelemetry/api";
/** Independent connection and total deadlines; notification remains secondary. */
export class NotificationClient {
 constructor(private readonly baseUrl:string){}
 async sendPaymentEvent(paymentId:string,orderId:number,_traceparent?:string):Promise<void>{
  const headers:Record<string,string>={"content-type":"application/json","x-request-id":requestContext.getStore()?.requestId??""};propagation.inject(context.active(),headers);
  const url=new URL(`${this.baseUrl}/notifications`);
  await new Promise<void>((resolve,reject)=>{
   let connectTimer:NodeJS.Timeout|undefined;
   const request=(url.protocol==="https:"?httpsRequest:httpRequest)(url,{method:"POST",headers},response=>{
    response.resume();response.once("end",()=>{if((response.statusCode??500)>=400)reject(new Error("notification rejected"));else resolve();});response.once("error",reject);
   });
   const totalTimer=setTimeout(()=>request.destroy(new Error("notification request timeout")),500);
   request.once("socket",socket=>{if(socket.connecting){connectTimer=setTimeout(()=>request.destroy(new Error("notification connect timeout")),200);socket.once("connect",()=>clearTimeout(connectTimer));}});
   request.once("error",reject);request.once("close",()=>{clearTimeout(totalTimer);clearTimeout(connectTimer);});
   request.end(JSON.stringify({type:"PAYMENT_CONFIRMED",payment_id:paymentId,order_id:orderId}));
  });
 }
}
