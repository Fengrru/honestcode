"""The corrected code after HonestCode reports the invented API."""

from app.client import UserClient


def login(client: UserClient) -> None:
    client.refresh_access_token()
