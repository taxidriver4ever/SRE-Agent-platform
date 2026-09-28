"""Bounded per-process faults. One worker per Pod gives deterministic controls."""
from threading import Lock
from time import monotonic

class FaultState:
    def __init__(self, allowed=None):
        self.allowed = set(allowed or {"normal", "cpu_saturation", "event_loop_blocking", "database_latency", "high_latency", "random_error"})
        self._mode = "normal"
        self._expires = 0.0
        self._lock = Lock()
        self.parameters = {"delay_ms": 3000, "error_rate": 1.0}
    def get(self):
        with self._lock:
            if monotonic() >= self._expires:
                self._mode = "normal"
            return self._mode
    def set(self, mode, duration_seconds=120, parameters=None):
        parameters = parameters or {}
        delay = parameters.get("delay_ms", 3000)
        rate = parameters.get("error_rate", 1.0)
        if mode not in self.allowed or not isinstance(duration_seconds, (int,float)) or not 1 <= duration_seconds <= 300 or not isinstance(delay,(int,float)) or not 0 <= delay <= 10000 or not isinstance(rate,(int,float)) or not 0 <= rate <= 1:
            return False
        with self._lock:
            self._mode = mode
            self._expires = monotonic() + duration_seconds
            self.parameters = {"delay_ms": delay, "error_rate": rate}
        return True
    def snapshot(self):
        return {"fault_mode":self.get(),"remaining_seconds":max(0, round(self._expires-monotonic(),2)) if self.get()!="normal" else 0,"parameters":self.parameters}
faults = FaultState()
