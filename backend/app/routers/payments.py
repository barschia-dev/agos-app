"""Payment requests and approvals."""

from decimal import Decimal, InvalidOperation
from typing import Literal, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field, field_validator

from app import stellar
from app.common import MANAGERS, audit, history_entry, new_id, now_iso, to_php
from app.database import NO_ID, db
from app.dependencies import require_roles, ws_ctx
from app.services.approvals import Decision, can_decide
from app.services.payments import find_payment

router = APIRouter(prefix="/api")


MAX_XLM = Decimal("1000")
FM_XLM_LIMIT = 100.0


class PaymentIn(BaseModel):
    type: Literal["domestic", "international"]
    recipient_name: str = Field(min_length=2, max_length=120)
    recipient_bank: Optional[str] = Field(None, max_length=60)
    recipient_account_last4: Optional[str] = Field(None, pattern=r"^\d{4}$")
    destination_address: Optional[str] = Field(None, max_length=56)
    asset_code: Optional[str] = "XLM"
    amount: str = Field(min_length=1, max_length=20)
    purpose: str = Field(min_length=3, max_length=200)

    @field_validator("amount")
    @classmethod
    def _amount(cls, v):
        try:
            d = Decimal(v)
        except InvalidOperation:
            raise ValueError("Amount must be a number")
        if d <= 0 or d.as_tuple().exponent < -7:
            raise ValueError("Amount must be positive with at most 7 decimals")
        return v


@router.get("/payments")
async def list_payments(ctx: dict = Depends(ws_ctx), type: Optional[str] = None):
    q = {"workspace_id": ctx["ws_id"]}
    if ctx["role"] not in MANAGERS:
        q["requested_by"] = ctx["member"]["id"]
    if type:
        q["type"] = type
    docs = await db.payments.find(q, NO_ID).sort("created_at", -1).to_list(1000)
    for p in docs:
        if p.get("stellar") and p["stellar"].get("tx_hash"):
            p["stellar"]["links"] = stellar.tx_links(p["stellar"]["tx_hash"])
    return docs


@router.get("/payments/{pid}")
async def get_payment(pid: str, ctx: dict = Depends(ws_ctx)):
    return await find_payment(ctx, pid)


@router.post("/payments")
async def create_payment(body: PaymentIn, ctx: dict = Depends(ws_ctx)):
    amount = Decimal(body.amount)
    doc = {
        "id": new_id(),
        "workspace_id": ctx["ws_id"],
        "type": body.type,
        "recipient_name": body.recipient_name,
        "purpose": body.purpose,
        "status": "pending_approval",
        "requested_by": ctx["member"]["id"],
        "requested_by_name": ctx["member"]["name"],
        "stellar": None,
        "reconciled": False,
        "is_synthetic": False,
        "created_at": now_iso(),
    }
    if body.type == "domestic":
        doc.update(
            {
                "rail": "demo_bank_transfer",
                "recipient_bank": body.recipient_bank or "Demo Bank",
                "recipient_account_last4": body.recipient_account_last4,
                "destination_address": None,
                "asset_code": None,
                "amount": float(amount),
                "currency": "PHP",
                "amount_php": to_php(float(amount), "PHP"),
            }
        )
    else:
        if body.asset_code not in stellar.ASSETS or not stellar.ASSETS[body.asset_code]["enabled"]:
            raise HTTPException(status_code=422, detail="Unsupported asset")
        if not stellar.valid_address(body.destination_address):
            raise HTTPException(
                status_code=422, detail="Destination must be a valid Stellar public key (G…, 56 characters)"
            )
        if amount > MAX_XLM:
            raise HTTPException(status_code=422, detail="Testnet demo payments are capped at 1,000 XLM")
        acct = await db.stellar_accounts.find_one({"workspace_id": ctx["ws_id"]}, {"_id": 0, "public_key": 1})
        if acct and acct["public_key"] == body.destination_address:
            raise HTTPException(status_code=422, detail="Destination cannot be the workspace's own account")
        meta = stellar.ASSETS[body.asset_code]
        doc.update(
            {
                "rail": "stellar_testnet",
                "recipient_bank": None,
                "recipient_account_last4": None,
                "destination_address": body.destination_address,
                "asset_code": meta["code"],
                "asset_issuer": meta["issuer"],
                "amount": float(amount),
                "amount_str": f"{amount:.7f}",
                "currency": meta["code"],
                "amount_php": 0,
            }
        )
    doc["history"] = [history_entry(ctx, "pending_approval", "Payment request submitted")]
    await db.payments.insert_one(dict(doc))
    await audit(ctx, "payment.requested", "payment", doc["id"], f"{body.type} {body.amount} {doc['currency']}")
    return await find_payment(ctx, doc["id"])


@router.post("/payments/{pid}/decision")
async def decide_payment(pid: str, body: Decision, ctx: dict = Depends(require_roles(*MANAGERS))):
    p = await find_payment(ctx, pid)
    if p["status"] != "pending_approval":
        raise HTTPException(status_code=409, detail="Payment is not pending approval")
    err = can_decide(ctx, p["amount_php"], p["requested_by"])
    if not err and p["currency"] == "XLM" and ctx["role"] == "finance_manager" and p["amount"] > FM_XLM_LIMIT:
        err = f"Stellar payments above {FM_XLM_LIMIT:g} XLM require owner approval"
    if err:
        raise HTTPException(status_code=403, detail=err)
    if body.action == "reject" and not body.note.strip():
        raise HTTPException(status_code=422, detail="A reason is required to reject")
    status = "approved" if body.action == "approve" else "rejected"
    res = await db.payments.update_one(
        {"id": pid, "status": "pending_approval"},
        {"$set": {"status": status}, "$push": {"history": history_entry(ctx, status, body.note)}},
    )
    if not res.modified_count:
        raise HTTPException(status_code=409, detail="Already decided")
    await audit(ctx, f"payment.{status}", "payment", pid, body.note)
    return await find_payment(ctx, pid)


@router.post("/payments/{pid}/record-paid")
async def record_domestic(pid: str, ctx: dict = Depends(require_roles(*MANAGERS))):
    res = await db.payments.update_one(
        {"id": pid, "workspace_id": ctx["ws_id"], "type": "domestic", "status": "approved"},
        {
            "$set": {"status": "recorded_as_paid"},
            "$push": {
                "history": history_entry(ctx, "recorded_as_paid", "Demo record only — no bank transfer was made")
            },
        },
    )
    if not res.modified_count:
        raise HTTPException(status_code=409, detail="Only approved domestic payments can be recorded")
    await audit(ctx, "payment.recorded_demo", "payment", pid)
    return await find_payment(ctx, pid)
