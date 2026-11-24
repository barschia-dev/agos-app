"""Expenses and receipts."""

import re
from typing import Literal, Optional

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile
from fastapi.responses import Response
from pydantic import BaseModel, Field

from app import storage
from app.common import MANAGERS, audit, csv_response, history_entry, new_id, now_iso, system_entry, to_php
from app.config import APP_NAME
from app.database import NO_ID, db
from app.dependencies import require_roles, ws_ctx
from app.services.approvals import Decision, can_decide
from app.services.budgets import COUNTED, budget_warnings
from app.services.expenses import find_expense, own_expenses

router = APIRouter(prefix="/api")


ALLOWED_TYPES = {"image/jpeg": "jpg", "image/png": "png", "image/webp": "webp", "application/pdf": "pdf"}
MAX_UPLOAD = 10 * 1024 * 1024


class ExpenseIn(BaseModel):
    merchant: str = Field(min_length=1, max_length=120)
    category: str = Field(min_length=1, max_length=60)
    amount: float = Field(gt=0, le=100_000_000)
    currency: Literal["PHP", "USD"] = "PHP"
    date: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")
    description: str = Field("", max_length=500)
    notes: str = Field("", max_length=1000)
    member_id: Optional[str] = None
    department: Optional[str] = Field(None, max_length=40)
    submit: bool = False


def _expense_query(ctx, status, department, member_id, q, date_from, date_to) -> dict:
    query = {"workspace_id": ctx["ws_id"], **own_expenses(ctx)}
    if status:
        query["status"] = status
    if department:
        query["department"] = department
    if member_id and ctx["role"] in MANAGERS:
        query["member_id"] = member_id
    if q:
        rx = {"$regex": re.escape(q[:60]), "$options": "i"}
        query["$or"] = [{"merchant": rx}, {"description": rx}, {"category": rx}, {"member_name": rx}]
    if date_from or date_to:
        query["date"] = {k: v for k, v in (("$gte", date_from), ("$lte", date_to)) if v}
    return query


@router.get("/expenses")
async def list_expenses(
    ctx: dict = Depends(ws_ctx),
    status: Optional[str] = None,
    department: Optional[str] = None,
    member_id: Optional[str] = None,
    q: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
):
    query = _expense_query(ctx, status, department, member_id, q, date_from, date_to)
    return await db.expenses.find(query, NO_ID).sort("date", -1).to_list(2000)


@router.get("/expenses/export")
async def export_expenses(
    ctx: dict = Depends(ws_ctx),
    status: Optional[str] = None,
    department: Optional[str] = None,
    q: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
):
    rows = (
        await db.expenses.find(_expense_query(ctx, status, department, None, q, date_from, date_to), NO_ID)
        .sort("date", -1)
        .to_list(5000)
    )
    for r in rows:
        r["data_type"] = "Simulated demo record" if r.get("is_synthetic") else "Recorded"
        r["receipt_count"] = len(r.get("receipts", []))
    return csv_response(
        rows,
        [
            "id",
            "date",
            "merchant",
            "category",
            "department",
            "member_name",
            "amount",
            "currency",
            "amount_php",
            "status",
            "receipt_count",
            "description",
            "data_type",
        ],
        "agos-expenses.csv",
    )


async def _route_for_approval(ctx: dict, exp: dict) -> None:
    exp["history"].append(history_entry(ctx, "submitted"))
    exp["history"].append(
        system_entry(
            "pending_approval",
            "Routed to "
            + (
                "Owner (above approval threshold)"
                if exp["amount_php"] > ctx["ws"].get("approval_threshold", 50000)
                else "Finance Manager or Owner"
            ),
        )
    )
    exp["status"] = "pending_approval"


