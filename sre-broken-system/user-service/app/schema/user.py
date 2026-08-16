"""Validated request and response contracts for the user API."""

from datetime import datetime
from pydantic import BaseModel, ConfigDict


class UserProfile(BaseModel):
    """Stable public user representation consumed by order-service."""

    model_config = ConfigDict(from_attributes=True)
    id: int
    email: str
    display_name: str
    membership_level: str
    status: str
    created_at: datetime


class MembershipSummary(BaseModel):
    """Smaller projection used when only eligibility and discount are needed."""

    user_id: int
    level: str
    discount_rate: float
