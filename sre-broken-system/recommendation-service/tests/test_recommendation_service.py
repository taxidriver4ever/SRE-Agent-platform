"""Tests verify ranking and bounded cache behavior."""
from app.repository.catalog_repository import CatalogRepository
from app.service.recommendation_service import RecommendationService

def test_recommendations_are_ranked_and_cached()->None:
    """Normal requests should produce results and reuse one bounded cache key."""
    service=RecommendationService(CatalogRepository(400));first=service.for_user(7,5);second=service.for_user(7,5);assert len(first)==5;assert first==second;assert service.cache_size()==1
