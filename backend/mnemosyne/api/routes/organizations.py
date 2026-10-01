"""Organizations (services/organizations.py): people grouped by who they are with, with the facts
they gave across meetings; synced from HubSpot companies when connected."""

from fastapi import APIRouter, Depends, HTTPException

from ... import access
from ...services import organizations
from ...services.hubspot import HubSpotError
from ..context import AppContext, get_ctx

router = APIRouter(prefix="/api/organizations", tags=["organizations"])


@router.get("", response_model=list[organizations.OrganizationSummary])
async def list_organizations(ctx: AppContext = Depends(get_ctx)):
    return organizations.summaries(ctx)


@router.post("", response_model=organizations.Organization)
async def create_organization(
    data: organizations.OrganizationInput, ctx: AppContext = Depends(get_ctx)
):
    return organizations.save(ctx, data)


@router.post("/sync-hubspot", response_model=organizations.HubSpotSync)
async def sync_hubspot(ctx: AppContext = Depends(get_ctx)):
    """HubSpot companies and their contacts as organizations (reads HubSpot, writes nothing)."""
    access.require_admin()
    try:
        return await organizations.sync_hubspot(ctx)
    except HubSpotError as e:
        raise HTTPException(status_code=502, detail=str(e)) from e


@router.get("/{organization_id}", response_model=organizations.OrganizationDetail)
async def get_organization(organization_id: str, ctx: AppContext = Depends(get_ctx)):
    found = organizations.detail(ctx, organization_id)
    if found is None:
        raise HTTPException(status_code=404, detail="Organization not found")
    return found


@router.put("/{organization_id}", response_model=organizations.Organization)
async def update_organization(
    organization_id: str, data: organizations.OrganizationInput, ctx: AppContext = Depends(get_ctx)
):
    if organizations.get(ctx, organization_id) is None:
        raise HTTPException(status_code=404, detail="Organization not found")
    return organizations.save(ctx, data, organization_id)


@router.delete("/{organization_id}")
async def delete_organization(organization_id: str, ctx: AppContext = Depends(get_ctx)):
    """The grouping only: its people and meetings stay."""
    if not ctx.repo.delete_organization(organization_id):
        raise HTTPException(status_code=404, detail="Organization not found")
    return {"deleted": organization_id}
