"""Expense lookup and filters."""

from fastapi import HTTPException

from app.common import MANAGERS
from app.database import NO_ID, db


def own_expenses(ctx: dict) -> dict:
    return {} if ctx["role"] in MANAGERS else {"member_id": ctx["member"]["id"]}


async def find_expense(ctx: dict, exp_id: str) -> dict:
    e = await db.expenses.find_one({"id": exp_id, "workspace_id": ctx["ws_id"], **own_expenses(ctx)}, NO_ID)
    if not e:
        raise HTTPException(status_code=404, detail="Not found")
    return e
