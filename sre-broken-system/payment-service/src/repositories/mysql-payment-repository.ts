import mysql, {type RowDataPacket, type PoolConnection} from "mysql2/promise";
import type {Payment} from "../models/payment.js";
import type {PaymentStore} from "./payment-repository.js";
import {ApiError} from "../models/error.js";
import {faults} from "../config/faults.js";
import {dbActive, dbIdle, dbPending} from "../observability/telemetry.js";

/** Real bounded pool and database uniqueness across Pods; no other service's tables. */
export class MySQLPaymentRepository implements PaymentStore {
  private pool = mysql.createPool({host: process.env.PAYMENT_DB_HOST ?? "mysql", port: Number(process.env.PAYMENT_DB_PORT ?? 3306),
    user: process.env.PAYMENT_DB_USER ?? "payment_app", password: process.env.PAYMENT_DB_PASSWORD,
    database: process.env.PAYMENT_DB_NAME ?? "payment_db", connectionLimit: Number(process.env.PAYMENT_DB_POOL_MAX ?? 3),
    waitForConnections: true, queueLimit: 32, connectTimeout: 500, decimalNumbers: true});
  private active = 0; private total = 0; private pending = 0;
  constructor() { this.pool.on("connection", c => { this.total++; this.report(); c.once("end", () => { this.total=Math.max(0,this.total-1); this.report(); }); }); }
  private report() { dbActive.set(this.active); dbIdle.set(Math.max(0, this.total - this.active)); dbPending.set(this.pending); }
  private async connection(): Promise<PoolConnection> {
    this.pending++; this.report(); let expired = false, timer: NodeJS.Timeout | undefined;
    const acquisition = this.pool.getConnection().then(c => { if (expired) { c.release(); throw new ApiError("PAYMENT_DB_POOL_TIMEOUT", "payment database pool timeout", 504); } return c; });
    try {
      const c = await Promise.race([acquisition, new Promise<never>((_, reject) => {
        timer = setTimeout(() => { expired = true; reject(new ApiError("PAYMENT_DB_POOL_TIMEOUT", "payment database pool timeout", 504)); }, 1000);
      })]); this.active++; return c;
    } finally { clearTimeout(timer); this.pending--; this.report(); }
  }
  private async using<T>(operation: (c: PoolConnection) => Promise<T>): Promise<T> {
    const c = await this.connection();
    try {
      if (["slow_sql", "connection_pool_exhaustion"].includes(faults.get()))
        await c.query({sql: "SELECT SLEEP(?) AS fault_delay", timeout: 11000}, [faults.parameters.delay_ms / 1000]);
      return await operation(c);
    } finally { c.release(); this.active--; this.report(); }
  }
  private convert(row: RowDataPacket): Payment {
    return {id: row.id, orderId: Number(row.order_id), amount: Number(row.amount), status: row.status,
      idempotencyKey: row.idempotency_key, createdAt: new Date(row.created_at).toISOString()};
  }
  async save(p: Payment): Promise<Payment> {
    return this.using(async c => {
      await c.execute({sql: "INSERT INTO payments(id,order_id,amount,status,idempotency_key,created_at) VALUES(?,?,?,?,?,?) ON DUPLICATE KEY UPDATE id=id", timeout: 1000},
        [p.id, p.orderId, p.amount, p.status, p.idempotencyKey, new Date(p.createdAt)]);
      const [rows] = await c.execute<RowDataPacket[]>({sql: "SELECT * FROM payments WHERE idempotency_key=? OR order_id=?", timeout: 1000}, [p.idempotencyKey, p.orderId]);
      const existing = this.convert(rows[0]);
      if (existing.orderId !== p.orderId || existing.amount !== p.amount || existing.idempotencyKey !== p.idempotencyKey)
        throw new ApiError("IDEMPOTENCY_CONFLICT", "payment key reused for a different request", 409);
      return existing;
    });
  }
  async findById(id: string): Promise<Payment | undefined> { return this.using(async c => {
    const [rows] = await c.execute<RowDataPacket[]>({sql: "SELECT * FROM payments WHERE id=?", timeout: 1000}, [id]);
    return rows[0] ? this.convert(rows[0]) : undefined;
  }); }
  async update(p: Payment): Promise<Payment> { return this.using(async c => {
    await c.execute({sql: "UPDATE payments SET status=? WHERE id=? AND status IN ('AUTHORIZED','CAPTURED')", timeout: 1000}, [p.status, p.id]);
    const [rows] = await c.execute<RowDataPacket[]>({sql: "SELECT * FROM payments WHERE id=?", timeout: 1000}, [p.id]);
    if (!rows[0]) throw new ApiError("PAYMENT_NOT_FOUND", "payment not found", 404);
    const current = this.convert(rows[0]);
    if (p.status === "CAPTURED" && current.status !== "CAPTURED") throw new ApiError("PAYMENT_STATE_CONFLICT", "payment cannot be confirmed", 409);
    return current;
  }); }
  async ready(): Promise<void> { await this.using(async c => { await c.query({sql: "SELECT 1 FROM payments LIMIT 1", timeout: 500}); }); }
}
