class UserClient:
    def __init__(self, token: str) -> None:
        self.token = token

    def refresh_access_token(self) -> str:
        return "new_token"
