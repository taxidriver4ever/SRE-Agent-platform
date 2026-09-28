import test from "node:test";
import assert from "node:assert/strict";
import {PaymentRepository} from "../src/repositories/payment-repository.js";
import {PaymentService} from "../src/services/payment-service.js";
import {NotificationClient} from "../src/clients/notification-client.js";
import {FaultState} from "../src/config/faults.js";
test("payment lifecycle stays idempotent and notification is secondary",async()=>{
 const n=new NotificationClient("http://unused"); n.sendPaymentEvent=async()=>{throw new Error("provider offline")};
 const s=new PaymentService(new PaymentRepository(),n);
 const input={orderId:7,amount:12.25,idempotencyKey:"order-7"};const p=await s.authorize(input);
 assert.equal((await s.authorize(input)).id,p.id);
 await assert.rejects(s.authorize({...input,amount:13}),/conflict/);
 assert.equal((await s.confirm(p.id)).status,"CAPTURED");assert.equal((await s.confirm(p.id)).status,"CAPTURED");
 assert.equal((await s.refund(p.id)).status,"REFUNDED");await assert.rejects(s.confirm(p.id),/confirmed/);
 await assert.rejects(s.authorize({...input,amount:1.234}),/valid/);
});
test("fault input is bounded and expires",async()=>{
 const f=new FaultState();assert.equal(f.set("slow_sql",0),false);assert.equal(f.set("slow_sql",1,{delay_ms:10001}),false);
 assert.equal(f.set("slow_sql",1),true);await new Promise(resolve=>setTimeout(resolve,1050));assert.equal(f.get(),"normal");
});
