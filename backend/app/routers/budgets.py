"""Budgets."""

from typing import Literal, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from app.common import MANAGERS, audit, new_id, now_iso
from app.database import NO_ID, db
from app.dependencies import require_roles, ws_ctx
from app.services.budgets import budgets_with_usage

router = APIRouter(prefix="/api")


class BudgetIn(BaseModel):
    name: str = Field(min_length=2, max_length=80)
    scope: Literal["business", "department", "employee"]
    department: Optional[str] = Field(None, max_length=40)
    member_id: Optional[str] = None
    amount: float = Field(gt=0, le=1_000_000_000)
    period: str = Field(pattern=r"^\d{4}-(0[1-9]|1[0-2])$")
    alert_pct: int = Field(80, ge=10, le=100)
    assigned_member_ids: list[str] = Field(default_factory=list, max_length=200)


async def _validate_budget(ctx, body: BudgetIn) -> dict:
    doc = body.model_dump()
    if body.scope == "department" and not body.department:
        raise HTTPException(status_code=422, detail="Department is required")
    if body.scope == "employee":
        m = await db.members.find_one({"id": body.member_id, "workspace_id": ctx["ws_id"]}, NO_ID)
        if not m:
            raise HTTPException(status_code=422, detail="Employee is required")
        doc["department"] = m["department"]
        doc["assigned_member_ids"] = [m["id"]]
    else:
        valid = await db.members.distinct("id", {"workspace_id": ctx["ws_id"], "id": {"$in": body.assigned_member_ids}})
        doc["assigned_member_ids"] = valid
        if body.scope == "business":
            doc["department"] = None
        doc["member_id"] = None
    return doc


@router.get("/budgets")
async def list_budgets(ctx: dict = Depends(ws_ctx), period: Optional[str] = None):
    q = {"period": period} if period else {}
    if ctx["role"] not in MANAGERS:
        m = ctx["member"]
        q["$or"] = [
            {"assigned_member_ids": m["id"]},
            {"member_id": m["id"]},
            {"scope": "department", "department": m["department"]},
        ]
    return await budgets_with_usage(ctx["ws_id"], q)


@router.post("/budgets")
async def create_budget(body: BudgetIn, ctx: dict = Depends(require_roles("owner"))):
    doc = {
        **await _validate_budget(ctx, body),
        "id": new_id(),
        "workspace_id": ctx["ws_id"],
        "currency": "PHP",
        "is_synthetic": False,
        "created_at": now_iso(),
    }
    await db.budgets.insert_one(dict(doc))
    await audit(ctx, "budget.created", "budget", doc["id"], f"{doc['name']} ₱{doc['amount']:,.0f} {doc['period']}")
    return (await budgets_with_usage(ctx["ws_id"], {"id": doc["id"]}))[0]


@router.put("/budgets/{budget_id}")
async def update_budget(budget_id: str, body: BudgetIn, ctx: dict = Depends(require_roles("owner"))):
    res = await db.budgets.update_one(
        {"id": budget_id, "workspace_id": ctx["ws_id"]}, {"$set": await _validate_budget(ctx, body)}
    )
    if not res.matched_count:
        raise HTTPException(status_code=404, detail="Not found")
    await audit(ctx, "budget.updated", "budget", budget_id, body.name)
    return (await budgets_with_usage(ctx["ws_id"], {"id": budget_id}))[0]


@router.delete("/budgets/{budget_id}")
async def delete_budget(budget_id: str, ctx: dict = Depends(require_roles("owner"))):
    res = await db.budgets.delete_one({"id": budget_id, "workspace_id": ctx["ws_id"]})
    if not res.deleted_count:
        raise HTTPException(status_code=404, detail="Not found")
    await audit(ctx, "budget.deleted", "budget", budget_id)
    return {"ok": True}
