"""Team members, roles and the audit log."""

from typing import Literal, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, EmailStr, Field

from app.common import MANAGERS, ROLES, audit, new_id, now_iso
from app.database import NO_ID, db
from app.dependencies import require_roles, ws_ctx

router = APIRouter(prefix="/api")


@router.get("/team")
async def list_team(ctx: dict = Depends(ws_ctx)):
    proj = (
        {"_id": 0, "user_id": 0}
        if ctx["role"] in MANAGERS
        else {"_id": 0, "user_id": 0, "email": 0, "spending_limit": 0}
    )
    return (
        await db.members.find({"workspace_id": ctx["ws_id"], "status": {"$ne": "removed"}}, proj)
        .sort("created_at", 1)
        .to_list(500)
    )


class MemberIn(BaseModel):
    name: str = Field(min_length=2, max_length=80)
    email: EmailStr
    role: Literal["owner", "finance_manager", "employee"]
    department: str = Field(min_length=1, max_length=40)
    title: str = Field("", max_length=80)
    spending_limit: float = Field(0, ge=0, le=100_000_000)


@router.post("/team")
async def add_member(body: MemberIn, ctx: dict = Depends(require_roles("owner"))):
    email = body.email.lower()
    if await db.members.find_one({"workspace_id": ctx["ws_id"], "email": email, "status": {"$ne": "removed"}}):
        raise HTTPException(status_code=409, detail="A member with this email already exists")
    doc = {
        **body.model_dump(),
        "email": email,
        "id": new_id(),
        "workspace_id": ctx["ws_id"],
        "user_id": None,
        "status": "invited",
        "is_synthetic": False,
        "created_at": now_iso(),
    }
    await db.members.insert_one(dict(doc))
    await audit(ctx, "member.invited", "member", doc["id"], f"{body.name} as {body.role}")
    doc.pop("_id", None)
    return doc


class MemberPatch(BaseModel):
    role: Optional[Literal["owner", "finance_manager", "employee"]] = None
    department: Optional[str] = Field(None, max_length=40)
    title: Optional[str] = Field(None, max_length=80)
    spending_limit: Optional[float] = Field(None, ge=0, le=100_000_000)
    status: Optional[Literal["active", "removed"]] = None


@router.patch("/team/{member_id}")
async def patch_member(member_id: str, body: MemberPatch, ctx: dict = Depends(require_roles("owner"))):
    target = await db.members.find_one({"id": member_id, "workspace_id": ctx["ws_id"]}, NO_ID)
    if not target:
        raise HTTPException(status_code=404, detail="Not found")
    upd = body.model_dump(exclude_none=True)
    if upd.get("status") == "active" and target["status"] == "invited":
        upd.pop("status")
    losing_owner = (
        target["role"] == "owner"
        and target["status"] == "active"
        and (upd.get("role", "owner") != "owner" or upd.get("status") == "removed")
    )
    if (
        losing_owner
        and await db.members.count_documents({"workspace_id": ctx["ws_id"], "role": "owner", "status": "active"}) <= 1
    ):
        raise HTTPException(status_code=409, detail="A workspace must keep at least one owner")
    await db.members.update_one({"id": member_id}, {"$set": upd})
    await audit(ctx, "member.updated", "member", member_id, ", ".join(f"{k}={v}" for k, v in upd.items()))
    return {"ok": True}


@router.get("/audit")
async def audit_log(ctx: dict = Depends(require_roles(*MANAGERS))):
    return await db.audit_logs.find({"workspace_id": ctx["ws_id"]}, NO_ID).sort("at", -1).limit(200).to_list(200)


@router.get("/roles")
async def roles():
    return {"roles": ROLES}
