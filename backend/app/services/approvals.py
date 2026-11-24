"""Approval rules shared by expenses and payments."""

from typing import Literal, Optional

from pydantic import BaseModel, Field

from app.common import MANAGERS


class Decision(BaseModel):
    action: Literal["approve", "reject"]
    note: str = Field("", max_length=500)


def can_decide(ctx: dict, amount_php: float, requester_id: str) -> Optional[str]:
    if ctx["role"] not in MANAGERS:
        return "Only owners and finance managers can approve"
    if requester_id == ctx["member"]["id"] and ctx["role"] != "owner":
        return "You cannot approve your own request"
    if ctx["role"] == "finance_manager" and amount_php > ctx["ws"].get("approval_threshold", 50000):
        return "Amount exceeds your approval threshold; owner approval required"
    return None
