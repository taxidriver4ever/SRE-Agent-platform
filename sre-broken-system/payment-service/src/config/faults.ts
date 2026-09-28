export class FaultState {
  private mode = "normal";
  private expires = 0;
  parameters = {delay_ms: 3000, error_rate: 1};
  readonly allowed = ["normal", "slow_sql", "connection_pool_exhaustion", "random_500", "high_latency",
    "dependency_timeout", "cpu_high", "memory_leak", "promise_backlog", "event_loop_blocking"];
  set(mode: string, duration = 120, parameters: Record<string, number> = {}): boolean {
    if (!this.allowed.includes(mode) || !Number.isFinite(duration) || duration < 1 || duration > 300) return false;
    const delay = parameters.delay_ms ?? 3000, rate = parameters.error_rate ?? 1;
    if (!Number.isFinite(delay) || delay < 0 || delay > 10000 || !Number.isFinite(rate) || rate < 0 || rate > 1) return false;
    this.mode = mode; this.expires = Date.now() + duration * 1000;
    this.parameters = {delay_ms: delay, error_rate: rate}; return true;
  }
  get(): string { if (Date.now() >= this.expires) this.mode = "normal"; return this.mode; }
  snapshot() { return {fault_mode: this.get(), expires_at: this.mode === "normal" ? null : new Date(this.expires).toISOString(), parameters: this.parameters}; }
}
export const faults = new FaultState();