@router.post("/expenses")
async def create_expense(body: ExpenseIn, ctx: dict = Depends(ws_ctx)):
    member = ctx["member"]
    if body.member_id and body.member_id != member["id"]:
        if ctx["role"] not in MANAGERS:
            raise HTTPException(status_code=403, detail="Only managers can assign expenses to others")
        member = await db.members.find_one(
            {"id": body.member_id, "workspace_id": ctx["ws_id"], "status": {"$ne": "removed"}}, NO_ID
        )
        if not member:
            raise HTTPException(status_code=404, detail="Member not found")
    exp = {
        **body.model_dump(exclude={"submit", "member_id"}),
        "id": new_id(),
        "workspace_id": ctx["ws_id"],
        "member_id": member["id"],
        "member_name": member["name"],
        "department": body.department or member["department"],
        "amount_php": to_php(body.amount, body.currency),
        "status": "draft",
        "receipts": [],
        "card_id": None,
        "history": [history_entry(ctx, "draft")],
        "reconciled": False,
        "is_synthetic": False,
        "created_by": ctx["member"]["id"],
        "created_at": now_iso(),
    }
    if body.submit:
        await _route_for_approval(ctx, exp)
    warnings = await budget_warnings(ctx["ws_id"], exp)
    limit = member.get("spending_limit") or 0
    if limit:
        spent = sum(
            e["amount_php"]
            for e in await db.expenses.find(
                {
                    "workspace_id": ctx["ws_id"],
                    "member_id": member["id"],
                    "status": {"$in": COUNTED},
                    "date": {"$regex": "^" + exp["date"][:7]},
                },
                {"_id": 0, "amount_php": 1},
            ).to_list(5000)
        )
        if spent + exp["amount_php"] > limit:
            warnings.append(f"Exceeds {member['name']}'s monthly spending limit of ₱{limit:,.0f}")
    exp["warnings"] = warnings
    await db.expenses.insert_one(dict(exp))
    await audit(ctx, "expense.created", "expense", exp["id"], f"{exp['merchant']} {exp['amount']} {exp['currency']}")
    exp.pop("_id", None)
    return exp


class ExpensePatch(BaseModel):
    merchant: Optional[str] = Field(None, min_length=1, max_length=120)
    category: Optional[str] = Field(None, max_length=60)
    amount: Optional[float] = Field(None, gt=0, le=100_000_000)
    currency: Optional[Literal["PHP", "USD"]] = None
    date: Optional[str] = Field(None, pattern=r"^\d{4}-\d{2}-\d{2}$")
    description: Optional[str] = Field(None, max_length=500)
    notes: Optional[str] = Field(None, max_length=1000)


@router.patch("/expenses/{exp_id}")
async def patch_expense(exp_id: str, body: ExpensePatch, ctx: dict = Depends(ws_ctx)):
    e = await find_expense(ctx, exp_id)
    if e["status"] not in ("draft", "rejected"):
        raise HTTPException(status_code=409, detail="Only draft or rejected expenses can be edited")
    upd = body.model_dump(exclude_none=True)
    merged = {**e, **upd}
    upd["amount_php"] = to_php(merged["amount"], merged["currency"])
    await db.expenses.update_one({"id": exp_id}, {"$set": upd})
    return await find_expense(ctx, exp_id)


@router.post("/expenses/{exp_id}/submit")
async def submit_expense(exp_id: str, ctx: dict = Depends(ws_ctx)):
    e = await find_expense(ctx, exp_id)
    if e["status"] not in ("draft", "rejected"):
        raise HTTPException(status_code=409, detail="Expense is already submitted")
    await _route_for_approval(ctx, e)
    res = await db.expenses.update_one(
        {"id": exp_id, "status": {"$in": ["draft", "rejected"]}},
        {"$set": {"status": e["status"], "history": e["history"]}},
    )
    if not res.modified_count:
        raise HTTPException(status_code=409, detail="Expense changed; refresh and retry")
    await audit(ctx, "expense.submitted", "expense", exp_id)
    return await find_expense(ctx, exp_id)


