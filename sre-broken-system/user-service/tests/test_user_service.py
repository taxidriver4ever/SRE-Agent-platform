"""Unit tests for deterministic business and CPU functions."""

from app.core.cpu_work import count_primes


def test_count_primes_uses_real_computation() -> None:
    """The CPU scenario function should return mathematically correct results."""
    assert count_primes(20) == 8
