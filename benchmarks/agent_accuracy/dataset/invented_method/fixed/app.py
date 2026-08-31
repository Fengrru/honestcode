from lib.client import UserClient

client = UserClient("abc")
new_token = client.refresh_access_token()
