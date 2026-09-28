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

@router.get("/recommendations/{user_id}")
@router.get("/recommendations/users/{user_id}")
async def user_recommendations(user_id:int,request:Request,limit:int=Query(10,ge=1,le=50))->list[dict]:
    import os, httpx, asyncio
    from starlette.concurrency import run_in_threadpool
    url=os.getenv("USER_BASE_URL","http://user-service:8082")
    if service.mode()=="user_dependency_timeout": await asyncio.sleep(faults.parameters["delay_ms"]/1000)
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(0.5,connect=0.2)) as client:
            response=await client.get(f"{url}/users/{user_id}/preferences",headers={"x-request-id":request.state.request_id})
        if response.status_code==404: raise HTTPException(404,"user not found")
        response.raise_for_status()
        preference=response.json()
        result=await run_in_threadpool(service._rank,f"user:{user_id}:{preference['category']}",preference["category"],limit)
        return [asdict(item) for item in result]
    except httpx.TimeoutException as exc: raise HTTPException(504,"user preferences timeout") from exc
    except (httpx.HTTPError,KeyError,ValueError) as exc: raise HTTPException(502,"user preferences unavailable") from exc

@router.api_route("/debug/fault",methods=["GET","POST"])
def fault(mode:str|None=None)->dict[str,str|int]:
    """Control cache/algorithm failure modes using a strict whitelist."""
    if mode is not None and not service.set_mode(mode):raise HTTPException(400,"unsupported fault mode")
    return{"fault_mode":service.mode(),"cache_entries":service.cache_size()}

from pydantic import BaseModel, Field
from app.core.faults import faults
class FaultInput(BaseModel):
    fault: str
    duration_seconds: float = Field(120, ge=1, le=300)
    parameters: dict = Field(default_factory=dict)
@router.get("/internal/faults")
def get_faults(): return faults.snapshot()
@router.post("/internal/faults")
def set_faults(body: FaultInput):
    if not faults.set(body.fault, body.duration_seconds, body.parameters): raise HTTPException(400,"invalid fault parameters")
    return faults.snapshot()
@router.delete("/internal/faults/{name}")
def clear_fault(name: str):
    if faults.get()==name: faults.set("normal")
    return faults.snapshot()
