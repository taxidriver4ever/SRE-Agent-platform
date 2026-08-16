"""Pure CPU workload used to distinguish application saturation from database latency."""


def count_primes(limit: int) -> int:
    """Deliberately inefficient trial division creates genuine CPU saturation for SRE-004."""
    count = 0
    for candidate in range(2, limit):
        divisor = 2
        is_prime = True
        while divisor * divisor <= candidate:
            if candidate % divisor == 0:
                is_prime = False
                break
            divisor += 1
        if is_prime:
            count += 1
    return count
