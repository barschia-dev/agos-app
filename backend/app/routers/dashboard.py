"""Workspace dashboard."""

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import Response

from app import stellar
from app.common import MANAGERS, audit
from app.database import NO_ID, db
from app.dependencies import ws_ctx
from app.pitch_pdf import build_pitch_pdf
from app.services.payments import find_payment
from app.services.reporting import dashboard_data
from app.services.settlement import refresh_payment

router = APIRouter(prefix="/api")


@router.get("/dashboard")
async def dashboard(ctx: dict = Depends(ws_ctx), months: int = 6, department: Optional[str] = None):
    return await dashboard_data(ctx, months, department)


@router.get("/payments/{pid}/pitch-summary")
async def pitch_summary(
    pid: str,
    ctx: dict = Depends(ws_ctx),
    expense_id: Optional[str] = None,
    duration: Optional[int] = Query(None, ge=1, le=7200),
):
    p = await find_payment(ctx, pid)
    if p["status"] != "confirmed" or not (p.get("stellar") or {}).get("tx_hash"):
        raise HTTPException(status_code=409, detail="A pitch summary needs a payment confirmed on the Stellar ledger")
    try:
        await refresh_payment(p)
        p = await find_payment(ctx, pid)
    except Exception:
        pass  # fall back to the last stored verification
    expense = None
    if expense_id:
        scope = {} if ctx["role"] in MANAGERS else {"member_id": ctx["member"]["id"]}
        expense = await db.expenses.find_one({"id": expense_id, "workspace_id": ctx["ws_id"], **scope}, NO_ID)
    kpis = (await dashboard_data(ctx, 6, None))["kpis"]
    pdf = build_pitch_pdf(ctx["ws"], kpis, p, expense, duration, stellar.tx_links(p["stellar"]["tx_hash"])["explorer"])
    await audit(ctx, "pitch_summary.downloaded", "payment", pid)
    return Response(
        content=pdf,
        media_type="application/pdf",
        headers={"Content-Disposition": 'attachment; filename="agos-pitch-summary.pdf"'},
    )
