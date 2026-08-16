import assert from"node:assert/strict";import test from"node:test";import{PaymentRepository}from"../src/repositories/payment-repository.js";

/** Repository must resolve idempotency keys to the original payment. */
test("payment repository preserves idempotency",()=>{const repository=new PaymentRepository();const payment={id:"p-1",orderId:1,amount:19.9,status:"AUTHORIZED" as const,idempotencyKey:"order-1",createdAt:new Date(0).toISOString()};repository.save(payment);assert.equal(repository.findByIdempotencyKey("order-1")?.id,"p-1");});
