import os
import unittest
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

from douyin_user_monitor.api import monitor as monitor_api
from douyin_user_monitor.main import API_KEY_ENV, app

VIDEO = {
    "aweme_id": "7001", "is_image_post": False, "desc": "标题", "download_url": "https://cdn.test/v.mp4",
    "quality_source": "original",
}


class ParsePluginRouteTests(unittest.TestCase):
    def setUp(self):
        env = patch.dict(os.environ, {API_KEY_ENV: "k"})
        env.start()
        self.addCleanup(env.stop)
        self.client = TestClient(app)

    def _get(self, result=None, error=None, **params):
        mock = AsyncMock(return_value=result, side_effect=error)
        with patch.object(monitor_api.PARSE_SERVICE, "parse", mock):
            response = self.client.get("/api/monitor/parse/plugin", params={"api_key": "k", **params})
        return response, mock

    def test_video_returns_plugin_single_format(self):
        response, mock = self._get(VIDEO, url="https://v.douyin.com/abc/")
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body["code"], 200)
        self.assertEqual(body["data"]["url"], "https://cdn.test/v.mp4")
        self.assertEqual(body["data"]["video_url"], "https://cdn.test/v.mp4")
        self.assertNotIn("video_list", body["data"])
        mock.assert_awaited_once_with("https://v.douyin.com/abc/")

    def test_image_post_is_an_error(self):
        response, _ = self._get({**VIDEO, "is_image_post": True, "download_url": "https://cdn.test/1.jpg"}, url="x")
        self.assertEqual(response.status_code, 400)
        self.assertNotEqual(response.json().get("code"), 200)

    def test_parse_failure_is_an_error(self):
        response, _ = self._get(error=ValueError("未找到有效抖音链接"), url="x")
        self.assertEqual(response.status_code, 400)
        self.assertIn("未找到有效抖音链接", response.text)

    def test_requires_api_key(self):
        response = self.client.get("/api/monitor/parse/plugin", params={"url": "x"})
        self.assertEqual(response.status_code, 401)


if __name__ == "__main__":
    unittest.main()
