"""Public waitlist."""

from typing import Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, EmailStr, Field

from app.common import new_id, now_iso
from app.database import db

router = APIRouter(prefix="/api")


class WaitlistIn(BaseModel):
    name: str = Field(min_length=2, max_length=80)
    business_name: str = Field(min_length=2, max_length=120)
    email: EmailStr
    category: str = Field(min_length=2, max_length=60)
    challenge: str = Field(min_length=3, max_length=1000)
    consent: bool
    website: Optional[str] = Field(None, max_length=200)  # honeypot


@router.post("/waitlist")
async def join_waitlist(body: WaitlistIn):
    if not body.consent:
        raise HTTPException(status_code=422, detail="Consent is required to join the waitlist")
    if body.website:
        return {"ok": True}
    email = body.email.lower()
    if await db.waitlist.find_one({"email": email}):
        return {"ok": True, "already": True}
    await db.waitlist.insert_one(
        {
            "id": new_id(),
            **body.model_dump(exclude={"website", "email"}),
            "email": email,
            "status": "new",
            "notes": "",
            "created_at": now_iso(),
        }
    )
    return {"ok": True}
