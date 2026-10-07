import unittest
from unittest.mock import AsyncMock, patch

import httpx
from fastapi import HTTPException

from app.jellyfin import client as jellyfin_client
from app import security


class JellyfinAuthenticationTests(unittest.IsolatedAsyncioTestCase):
    async def test_authenticates_and_discards_jellyfin_token(self):
        response = httpx.Response(200, json={"User": {"Id": "user-1", "Name": "Vitor"}, "AccessToken": "secret-token"})
        with patch.object(jellyfin_client, "JELLYFIN_URL", "https://jf.example.test"), patch.object(jellyfin_client.httpx, "AsyncClient") as client_type:
            client = client_type.return_value.__aenter__.return_value
            client.post = AsyncMock(return_value=response)

            result = await jellyfin_client.authenticate_user("Vitor", "password-value")

        self.assertEqual(result, {"id": "user-1", "name": "Vitor", "is_admin": False})
        client.post.assert_awaited_once()
        args, kwargs = client.post.await_args
        self.assertEqual(args[0], "https://jf.example.test/Users/AuthenticateByName")
        self.assertEqual(kwargs["json"], {"Username": "Vitor", "Pw": "password-value"})
        self.assertIn("X-Emby-Authorization", kwargs["headers"])
        self.assertNotIn("secret-token", repr(result))

    async def test_invalid_credentials_return_401(self):
        response = httpx.Response(401)
        with patch.object(jellyfin_client, "JELLYFIN_URL", "https://jf.example.test"), patch.object(jellyfin_client.httpx, "AsyncClient") as client_type:
            client = client_type.return_value.__aenter__.return_value
            client.post = AsyncMock(return_value=response)

            with self.assertRaises(HTTPException) as raised:
                await jellyfin_client.authenticate_user("Vitor", "wrong")

        self.assertEqual(raised.exception.status_code, 401)

    async def test_rejects_non_https_server_url(self):
        with patch.object(jellyfin_client, "JELLYFIN_URL", "http://jf.example.test"):
            with self.assertRaises(HTTPException) as raised:
                await jellyfin_client.authenticate_user("Vitor", "password-value")
        self.assertEqual(raised.exception.status_code, 503)

    async def test_refreshes_library_with_server_side_api_key(self):
        response = httpx.Response(204)
        with patch.object(jellyfin_client, "JELLYFIN_URL", "https://jf.example.test"), patch.object(jellyfin_client, "JELLYFIN_API_KEY", "server-only-key"), patch.object(jellyfin_client.httpx, "AsyncClient") as client_type:
            client = client_type.return_value.__aenter__.return_value
            client.post = AsyncMock(return_value=response)

            await jellyfin_client.refresh_library()

        args, kwargs = client.post.await_args
        self.assertEqual(args[0], "https://jf.example.test/Library/Refresh")
        self.assertEqual(kwargs["headers"], {"X-Emby-Token": "server-only-key"})

    async def test_refresh_requires_api_key(self):
        with patch.object(jellyfin_client, "JELLYFIN_URL", "https://jf.example.test"), patch.object(jellyfin_client, "JELLYFIN_API_KEY", ""):
            with self.assertRaises(HTTPException) as raised:
                await jellyfin_client.refresh_library()
        self.assertEqual(raised.exception.status_code, 503)

    async def test_session_keeps_user_identity_and_admin_claim(self):
        user = {"id": "user-1", "name": "Vitor", "is_admin": True}
        with patch.object(security, "SESSION_SECRET", "s" * 40):
            token = security.create_session(user)
            self.assertEqual(security.session_user(token)["user_id"], "user-1")
            self.assertTrue(security.session_user(token)["is_admin"])
            self.assertIsNone(security.session_user(token + "x"))


if __name__ == "__main__":
    unittest.main()
