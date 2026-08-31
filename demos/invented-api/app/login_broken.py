"""Agent-generated code that invents an API."""

from app.client import UserClient


def login(client: UserClient) -> None:
    # Agent hallucinated this method. UserClient only has refresh() and
    # refresh_access_token().
    client.refresh_token()
