import os
import unittest
from unittest.mock import Mock, patch

from studio.oauth import ensure_tiktok_access


class OAuthRefreshTests(unittest.TestCase):
    def store(self):
        store = Mock()
        store.data = {"oauth": {}}
        return store

    def test_review_mode_never_touches_tiktok(self):
        store = self.store()
        with patch.dict(os.environ, {"STUDIO_PUBLISHER": "review", "STUDIO_TARGETS": "tiktok"}, clear=False):
            self.assertEqual(ensure_tiktok_access(store), "")
        store.save.assert_not_called()

    def test_rotated_refresh_token_is_checkpointed(self):
        store = self.store()
        response = Mock()
        response.ok = True
        response.json.return_value = {"access_token": "new-access", "refresh_token": "new-refresh",
                                      "expires_in": 86400, "refresh_expires_in": 31536000,
                                      "scope": "video.publish", "token_type": "Bearer"}
        env = {"STUDIO_PUBLISHER": "direct", "STUDIO_TARGETS": "tiktok",
               "TIKTOK_ACCESS_TOKEN": "old-access", "TIKTOK_REFRESH_TOKEN": "old-refresh",
               "TIKTOK_CLIENT_KEY": "key", "TIKTOK_CLIENT_SECRET": "secret"}
        with patch.dict(os.environ, env, clear=False), patch("studio.oauth.requests.post", return_value=response):
            token = ensure_tiktok_access(store)
        self.assertEqual(token, "new-access")
        self.assertEqual(store.data["oauth"]["tiktok"]["refresh_token"], "new-refresh")
        store.save.assert_called_once()

    def test_unexpired_encrypted_token_is_reused(self):
        import time
        store = self.store()
        store.data["oauth"]["tiktok"] = {"access_token": "cached", "refresh_token": "refresh",
                                                  "expires_at": time.time() + 7200}
        env = {"STUDIO_PUBLISHER": "direct", "STUDIO_TARGETS": "tiktok",
               "TIKTOK_CLIENT_KEY": "key", "TIKTOK_CLIENT_SECRET": "secret"}
        with patch.dict(os.environ, env, clear=False), patch("studio.oauth.requests.post") as post:
            self.assertEqual(ensure_tiktok_access(store), "cached")
        post.assert_not_called()
        store.save.assert_not_called()


if __name__ == "__main__":
    unittest.main()
