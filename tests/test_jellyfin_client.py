import unittest
from unittest.mock import AsyncMock, patch

import httpx
from fastapi import HTTPException

from app.jellyfin import client as jellyfin_client
from app import security


class JellyfinAuthenticationTests(unittest.IsolatedAsyncioTestCase):
    async def test_reads_resume_items_for_session_user_without_exposing_server_key(self):
        user_id = "a1a1a1a1111141118111111111111111"
        item = {"Id": "b2b2b2b2222242228222222222222222", "Name": "Episode", "UserData": {"PlaybackPositionTicks": 1}}
        response = httpx.Response(200, json={"Items": [item]}, request=httpx.Request("GET", "https://jf.example.test/Users/" + user_id + "/Items/Resume"))
        with patch.object(jellyfin_client, "JELLYFIN_URL", "https://jf.example.test"), patch.object(jellyfin_client, "JELLYFIN_API_KEY", "server-only-key"), patch.object(jellyfin_client.httpx, "AsyncClient") as client_type:
            client = client_type.return_value.__aenter__.return_value
            client.get = AsyncMock(return_value=response)

            items = await jellyfin_client.resume_items(user_id)

        self.assertEqual(items, [item])
        args, kwargs = client.get.await_args
        self.assertEqual(args[0], f"https://jf.example.test/Users/{user_id}/Items/Resume")
        self.assertEqual(kwargs["headers"], {"X-Emby-Token": "server-only-key"})
        self.assertEqual(kwargs["params"]["MediaTypes"], "Video")
        self.assertNotIn("server-only-key", repr(items))

    async def test_resume_rejects_invalid_user_id(self):
        with patch.object(jellyfin_client, "JELLYFIN_URL", "https://jf.example.test"), patch.object(jellyfin_client, "JELLYFIN_API_KEY", "server-only-key"):
            with self.assertRaises(HTTPException) as raised:
                await jellyfin_client.resume_items("not-a-user-id")
        self.assertEqual(raised.exception.status_code, 401)

    async def test_reads_recently_watched_movies_and_episodes_for_recommendations(self):
        user_id = "a1a1a1a1-1111-4111-8111-111111111111"
        item = {"Id": "b2b2b2b2-2222-4222-8222-222222222222", "Type": "Movie", "ProviderIds": {"Tmdb": "603"}}
        response = httpx.Response(200, json={"Items": [item]}, request=httpx.Request("GET", f"https://jf.example.test/Users/{user_id}/Items"))
        with patch.object(jellyfin_client, "JELLYFIN_URL", "https://jf.example.test"), patch.object(jellyfin_client, "JELLYFIN_API_KEY", "server-only-key"), patch.object(jellyfin_client.httpx, "AsyncClient") as client_type:
            client = client_type.return_value.__aenter__.return_value
            client.get = AsyncMock(return_value=response)

            items = await jellyfin_client.watched_media(user_id, limit=60)

        self.assertEqual(items, [item])
        args, kwargs = client.get.await_args
        self.assertEqual(args[0], f"https://jf.example.test/Users/{user_id}/Items")
        self.assertEqual(kwargs["params"]["IncludeItemTypes"], "Movie,Episode")
        self.assertEqual(kwargs["params"]["Filters"], "IsPlayed")
        self.assertEqual(kwargs["params"]["SortBy"], "DatePlayed")
        self.assertEqual(kwargs["headers"], {"X-Emby-Token": "server-only-key"})

    async def test_fetches_jellyfin_cover_with_server_key(self):
        item_id = "b2b2b2b2222242228222222222222222"
        response = httpx.Response(200, content=b"image-data", headers={"content-type": "image/webp"}, request=httpx.Request("GET", "https://jf.example.test/Items/" + item_id + "/Images/Primary"))
        with patch.object(jellyfin_client, "JELLYFIN_URL", "https://jf.example.test"), patch.object(jellyfin_client, "JELLYFIN_API_KEY", "server-only-key"), patch.object(jellyfin_client.httpx, "AsyncClient") as client_type:
            client = client_type.return_value.__aenter__.return_value
            client.get = AsyncMock(return_value=response)

            content, media_type = await jellyfin_client.item_image(item_id)

        self.assertEqual(content, b"image-data")
        self.assertEqual(media_type, "image/webp")
        self.assertEqual(client.get.await_args.kwargs["headers"], {"X-Emby-Token": "server-only-key"})

    async def test_reads_active_sessions_and_played_state_with_server_key(self):
        user_id = "a1a1a1a1-1111-4111-8111-111111111111"
        item_id = "b2b2b2b2-2222-4222-8222-222222222222"
        with patch.object(jellyfin_client, "JELLYFIN_URL", "https://jf.example.test"), patch.object(jellyfin_client, "JELLYFIN_API_KEY", "server-only-key"), patch.object(jellyfin_client.httpx, "AsyncClient") as client_type:
            client = client_type.return_value.__aenter__.return_value
            client.get = AsyncMock(side_effect=[
                httpx.Response(200, json=[{"UserId": user_id, "NowPlayingItem": {"Id": item_id, "Path": "/media/movie.mkv"}}], request=httpx.Request("GET", "https://jf.example.test/Sessions")),
                httpx.Response(200, json={"UserData": {"Played": True}}, request=httpx.Request("GET", f"https://jf.example.test/Users/{user_id}/Items/{item_id}")),
            ])

            sessions = await jellyfin_client.playback_sessions()
            played = await jellyfin_client.item_marked_played(user_id, item_id)

        self.assertEqual(sessions[0]["NowPlayingItem"]["Path"], "/media/movie.mkv")
        self.assertTrue(played)
        self.assertEqual(client.get.await_args_list[0].args[0], "https://jf.example.test/Sessions")
        self.assertEqual(client.get.await_args_list[1].args[0], f"https://jf.example.test/Users/{user_id}/Items/{item_id}")
        self.assertEqual(client.get.await_args_list[0].kwargs["headers"], {"X-Emby-Token": "server-only-key"})

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
