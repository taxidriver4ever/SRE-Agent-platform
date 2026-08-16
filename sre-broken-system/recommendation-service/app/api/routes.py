"""HTTP contracts for product and user recommendation use cases."""
from dataclasses import asdict
from fastapi import APIRouter, HTTPException, Query, Request
from app.observability.telemetry import CACHE_SIZE, LABELS, log_event
from app.repository.catalog_repository import CatalogRepository
from app.service.recommendation_service import RecommendationService

router=APIRouter();service=RecommendationService(CatalogRepository())

@router.get("/recommendations/products/{product_id}")
def product_recommendations(product_id:int,request:Request,limit:int=Query(10,ge=1,le=50))->list[dict]:
    """Return related catalog products and retain incoming trace identity."""
    result=service.for_product(product_id,limit)
    if not result:raise HTTPException(404,"product not found")
    CACHE_SIZE.labels(*LABELS).set(service.cache_size());log_event("product recommendations calculated",request.state.trace_id,product_id=product_id,fault_mode=service.mode())
    return[asdict(item)for item in result]

@router.get("/recommendations/users/{user_id}")
def user_recommendations(user_id:int,request:Request,limit:int=Query(10,ge=1,le=50))->list[dict]:
    """Return deterministic personalized recommendations."""
    result=service.for_user(user_id,limit);CACHE_SIZE.labels(*LABELS).set(service.cache_size());log_event("user recommendations calculated",request.state.trace_id,user_id=user_id,fault_mode=service.mode());return[asdict(item)for item in result]

@router.api_route("/debug/fault",methods=["GET","POST"])
def fault(mode:str|None=None)->dict[str,str|int]:
    """Control cache/algorithm failure modes using a strict whitelist."""
    if mode is not None and not service.set_mode(mode):raise HTTPException(400,"unsupported fault mode")
    return{"fault_mode":service.mode(),"cache_entries":service.cache_size()}
