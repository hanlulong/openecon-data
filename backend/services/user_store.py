from __future__ import annotations

from typing import Dict, List, Optional

from ..models import User, UserQueryHistory


class UserStore:
    """In-memory store for users and their query history."""

    def __init__(self) -> None:
        self._users: Dict[str, User] = {}
        self._users_by_email: Dict[str, User] = {}
        self._history: Dict[str, List[UserQueryHistory]] = {}

    def create_user(self, user: User) -> None:
        self._users[user.id] = user
        self._users_by_email[user.email.lower()] = user

    def get_user_by_id(self, user_id: str) -> Optional[User]:
        return self._users.get(user_id)

    def get_user_by_email(self, email: str) -> Optional[User]:
        return self._users_by_email.get(email.lower())

    def update_user(self, user_id: str, **updates) -> Optional[User]:
        user = self._users.get(user_id)
        if not user:
            return None

        updated_user = user.copy(update=updates)
        self._users[user_id] = updated_user

        if "email" in updates and updates["email"] != user.email:
            self._users_by_email.pop(user.email.lower(), None)
            self._users_by_email[updated_user.email.lower()] = updated_user

        return updated_user

    def add_query_to_history(self, history_item: UserQueryHistory) -> None:
        history = self._history.setdefault(history_item.userId, [])
        history.append(history_item)

    def get_user_history(self, user_id: str, limit: Optional[int] = None) -> List[UserQueryHistory]:
        history = self._history.get(user_id, [])
        sorted_history = sorted(history, key=lambda h: h.timestamp, reverse=True)
        return sorted_history[:limit] if limit is not None else sorted_history

    def get_stats(self) -> Dict[str, int]:
        total_queries = sum(len(items) for items in self._history.values())
        return {
            "totalUsers": len(self._users),
            "totalQueries": total_queries,
        }

    def clear(self) -> None:
        self._users.clear()
        self._users_by_email.clear()
        self._history.clear()


user_store = UserStore()

