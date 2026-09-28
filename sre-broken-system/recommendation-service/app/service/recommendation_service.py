"""Recommendation ranking, cache policy and genuine algorithmic failure modes."""

from threading import Lock
from app.core.faults import faults
faults.allowed = {"normal", "cache_miss", "large_scan", "quadratic_ranking", "cpu_high", "slow_algorithm", "user_dependency_timeout"}
from app.model.product import Recommendation
from app.repository.catalog_repository import CatalogRepository


class RecommendationService:
    """Calculate product/user recommendations and maintain a bounded result cache."""

    _allowed_modes = {"normal", "cache_miss", "large_scan", "quadratic_ranking"}

    def __init__(self, repository: CatalogRepository) -> None:
        self.repository = repository
        self._cache: dict[str, list[Recommendation]] = {}
        self._mode = "normal"
        self._lock = Lock()

    def for_product(self, product_id: int, limit: int = 10) -> list[Recommendation]:
        product = self.repository.get(product_id)
        if product is None:
            return []
        return self._rank(f"product:{product_id}", product.category, limit)

    def for_user(self, user_id: int, limit: int = 10) -> list[Recommendation]:
        """Use a deterministic preference so repeated requests exercise the cache."""
        category = f"category-{user_id % 40}"
        return self._rank(f"user:{user_id}", category, limit)

    def _rank(self, key: str, category: str, limit: int) -> list[Recommendation]:
        bounded_limit = min(max(limit, 1), 50)
        if self.mode() == "normal" and key in self._cache:
            return self._cache[key][:bounded_limit]
        candidates = self.repository.full_scan() if self.mode() in {"large_scan", "quadratic_ranking", "cpu_high", "slow_algorithm"} else self.repository.category(category)
        if self.mode() in {"quadratic_ranking", "cpu_high", "slow_algorithm"}:
            candidates = candidates[:2000]
            # Pairwise comparison is intentionally O(n²), creating real CPU cost rather than sleep.
            scores = [(candidate, sum(1 for other in candidates if candidate.popularity >= other.popularity))
                      for candidate in candidates]
            ranked = sorted(scores, key=lambda item: item[1], reverse=True)
            result = [Recommendation(product.id, float(score), "pairwise popularity rank")
                      for product, score in ranked[:bounded_limit]]
        else:
            ranked = sorted(candidates, key=lambda item: item.popularity / max(item.price, 1), reverse=True)
            result = [Recommendation(product.id, product.popularity / max(product.price, 1), "popularity-price score")
                      for product in ranked[:bounded_limit]]
        if self.mode() == "normal":
            if len(self._cache) >= 2_000:
                self._cache.pop(next(iter(self._cache)))
            self._cache[key] = result
        return result

    def set_mode(self, mode: str) -> bool:
        self._cache.clear()
        return faults.set(mode)

    def mode(self) -> str:
        return faults.get()

    def cache_size(self) -> int:
        return len(self._cache)
