"""Audit history, today's reserved spend, and the configured chat models."""
from fastapi import APIRouter, Depends

from ..deps import current_principal
from ... import config
from ...llm import registry
from ...services import audit, limits
from ...services.auth import Principal

router = APIRouter()


@router.get("/v1/runs")
def runs(principal: Principal = Depends(current_principal)) -> dict:
    return {"runs": audit.list_for(principal.user_id)}


@router.get("/v1/spend")
def spend(principal: Principal = Depends(current_principal)) -> dict:
    return {"spent": limits.spent(principal.user_id), "cap": config.llm_daily_budget_usd()}


@router.get("/v1/models")
def models(_principal: Principal = Depends(current_principal)) -> dict:
    return {"models": registry.catalog()}
