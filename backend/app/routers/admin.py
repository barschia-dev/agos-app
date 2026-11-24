"""Platform administration."""

from typing import Literal, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from app.config import ENV
from app.database import NO_ID, db
from app.dependencies import require_platform

router = APIRouter(prefix="/api")


ADMINS = ("super_admin", "platform_admin")


@router.get("/admin/waitlist")
async def admin_waitlist(user: dict = Depends(require_platform(*ADMINS))):
    return await db.waitlist.find({}, NO_ID).sort("created_at", -1).to_list(5000)


class WaitlistPatch(BaseModel):
    status: Optional[Literal["new", "contacted", "approved_for_pilot", "archived"]] = None
    notes: Optional[str] = Field(None, max_length=1000)


@router.patch("/admin/waitlist/{entry_id}")
async def patch_waitlist(entry_id: str, body: WaitlistPatch, user: dict = Depends(require_platform(*ADMINS))):
    upd = body.model_dump(exclude_none=True)
    upd["updated_by_role"] = user["platform_role"]
    res = await db.waitlist.update_one({"id": entry_id}, {"$set": upd})
    if not res.matched_count:
        raise HTTPException(status_code=404, detail="Not found")
    return {"ok": True}


@router.get("/admin/overview")
async def admin_overview(user: dict = Depends(require_platform("super_admin"))):
    env = {"env": {"$in": [ENV, None]}}
    return {
        "users": await db.users.count_documents(env),
        "workspaces": await db.workspaces.count_documents({}),
        "demo_workspaces": await db.workspaces.count_documents({"is_demo": True}),
        "real_workspaces": await db.workspaces.count_documents({"is_demo": False}),
        "waitlist": await db.waitlist.count_documents({}),
        "stellar_confirmed": await db.payments.count_documents({"status": "confirmed"}),
        "platform_admins": await db.users.count_documents({"platform_role": "platform_admin", **env}),
    }
