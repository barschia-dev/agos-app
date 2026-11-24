"""Shared constants and small helpers."""

import csv
import io
import uuid
from datetime import UTC, datetime

from fastapi.responses import StreamingResponse

from app.database import db

USD_PHP = 58.0  # fixed demo conversion rate, not a market rate
ROLES = ["owner", "finance_manager", "employee"]
MANAGERS = ("owner", "finance_manager")
DEFAULT_DEPARTMENTS = ["Executive", "Engineering", "Marketing", "Operations", "Finance"]


def now_iso() -> str:
    return datetime.now(UTC).isoformat()


def new_id() -> str:
    return str(uuid.uuid4())


def to_php(amount: float, currency: str) -> float:
    if currency == "PHP":
        return round(amount, 2)
    if currency == "USD":
        return round(amount * USD_PHP, 2)
    return 0.0


def this_month() -> str:
    return datetime.now(UTC).strftime("%Y-%m")


def _csv_cell(value):
    """Stop spreadsheets from running user-entered text as a formula."""
    if isinstance(value, str) and value.startswith(("=", "+", "-", "@", "\t", "\r")):
        return "'" + value
    return value


def csv_response(rows: list, columns: list, filename: str) -> StreamingResponse:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(columns)
    for r in rows:
        w.writerow([_csv_cell(r.get(c, "")) for c in columns])
    return StreamingResponse(
        iter([buf.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


async def audit(ctx: dict, action: str, entity: str, entity_id: str, details: str = "") -> None:
    await db.audit_logs.insert_one(
        {
            "id": new_id(),
            "workspace_id": ctx["ws_id"],
            "actor_member_id": ctx["member"]["id"],
            "actor_name": ctx["actor_name"],
            "action": action,
            "entity": entity,
            "entity_id": entity_id,
            "details": details,
            "at": now_iso(),
        }
    )


def history_entry(ctx: dict, status: str, note: str = "") -> dict:
    return {
        "status": status,
        "actor_name": ctx["actor_name"],
        "actor_member_id": ctx["member"]["id"],
        "at": now_iso(),
        "note": note,
    }


def system_entry(status: str, note: str) -> dict:
    return {"status": status, "actor_name": "AGOS Workflow", "actor_member_id": None, "at": now_iso(), "note": note}
