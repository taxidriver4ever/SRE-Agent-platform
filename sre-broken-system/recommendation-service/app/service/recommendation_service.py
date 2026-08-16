"""Recommendation ranking, cache policy and genuine algorithmic failure modes."""

from threading import Lock
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
        if self.mode() != "cache_miss" and key in self._cache:
            return self._cache[key][:bounded_limit]
        # BAD: a personalization shortcut removed the category index and scans the whole catalog
        # on every cache miss, creating version-specific CPU and latency regression.
        candidates = self.repository.full_scan()
        if self.mode() == "quadratic_ranking":
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
        if self.mode() != "cache_miss":
            if len(self._cache) >= 2_000:
                self._cache.pop(next(iter(self._cache)))
            self._cache[key] = result
        return result

    def set_mode(self, mode: str) -> bool:
        if mode not in self._allowed_modes:
            return False
        with self._lock:
            self._mode = mode
            if mode == "normal":
                self._cache.clear()
        return True

    def mode(self) -> str:
        with self._lock:
            return self._mode

    def cache_size(self) -> int:
        return len(self._cache)
