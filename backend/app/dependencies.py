"""FastAPI dependencies: the signed-in user, their workspace and role checks."""

import requests
from fastapi import Depends, HTTPException, Request
from starlette.concurrency import run_in_threadpool

from app.clerk_auth import require_user
from app.common import now_iso
from app.config import CLERK_SECRET_KEY, ENV, PLATFORM_ADMIN_EMAILS, SUPER_ADMIN_EMAILS
from app.database import db


def _clerk_profile(user_id: str) -> dict:
    r = requests.get(
        f"https://api.clerk.com/v1/users/{user_id}",
        timeout=10,
        headers={"Authorization": f"Bearer {CLERK_SECRET_KEY}", "User-Agent": "agos-app"},
    )
    r.raise_for_status()
    data = r.json()
    verified = [
        e["email_address"].lower()
        for e in data.get("email_addresses", [])
        if (e.get("verification") or {}).get("status") == "verified"
    ]
    name = " ".join(x for x in [data.get("first_name"), data.get("last_name")] if x) or (
        verified[0].split("@")[0] if verified else "AGOS User"
    )
    return {"emails": verified, "name": name}


async def current_user(user: dict = Depends(require_user)) -> dict:
    uid = user["user_id"]
    doc = await db.users.find_one({"user_id": uid}, {"_id": 0})
    if doc is None or "emails" not in doc:
        prof = await run_in_threadpool(_clerk_profile, uid)
        emails = set(prof["emails"])
        role = (
            "super_admin"
            if emails & SUPER_ADMIN_EMAILS
            else ("platform_admin" if emails & PLATFORM_ADMIN_EMAILS else "user")
        )
        doc = {
            "user_id": uid,
            "emails": prof["emails"],
            "email": prof["emails"][0] if prof["emails"] else "",
            "name": prof["name"],
            "platform_role": role,
            "env": ENV,
            "created_at": now_iso(),
        }
        await db.users.update_one({"user_id": uid}, {"$setOnInsert": doc}, upsert=True)
        doc = await db.users.find_one({"user_id": uid}, {"_id": 0})
    return doc


def require_platform(*roles: str):
    async def check(user: dict = Depends(current_user)) -> dict:
        if user.get("platform_role") not in roles:
            raise HTTPException(status_code=403, detail="Not allowed")
        return user

    return check


async def ws_ctx(request: Request, user: dict = Depends(current_user)) -> dict:
    ws_id = request.headers.get("x-workspace-id")
    if not ws_id:
        raise HTTPException(status_code=400, detail="Workspace not selected")
    member = await db.members.find_one(
        {"workspace_id": ws_id, "user_id": user["user_id"], "status": "active"}, {"_id": 0}
    )
    if not member:
        raise HTTPException(status_code=404, detail="Workspace not found")
    ws = await db.workspaces.find_one({"id": ws_id}, {"_id": 0})
    acting = member
    act_as = request.headers.get("x-act-as")
    if act_as and act_as != member["id"] and ws.get("is_demo") and member["role"] == "owner":
        persona = await db.members.find_one({"id": act_as, "workspace_id": ws_id, "status": "active"}, {"_id": 0})
        if persona:
            acting = persona
    actor_name = acting["name"] if acting is member else f"{acting['name']} (demo persona)"
    return {
        "ws": ws,
        "ws_id": ws_id,
        "member": acting,
        "real_member": member,
        "user": user,
        "role": acting["role"],
        "actor_name": actor_name,
    }


def require_roles(*roles: str):
    async def check(ctx: dict = Depends(ws_ctx)) -> dict:
        if ctx["role"] not in roles:
            raise HTTPException(status_code=403, detail="Your role cannot perform this action")
        return ctx

    return check
