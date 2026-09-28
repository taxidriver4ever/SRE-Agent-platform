import {randomUUID} from "node:crypto";
import type {CreatePayment, Payment} from "../models/payment.js";
import {NotificationClient} from "../clients/notification-client.js";
import type {PaymentStore} from "../repositories/payment-repository.js";
import {config} from "../config/config.js";
import {faults} from "../config/faults.js";
import {ApiError} from "../models/error.js";
import {leakedBytes, log} from "../observability/telemetry.js";

export class PaymentService {
  private leakedBuffers: Buffer[] = [];
  constructor(private readonly repository: PaymentStore, private readonly notifications: NotificationClient) {
    setInterval(() => this.leakTick(), 1000).unref();
  }
  async authorize(input: CreatePayment, traceId = "", traceparent?: string): Promise<Payment> {
    if (!Number.isSafeInteger(input.orderId) || input.orderId <= 0 || !Number.isFinite(input.amount) || input.amount <= 0 || input.amount > 1e8 || Math.abs(input.amount * 100 - Math.round(input.amount * 100)) > 1e-6 || !input.idempotencyKey || input.idempotencyKey.length > 128)
      throw new ApiError("INVALID_PAYMENT", "valid order, amount and idempotency key required", 400);
    await this.inject();
    return this.repository.save({id: randomUUID(), orderId: input.orderId, amount: input.amount,
      status: "AUTHORIZED", idempotencyKey: input.idempotencyKey, createdAt: new Date().toISOString()});
  }
  async status(id: string): Promise<Payment> { const p = await this.repository.findById(id); if (!p) throw new ApiError("PAYMENT_NOT_FOUND", "payment not found", 404); return p; }
  async confirm(id: string, traceId = "", traceparent?: string): Promise<Payment> {
    const p = await this.status(id);
    if (p.status === "CAPTURED") return p;
    if (p.status !== "AUTHORIZED") throw new ApiError("PAYMENT_STATE_CONFLICT", "payment cannot be confirmed", 409);
    const result = await this.repository.update({...p, status: "CAPTURED"});
    try { await this.notifications.sendPaymentEvent(result.id, result.orderId, traceparent); }
    catch (error) { log("WARN", "payment notification failed", traceId, {event: "notification_partial_failure", payment_id: id}); }
    return result;
  }
  async refund(id: string): Promise<Payment> { const p = await this.status(id); return p.status === "REFUNDED" ? p : this.repository.update({...p, status: "REFUNDED"}); }
  setFault(mode: string): boolean { return faults.set(mode); }
  getFault(): string { return faults.get(); }
  private async inject() {
    const mode = faults.get();
    if (mode === "random_500" && Math.random() < faults.parameters.error_rate) throw new ApiError("PAYMENT_FAILURE", "simulated payment provider error", 500);
    if (["high_latency", "dependency_timeout", "promise_backlog"].includes(mode)) await new Promise(resolve => setTimeout(resolve, faults.parameters.delay_ms));
    if (["cpu_high", "event_loop_blocking"].includes(mode)) {
      const until = Date.now() + Math.min(2000, faults.parameters.delay_ms); let n = 1;
      while (Date.now() < until) n = Math.sqrt(n + Math.random());
    }
  }
  private leakTick() {
    if (faults.get() === "memory_leak" && this.leakedBuffers.length < 10) this.leakedBuffers.push(Buffer.alloc(6 * 1024 * 1024, 1)); else if (faults.get() !== "memory_leak") this.leakedBuffers = [];
    leakedBytes.labels("payment-service", config.version, config.podName).set(this.leakedBuffers.length * 6 * 1024 * 1024);
  }
}
