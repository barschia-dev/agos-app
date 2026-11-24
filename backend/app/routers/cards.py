"""Simulated corporate cards."""

import secrets
from datetime import UTC, datetime
from typing import Literal, Optional

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from app.common import (
    MANAGERS,
    audit,
    new_id,
    now_iso,
    system_entry,
    this_month,
)
from app.database import NO_ID, db
from app.dependencies import require_roles, ws_ctx

router = APIRouter(prefix="/api")


class CardIn(BaseModel):
    member_id: str
    nickname: str = Field(min_length=2, max_length=60)
    spending_limit: float = Field(gt=0, le=10_000_000)
    budget_id: Optional[str] = None


class CardPatch(BaseModel):
    nickname: Optional[str] = Field(None, min_length=2, max_length=60)
    spending_limit: Optional[float] = Field(None, gt=0, le=10_000_000)


class SimTxnIn(BaseModel):
    merchant: str = Field(min_length=1, max_length=120)
    category: str = Field(min_length=1, max_length=60)
    amount: float = Field(gt=0, le=10_000_000)


async def _card_spend(card_id: str) -> float:
    month = this_month()
    txns = await db.card_transactions.find(
        {"card_id": card_id, "status": "posted", "date": {"$regex": "^" + month}}, {"_id": 0, "amount_php": 1}
    ).to_list(5000)
    return round(sum(t["amount_php"] for t in txns), 2)


async def _get_card(ctx, card_id) -> dict:
    scope = {} if ctx["role"] in MANAGERS else {"member_id": ctx["member"]["id"]}
    c = await db.cards.find_one({"id": card_id, "workspace_id": ctx["ws_id"], **scope}, NO_ID)
    if not c:
        raise HTTPException(status_code=404, detail="Not found")
    c["spent_this_month"] = await _card_spend(card_id)
    c["available"] = round(c["spending_limit"] - c["spent_this_month"], 2)
    return c


@router.get("/cards")
async def list_cards(ctx: dict = Depends(ws_ctx)):
    scope = {} if ctx["role"] in MANAGERS else {"member_id": ctx["member"]["id"]}
    ids = await db.cards.distinct("id", {"workspace_id": ctx["ws_id"], **scope})
    return [await _get_card(ctx, i) for i in ids]


@router.post("/cards")
async def create_card(body: CardIn, ctx: dict = Depends(require_roles(*MANAGERS))):
    m = await db.members.find_one(
        {"id": body.member_id, "workspace_id": ctx["ws_id"], "status": {"$ne": "removed"}}, NO_ID
    )
    if not m:
        raise HTTPException(status_code=404, detail="Member not found")
    if body.budget_id and not await db.budgets.find_one({"id": body.budget_id, "workspace_id": ctx["ws_id"]}):
        raise HTTPException(status_code=404, detail="Budget not found")
    card = {
        "id": new_id(),
        "workspace_id": ctx["ws_id"],
        "member_id": m["id"],
        "member_name": m["name"],
        "nickname": body.nickname,
        "last4": f"{secrets.randbelow(10000):04d}",
        "spending_limit": body.spending_limit,
        "status": "active",
        "budget_id": body.budget_id,
        "expiry": "12/28",
        "is_simulated": True,
        "created_at": now_iso(),
    }
    await db.cards.insert_one(dict(card))
    await audit(ctx, "card.issued_simulated", "card", card["id"], f"{body.nickname} for {m['name']}")
    return await _get_card(ctx, card["id"])


@router.patch("/cards/{card_id}")
async def patch_card(card_id: str, body: CardPatch, ctx: dict = Depends(require_roles(*MANAGERS))):
    await _get_card(ctx, card_id)
    await db.cards.update_one({"id": card_id}, {"$set": body.model_dump(exclude_none=True)})
    await audit(ctx, "card.updated", "card", card_id, str(body.model_dump(exclude_none=True)))
    return await _get_card(ctx, card_id)


class CardStatusIn(BaseModel):
    action: Literal["freeze", "unfreeze"]


@router.post("/cards/{card_id}/status")
async def card_action(card_id: str, body: CardStatusIn, ctx: dict = Depends(ws_ctx)):
    action = body.action
    await _get_card(ctx, card_id)
    if action == "unfreeze" and ctx["role"] not in MANAGERS:
        raise HTTPException(status_code=403, detail="Only managers can unfreeze cards")
    await db.cards.update_one({"id": card_id}, {"$set": {"status": "frozen" if action == "freeze" else "active"}})
    await audit(ctx, f"card.{action}", "card", card_id)
    return await _get_card(ctx, card_id)


@router.get("/cards/{card_id}/transactions")
async def card_transactions(card_id: str, ctx: dict = Depends(ws_ctx)):
    await _get_card(ctx, card_id)
    return await db.card_transactions.find({"card_id": card_id}, NO_ID).sort("date", -1).to_list(500)


@router.post("/cards/{card_id}/simulate")
async def simulate_txn(card_id: str, body: SimTxnIn, ctx: dict = Depends(ws_ctx)):
    card = await _get_card(ctx, card_id)
    reason = None
    if card["status"] == "frozen":
        reason = "Card is frozen"
    elif body.amount > card["available"]:
        reason = f"Exceeds remaining card limit (₱{card['available']:,.2f} available)"
    txn = {
        "id": new_id(),
        "workspace_id": ctx["ws_id"],
        "card_id": card_id,
        "member_id": card["member_id"],
        "merchant": body.merchant,
        "category": body.category,
        "amount": body.amount,
        "currency": "PHP",
        "amount_php": body.amount,
        "date": datetime.now(UTC).date().isoformat(),
        "status": "declined" if reason else "posted",
        "decline_reason": reason,
        "expense_id": None,
        "is_simulated": True,
        "created_at": now_iso(),
    }
    if not reason:
        member = await db.members.find_one({"id": card["member_id"]}, NO_ID)
        exp = {
            "id": new_id(),
            "workspace_id": ctx["ws_id"],
            "member_id": member["id"],
            "member_name": member["name"],
            "department": member["department"],
            "merchant": body.merchant,
            "category": body.category,
            "amount": body.amount,
            "currency": "PHP",
            "amount_php": body.amount,
            "date": txn["date"],
            "description": f"Simulated card spend — {card['nickname']}",
            "notes": "",
            "status": "pending_approval",
            "receipts": [],
            "card_id": card_id,
            "card_txn_id": txn["id"],
            "history": [system_entry("pending_approval", "Created from simulated card transaction")],
            "reconciled": False,
            "is_synthetic": True,
            "created_by": ctx["member"]["id"],
            "created_at": now_iso(),
        }
        await db.expenses.insert_one(exp)
        txn["expense_id"] = exp["id"]
    await db.card_transactions.insert_one(dict(txn))
    await audit(ctx, "card.simulated_txn", "card", card_id, f"{body.merchant} ₱{body.amount:,.2f} {txn['status']}")
    txn.pop("_id", None)
    return txn
