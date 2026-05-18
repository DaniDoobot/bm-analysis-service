"""Health check router."""
from fastapi import APIRouter

router = APIRouter(tags=["Health"])


@router.get("/health")
async def health() -> dict:
    return {"ok": True, "service": "bm-analysis-service"}
