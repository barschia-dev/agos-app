"""Workspaces and the signed-in user."""

from typing import Literal, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, EmailStr, Field

from app.common import DEFAULT_DEPARTMENTS, audit, new_id, now_iso
from app.database import NO_ID, db
from app.demo_seed import seed_demo
from app.dependencies import current_user, require_roles, ws_ctx

router = APIRouter(prefix="/api")


async def _my_workspaces(user: dict) -> list:
    mems = await db.members.find({"user_id": user["user_id"], "status": "active"}, NO_ID).to_list(200)
    ids = [m["id"] for m in mems]
    by_ws = {m["workspace_id"]: m for m in mems}
    wss = await db.workspaces.find({"id": {"$in": list(by_ws)}}, NO_ID).to_list(200)
    return (
        [
            {
                "id": w["id"],
                "name": w["name"],
                "is_demo": w.get("is_demo", False),
                "role": by_ws[w["id"]]["role"],
                "member_id": by_ws[w["id"]]["id"],
                "industry": w.get("industry"),
            }
            for w in wss
        ]
        if ids
        else []
    )


@router.get("/me")
async def me(user: dict = Depends(current_user)):
    # Link workspace invitations addressed to this user's verified email.
    if user.get("emails"):
        await db.members.update_many(
            {"email": {"$in": user["emails"]}, "user_id": None, "status": "invited", "is_synthetic": False},
            {"$set": {"user_id": user["user_id"], "status": "active", "joined_at": now_iso()}},
        )
    return {
        "name": user["name"],
        "email": user.get("email"),
        "platform_role": user.get("platform_role", "user"),
        "workspaces": await _my_workspaces(user),
    }


class WorkspaceIn(BaseModel):
    name: str = Field(min_length=2, max_length=120)
    category: str = Field(min_length=2, max_length=60)
    industry: str = Field(min_length=2, max_length=80)
    location: str = Field(min_length=2, max_length=120)
    business_email: EmailStr
    admin_name: str = Field(min_length=2, max_length=80)
    team_size: Literal["1-10", "11-50", "51-200", "200+"]
    default_currency: Literal["PHP", "USD"] = "PHP"


@router.post("/workspaces")
async def create_workspace(body: WorkspaceIn, user: dict = Depends(current_user)):
    ws_id = new_id()
    await db.workspaces.insert_one(
        {
            **body.model_dump(),
            "id": ws_id,
            "is_demo": False,
            "approval_threshold": 50000,
            "departments": DEFAULT_DEPARTMENTS,
            "owner_user_id": user["user_id"],
            "created_at": now_iso(),
        }
    )
    await db.members.insert_one(
        {
            "id": new_id(),
            "workspace_id": ws_id,
            "user_id": user["user_id"],
            "name": body.admin_name,
            "email": user.get("email", ""),
            "role": "owner",
            "department": "Executive",
            "title": "Administrator",
            "spending_limit": 0,
            "status": "active",
            "is_synthetic": False,
            "created_at": now_iso(),
        }
    )
    return {"id": ws_id}


@router.post("/workspaces/demo")
async def create_demo(user: dict = Depends(current_user)):
    existing = await db.workspaces.count_documents({"owner_user_id": user["user_id"], "is_demo": True})
    if existing >= 3:
        raise HTTPException(status_code=409, detail="You already have the maximum of 3 demo workspaces")
    return {"id": await seed_demo(user)}


@router.get("/workspace")
async def get_workspace(ctx: dict = Depends(ws_ctx)):
    personas = []
    if ctx["ws"].get("is_demo") and ctx["real_member"]["role"] == "owner":
        personas = await db.members.find(
            {"workspace_id": ctx["ws_id"], "status": "active"},
            {"_id": 0, "id": 1, "name": 1, "role": 1, "department": 1},
        ).to_list(50)
    ws = {k: v for k, v in ctx["ws"].items() if k != "owner_user_id"}
    return {
        "workspace": ws,
        "member": ctx["member"],
        "role": ctx["role"],
        "real_member_id": ctx["real_member"]["id"],
        "acting_as_persona": ctx["member"]["id"] != ctx["real_member"]["id"],
        "personas": personas,
    }


class WorkspacePatch(BaseModel):
    name: Optional[str] = Field(None, min_length=2, max_length=120)
    category: Optional[str] = Field(None, max_length=60)
    industry: Optional[str] = Field(None, max_length=80)
    location: Optional[str] = Field(None, max_length=120)
    business_email: Optional[EmailStr] = None
    default_currency: Optional[Literal["PHP", "USD"]] = None
    approval_threshold: Optional[float] = Field(None, ge=0, le=100_000_000)
    departments: Optional[list[str]] = Field(None, max_length=30)


@router.patch("/workspace")
async def patch_workspace(body: WorkspacePatch, ctx: dict = Depends(require_roles("owner"))):
    upd = body.model_dump(exclude_none=True)
    if "departments" in upd:
        upd["departments"] = [d.strip()[:40] for d in upd["departments"] if d.strip()]
    await db.workspaces.update_one({"id": ctx["ws_id"]}, {"$set": upd})
    await audit(ctx, "workspace.updated", "workspace", ctx["ws_id"], ", ".join(upd))
    return {"ok": True}
