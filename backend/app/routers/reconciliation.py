"""Reconciliation."""

from collections import defaultdict
from typing import Literal

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel

from app.common import MANAGERS, audit, csv_response
from app.database import db
from app.dependencies import require_roles
from app.services.budgets import COUNTED
from app.services.reporting import DISCLAIMER, reconciliation_rows

router = APIRouter(prefix="/api")


@router.get("/reconciliation")
async def reconciliation(ctx: dict = Depends(require_roles(*MANAGERS))):
    rows = await reconciliation_rows(ctx)
    summary = defaultdict(int)
    for r in rows:
        summary[r["reconciliation_status"]] += 1
    return {"rows": rows, "summary": summary, "disclaimer": DISCLAIMER}


@router.get("/reconciliation/export")
async def reconciliation_export(ctx: dict = Depends(require_roles(*MANAGERS))):
    return csv_response(
        await reconciliation_rows(ctx),
        [
            "reference",
            "date",
            "kind",
            "description",
            "method",
            "amount",
            "currency",
            "category",
            "approval_status",
            "payment_status",
            "reconciliation_status",
            "evidence",
            "ledger",
            "explorer",
            "is_synthetic",
        ],
        "agos-reconciliation.csv",
    )


class ReconIn(BaseModel):
    kind: Literal["expense", "payment"]
    reconciled: bool


@router.post("/reconciliation/{item_id}")
async def toggle_reconciled(item_id: str, body: ReconIn, ctx: dict = Depends(require_roles(*MANAGERS))):
    coll = db.expenses if body.kind == "expense" else db.payments
    allowed = COUNTED if body.kind == "expense" else ["recorded_as_paid", "confirmed"]
    res = await coll.update_one(
        {"id": item_id, "workspace_id": ctx["ws_id"], "status": {"$in": allowed}},
        {"$set": {"reconciled": body.reconciled}},
    )
    if not res.matched_count:
        raise HTTPException(status_code=409, detail="Item is not eligible for reconciliation")
    await audit(ctx, "reconciliation." + ("marked" if body.reconciled else "unmarked"), body.kind, item_id)
    return {"ok": True}
