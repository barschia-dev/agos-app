"""Reports and CSV exports."""

from typing import Optional

from fastapi import APIRouter, Depends, HTTPException

from app.common import MANAGERS, csv_response
from app.dependencies import require_roles
from app.services.reporting import report_data

router = APIRouter(prefix="/api")


@router.get("/reports")
async def reports(
    ctx: dict = Depends(require_roles(*MANAGERS)), date_from: Optional[str] = None, date_to: Optional[str] = None
):
    return await report_data(ctx, date_from, date_to)


EXPORTS = {
    "monthly": ["month", "amount"],
    "departments": ["name", "amount"],
    "employees": ["name", "amount"],
    "budget_vs_actual": ["name", "period", "scope", "budget", "actual", "variance", "utilization"],
    "pending": ["id", "date", "merchant", "member_name", "amount_php"],
    "payments": ["id", "date", "type", "rail", "recipient", "amount", "currency", "status", "is_synthetic"],
    "stellar": [
        "id",
        "recipient",
        "destination",
        "amount",
        "asset",
        "status",
        "tx_hash",
        "ledger",
        "network",
        "source",
        "submitted_at",
        "confirmed_at",
        "verified_at",
        "matches_request",
        "explorer",
    ],
}


@router.get("/reports/export/{kind}")
async def export_report(
    kind: str,
    ctx: dict = Depends(require_roles(*MANAGERS)),
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
):
    if kind not in EXPORTS:
        raise HTTPException(status_code=404, detail="Unknown report")
    data = await report_data(ctx, date_from, date_to)
    return csv_response(data[kind], EXPORTS[kind], f"agos-{kind}.csv")
