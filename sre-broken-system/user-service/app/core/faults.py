"""Thread-safe, per-Pod fault state used by repeatable SRE scenarios."""

from threading import Lock


class FaultState:
    """Protect the fault mode because sync workers and async routes may access it together."""

    _allowed = {"normal", "cpu_saturation", "event_loop_blocking", "database_latency"}

    def __init__(self) -> None:
        self._mode = "normal"
        self._lock = Lock()

    def get(self) -> str:
        """Return a consistent mode snapshot."""
        with self._lock:
            return self._mode

    def set(self, mode: str) -> bool:
        """Apply only modes implemented in code so arbitrary input cannot execute behavior."""
        if mode not in self._allowed:
            return False
        with self._lock:
            self._mode = mode
        return True


faults = FaultState()
