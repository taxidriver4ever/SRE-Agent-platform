"""Product and recommendation domain models."""

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class Product:
    """Synthetic catalog entry used by ranking and scan scenarios."""

    id: int
    category: str
    popularity: float
    price: float


@dataclass(frozen=True, slots=True)
class Recommendation:
    """Scored result returned after filtering and ranking."""

    product_id: int
    score: float
    reason: str
