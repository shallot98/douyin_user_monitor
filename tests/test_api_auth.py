import os
import unittest

from fastapi.testclient import TestClient

from douyin_user_monitor.main import API_KEY_ENV, app

TEST_API_KEY = "test-secret-key"


class ApiKeyAuthTests(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(app)
        self._saved_key = os.environ.pop(API_KEY_ENV, None)

    def tearDown(self):
        if self._saved_key is None:
            os.environ.pop(API_KEY_ENV, None)
        else:
            os.environ[API_KEY_ENV] = self._saved_key

    def test_request_passes_without_api_key_configured(self):
        response = self.client.get("/api/monitor/dashboard")

        self.assertEqual(response.status_code, 200)

    def test_request_without_key_returns_401_when_configured(self):
        os.environ[API_KEY_ENV] = TEST_API_KEY

        response = self.client.get("/api/monitor/dashboard")

        self.assertEqual(response.status_code, 401)

    def test_request_with_correct_header_passes(self):
        os.environ[API_KEY_ENV] = TEST_API_KEY

        response = self.client.get(
            "/api/monitor/dashboard",
            headers={"X-API-Key": TEST_API_KEY},
        )

        self.assertEqual(response.status_code, 200)

    def test_request_with_correct_query_param_passes(self):
        os.environ[API_KEY_ENV] = TEST_API_KEY

        response = self.client.get(
            "/api/monitor/dashboard",
            params={"api_key": TEST_API_KEY},
        )

        self.assertEqual(response.status_code, 200)


if __name__ == "__main__":
    unittest.main()
