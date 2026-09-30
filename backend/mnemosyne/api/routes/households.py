"""Households (services/households.py): clients grouped as the firm serves them, with the facts
they gave across meetings; synced from HubSpot companies when connected."""

from fastapi import APIRouter, Depends, HTTPException

from ... import access
from ...services import households
from ...services.hubspot import HubSpotError
from ..context import AppContext, get_ctx

router = APIRouter(prefix="/api/households", tags=["households"])


@router.get("", response_model=list[households.HouseholdSummary])
async def list_households(ctx: AppContext = Depends(get_ctx)):
    return households.summaries(ctx)


@router.post("", response_model=households.Household)
async def create_household(data: households.HouseholdInput, ctx: AppContext = Depends(get_ctx)):
    return households.save(ctx, data)


@router.post("/sync-hubspot", response_model=households.HubSpotSync)
async def sync_hubspot(ctx: AppContext = Depends(get_ctx)):
    """HubSpot companies and their contacts as households (reads HubSpot, writes nothing)."""
    access.require_admin()
    try:
        return await households.sync_hubspot(ctx)
    except HubSpotError as e:
        raise HTTPException(status_code=502, detail=str(e)) from e


@router.get("/{household_id}", response_model=households.HouseholdDetail)
async def get_household(household_id: str, ctx: AppContext = Depends(get_ctx)):
    found = households.detail(ctx, household_id)
    if found is None:
        raise HTTPException(status_code=404, detail="Household not found")
    return found


@router.put("/{household_id}", response_model=households.Household)
async def update_household(
    household_id: str, data: households.HouseholdInput, ctx: AppContext = Depends(get_ctx)
):
    if households.get(ctx, household_id) is None:
        raise HTTPException(status_code=404, detail="Household not found")
    return households.save(ctx, data, household_id)


@router.delete("/{household_id}")
async def delete_household(household_id: str, ctx: AppContext = Depends(get_ctx)):
    """The grouping only: its people and meetings stay."""
    if not ctx.repo.delete_household(household_id):
        raise HTTPException(status_code=404, detail="Household not found")
    return {"deleted": household_id}
