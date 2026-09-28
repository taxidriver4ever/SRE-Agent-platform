import type {Payment} from "../models/payment.js";
import {ApiError} from "../models/error.js";
export interface PaymentStore {
  save(payment: Payment): Payment | Promise<Payment>;
  findById(id: string): Payment | undefined | Promise<Payment | undefined>;
  update(payment: Payment): Payment | Promise<Payment>;
}
/** Unit-test adapter; HTTP processes use MySQL. */
export class PaymentRepository implements PaymentStore {
  private byId = new Map<string, Payment>(); private idByKey = new Map<string, string>();
  save(p: Payment): Payment {
    const existing = this.findByIdempotencyKey(p.idempotencyKey);
    if (existing) {
      if (existing.orderId !== p.orderId || existing.amount !== p.amount) throw new ApiError("IDEMPOTENCY_CONFLICT", "payment key conflict", 409);
      return existing;
    }
    this.byId.set(p.id, p); this.idByKey.set(p.idempotencyKey, p.id); return p;
  }
  findById(id: string): Payment | undefined { return this.byId.get(id); }
  findByIdempotencyKey(key: string): Payment | undefined { return this.byId.get(this.idByKey.get(key) ?? ""); }
  update(p: Payment): Payment { if (!this.byId.has(p.id)) throw new ApiError("PAYMENT_NOT_FOUND", "payment not found", 404); this.byId.set(p.id, p); return p; }
}
