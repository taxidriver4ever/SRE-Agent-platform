"""User use cases and realistic CPU/event-loop failure mechanisms."""

from dataclasses import dataclass

from app.repository.user_repository import UserRepository
from app.schema.user import MembershipSummary, UserProfile


class UserNotFoundError(LookupError):
    """Domain error translated to HTTP 404 by the API layer."""


@dataclass(slots=True)
class UserService:
    """Coordinate repository results and membership business rules."""

    repository: UserRepository

    def profile(self, user_id: int) -> UserProfile:
        user = self.repository.find_by_id(user_id)
        if user is None:
            raise UserNotFoundError(f"user {user_id} not found")
        return UserProfile.model_validate(user)

    def membership(self, user_id: int) -> MembershipSummary:
        profile = self.profile(user_id)
        discounts = {"STANDARD": 0.0, "SILVER": 0.03, "GOLD": 0.08, "PLATINUM": 0.12}
        return MembershipSummary(user_id=user_id, level=profile.membership_level,
                                 discount_rate=discounts.get(profile.membership_level, 0.0))

    def list_users(self, after_id: int, limit: int) -> list[UserProfile]:
        bounded_limit = min(max(limit, 1), 100)
        return [UserProfile.model_validate(user) for user in self.repository.list_after(after_id, bounded_limit)]