@router.post("/expenses/{exp_id}/decision")
async def decide_expense(exp_id: str, body: Decision, ctx: dict = Depends(require_roles(*MANAGERS))):
    e = await find_expense(ctx, exp_id)
    if e["status"] != "pending_approval":
        raise HTTPException(status_code=409, detail="Expense is not pending approval")
    err = can_decide(ctx, e["amount_php"], e["member_id"])
    if err:
        raise HTTPException(status_code=403, detail=err)
    if body.action == "reject" and not body.note.strip():
        raise HTTPException(status_code=422, detail="A reason is required to reject")
    status = "approved" if body.action == "approve" else "rejected"
    res = await db.expenses.update_one(
        {"id": exp_id, "status": "pending_approval"},
        {"$set": {"status": status}, "$push": {"history": history_entry(ctx, status, body.note)}},
    )
    if not res.modified_count:
        raise HTTPException(status_code=409, detail="Already decided")
    await audit(ctx, f"expense.{status}", "expense", exp_id, body.note)
    return await find_expense(ctx, exp_id)


@router.post("/expenses/{exp_id}/mark-paid")
async def mark_paid(exp_id: str, ctx: dict = Depends(require_roles(*MANAGERS))):
    res = await db.expenses.update_one(
        {"id": exp_id, "workspace_id": ctx["ws_id"], "status": "approved"},
        {
            "$set": {"status": "paid"},
            "$push": {"history": history_entry(ctx, "paid", "Recorded as paid — no funds moved by AGOS")},
        },
    )
    if not res.modified_count:
        raise HTTPException(status_code=409, detail="Only approved expenses can be recorded as paid")
    await audit(ctx, "expense.recorded_paid", "expense", exp_id)
    return await find_expense(ctx, exp_id)


@router.post("/expenses/{exp_id}/receipts")
async def upload_receipt(exp_id: str, file: UploadFile = File(...), ctx: dict = Depends(ws_ctx)):
    e = await find_expense(ctx, exp_id)
    if file.content_type not in ALLOWED_TYPES:
        raise HTTPException(status_code=415, detail="Receipts must be JPG, PNG, WEBP or PDF")
    data = await file.read(MAX_UPLOAD + 1)
    if len(data) > MAX_UPLOAD:
        raise HTTPException(status_code=413, detail="File too large (max 10 MB)")
    fid = new_id()
    path = f"{APP_NAME}/receipts/{ctx['ws_id']}/{fid}.{ALLOWED_TYPES[file.content_type]}"
    try:
        result = await storage.put_object(path, data, file.content_type)
    except Exception:
        raise HTTPException(status_code=502, detail="Receipt storage is unavailable; try again")
    rec = {
        "id": fid,
        "workspace_id": ctx["ws_id"],
        "expense_id": exp_id,
        "storage_path": result["path"],
        "filename": (file.filename or "receipt")[:120],
        "content_type": file.content_type,
        "size": len(data),
        "uploaded_by": ctx["actor_name"],
        "is_deleted": False,
        "created_at": now_iso(),
    }
    await db.files.insert_one(dict(rec))
    meta = {k: rec[k] for k in ("id", "filename", "content_type", "size", "created_at")}
    await db.expenses.update_one({"id": exp_id}, {"$push": {"receipts": meta}})
    await audit(ctx, "receipt.uploaded", "expense", e["id"], meta["filename"])
    return meta


@router.get("/files/{file_id}")
async def download_file(file_id: str, ctx: dict = Depends(ws_ctx)):
    f = await db.files.find_one({"id": file_id, "workspace_id": ctx["ws_id"], "is_deleted": False}, NO_ID)
    if not f:
        raise HTTPException(status_code=404, detail="Not found")
    await find_expense(ctx, f["expense_id"])
    try:
        data, _ = await storage.get_object(f["storage_path"])
    except FileNotFoundError:
        raise HTTPException(status_code=404, detail="Receipt file is not available")
    return Response(content=data, media_type=f["content_type"])
