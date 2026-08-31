"""Real UserClient — the source of truth for the demo."""


class UserClient:
    """A minimal API client. Notice: there is no ``refresh_token`` method."""

    def refresh(self) -> str:
        return "session-refreshed"

    def refresh_access_token(self) -> str:
        return "new-access-token"
