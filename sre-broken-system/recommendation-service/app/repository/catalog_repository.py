"""Deterministic synthetic catalog and indexed access paths."""

from collections import defaultdict
from app.model.product import Product


class CatalogRepository:
    """Keep both primary-key and category indexes so BAD history can visibly remove an efficient path."""

    def __init__(self, size: int = 20_000) -> None:
        self._products = [Product(product_id, f"category-{product_id % 40}",
                                  ((product_id * 37) % 1000) / 1000, 5 + (product_id * 17) % 500)
                          for product_id in range(1, size + 1)]
        self._by_id = {product.id: product for product in self._products}
        self._by_category: dict[str, list[Product]] = defaultdict(list)
        for product in self._products:
            self._by_category[product.category].append(product)

    def get(self, product_id: int) -> Product | None:
        """Primary-key lookup remains O(1) in the GOOD implementation."""
        return self._by_id.get(product_id)

    def category(self, name: str) -> list[Product]:
        """Return a copy so ranking cannot mutate the shared category index."""
        return list(self._by_category.get(name, []))

    def full_scan(self) -> list[Product]:
        """Explicit full scan is reserved for the large_scan failure scenario."""
        return list(self._products)
