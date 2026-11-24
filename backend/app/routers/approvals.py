"""Approval inbox."""

from fastapi import APIRouter, Depends

from app.common import MANAGERS
from app.database import NO_ID, db
from app.dependencies import ws_ctx
from app.services.approvals import can_decide
from app.services.expenses import own_expenses

router = APIRouter(prefix="/api")


@router.get("/approvals")
async def approvals(ctx: dict = Depends(ws_ctx)):
    scope_e = own_expenses(ctx)
    scope_p = {} if ctx["role"] in MANAGERS else {"requested_by": ctx["member"]["id"]}
    pend_e = (
        await db.expenses.find({"workspace_id": ctx["ws_id"], "status": "pending_approval", **scope_e}, NO_ID)
        .sort("date", -1)
        .to_list(500)
    )
    pend_p = (
        await db.payments.find({"workspace_id": ctx["ws_id"], "status": "pending_approval", **scope_p}, NO_ID)
        .sort("created_at", -1)
        .to_list(500)
    )
    items = []
    for e in pend_e:
        items.append(
            {
                "kind": "expense",
                "id": e["id"],
                "title": e["merchant"],
                "requester": e["member_name"],
                "requester_id": e["member_id"],
                "department": e["department"],
                "amount": e["amount"],
                "currency": e["currency"],
                "amount_php": e["amount_php"],
                "date": e["date"],
                "is_synthetic": e.get("is_synthetic"),
                "blocked_reason": can_decide(ctx, e["amount_php"], e["member_id"]),
            }
        )
    for p in pend_p:
        items.append(
            {
                "kind": "payment",
                "id": p["id"],
                "title": p["recipient_name"],
                "requester": p["requested_by_name"],
                "requester_id": p["requested_by"],
                "department": p["type"].title() + " payment",
                "amount": p["amount"],
                "currency": p["currency"],
                "amount_php": p["amount_php"],
                "date": p["created_at"][:10],
                "rail": p["rail"],
                "is_synthetic": p.get("is_synthetic"),
                "blocked_reason": can_decide(ctx, p["amount_php"], p["requested_by"]),
            }
        )
    history = []
    for coll, kind, title_key in ((db.expenses, "expense", "merchant"), (db.payments, "payment", "recipient_name")):
        sc = scope_e if kind == "expense" else scope_p
        docs = await coll.find(
            {"workspace_id": ctx["ws_id"], "history.status": {"$in": ["approved", "rejected"]}, **sc},
            {"_id": 0, "id": 1, title_key: 1, "amount": 1, "currency": 1, "history": 1},
        ).to_list(1000)
        for d in docs:
            for h in d["history"]:
                if h["status"] in ("approved", "rejected"):
                    history.append(
                        {
                            "kind": kind,
                            "id": d["id"],
                            "title": d[title_key],
                            "amount": d["amount"],
                            "currency": d["currency"],
                            **h,
                        }
                    )
    history.sort(key=lambda h: h["at"], reverse=True)
    return {"pending": items, "history": history[:100], "threshold": ctx["ws"].get("approval_threshold", 50000)}
