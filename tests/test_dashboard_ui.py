import unittest

from fastapi.testclient import TestClient

from douyin_user_monitor.main import app


class DashboardUiTests(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(app)

    def test_dashboard_replaces_add_user_ui_with_search_ui(self):
        response = self.client.get('/api/monitor/dashboard')

        self.assertEqual(response.status_code, 200)
        html = response.text
        self.assertIn('id="userSearchInput"', html)
        self.assertIn('placeholder="搜索监控用户..."', html)
        self.assertNotIn('id="profileUrl"', html)
        self.assertNotIn('onclick="addUser()"', html)
        self.assertNotIn('粘贴抖音用户主页链接', html)

    def test_dashboard_uses_shanghai_timezone_formatter(self):
        response = self.client.get('/api/monitor/dashboard')

        self.assertEqual(response.status_code, 200)
        html = response.text
        self.assertIn('const SHANGHAI_TIME_ZONE = "Asia/Shanghai";', html)
        self.assertIn("formatTime(user.last_checked_at, '未检查')", html)
        self.assertIn("parseTimeToMillis(b.downloaded_at)", html)

    def test_dashboard_has_no_platform_switcher(self):
        response = self.client.get('/api/monitor/dashboard')

        self.assertEqual(response.status_code, 200)
        html = response.text
        self.assertIn('id="sidebarTitle"', html)
        self.assertIn('let refreshSeq = 0;', html)
        self.assertIn('if (seq !== refreshSeq) return;', html)
        self.assertIn('const API_BASE = "/api/monitor";', html)
        self.assertIn('Cookie 池', html)
        self.assertIn('/api/monitor/cookies/dashboard', html)
        self.assertNotIn("function switchPlatform(platform)", html)
        self.assertNotIn("function updatePlatformTabs(platform)", html)
        self.assertNotIn('class="platform-tab', html)
        self.assertNotIn(">抖音</button>", html)

    def test_statistics_has_no_platform_switcher(self):
        response = self.client.get('/api/monitor/statistics/dashboard')

        self.assertEqual(response.status_code, 200)
        html = response.text
        self.assertIn('id="sidebarTitle"', html)
        self.assertIn('let refreshSeq = 0;', html)
        self.assertIn('if (seq !== refreshSeq) return;', html)
        self.assertIn('const API_BASE = "/api/monitor";', html)
        self.assertIn('Cookie 池', html)
        self.assertIn('/api/monitor/cookies/dashboard', html)
        self.assertNotIn("function switchPlatform(platform)", html)
        self.assertNotIn('class="platform-tab', html)
        self.assertNotIn(">抖音</button>", html)

    def test_cookies_dashboard_has_pool_controls(self):
        response = self.client.get('/api/monitor/cookies/dashboard')

        self.assertEqual(response.status_code, 200)
        html = response.text
        self.assertIn('id="cookieList"', html)
        self.assertIn('id="cookieValue"', html)
        self.assertIn('id="btnAddCookie"', html)
        self.assertIn('function checkCookie(', html)
        self.assertIn('function activateCookie(', html)
        self.assertIn('Cookie 池', html)
        self.assertIn('const API_BASE = "/api/monitor";', html)

    def test_statistics_dashboard_uses_shanghai_timezone_formatter(self):
        response = self.client.get('/api/monitor/statistics/dashboard')

        self.assertEqual(response.status_code, 200)
        html = response.text
        self.assertIn('const SHANGHAI_TIME_ZONE = "Asia/Shanghai";', html)
        self.assertIn("统计生成时间（上海）", html)
        self.assertIn('hourCycle: "h23"', html)
        self.assertIn('id="deactivatedUsers"', html)
        self.assertIn("已注销 / 已封禁用户", html)
        self.assertIn("account_status_reason", html)
        self.assertIn("account_status_updated_at", html)
        self.assertIn("toggleUser(userId, enabled)", html)


if __name__ == '__main__':
    unittest.main()
