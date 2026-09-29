import re
import unittest

from fastapi.testclient import TestClient

from douyin_user_monitor.main import app

# 允许 cache-bust 查询串：/static/common.js 或 /static/common.js?v=...
_COMMON_JS_SRC_RE = re.compile(r'src="/static/common\.js(?:\?[^"]*)?"')


class DashboardUiTests(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(app)

    def _common_js_text(self) -> str:
        # 公共 JS（API_BASE / SHANGHAI 时间格式化 / jsAttr 等）已抽到 /static/common.js。
        resp = self.client.get('/static/common.js')
        self.assertEqual(resp.status_code, 200)
        return resp.text

    def _assert_loads_common_js(self, html: str) -> None:
        self.assertRegex(html, _COMMON_JS_SRC_RE, '页面未引入 common.js')

    def test_dashboard_drops_operable_user_list_and_links_to_statistics(self):
        response = self.client.get('/api/monitor/dashboard')

        self.assertEqual(response.status_code, 200)
        html = response.text
        # 可操作监控用户列表已迁到统计统一表
        self.assertNotIn('id="userSearchInput"', html)
        self.assertNotIn('id="users"', html)
        self.assertNotIn('function renderUsers(', html)
        self.assertNotIn('监控用户列表', html)
        self.assertNotIn('id="profileUrl"', html)
        self.assertNotIn('onclick="addUser()"', html)
        self.assertNotIn('用户管理入口', html)
        # 页内「用户管理」按钮已删，导航统一收到侧栏；入口用链接断言，不锚文案
        self.assertIn('/api/monitor/statistics/dashboard', html)
        self.assertIn('下载动态', html)
        self.assertIn('内容结构总览', html)
        self.assertIn('活跃与风险洞察', html)
        self.assertIn('api("/recent_activity")', html)
        self.assertIn('api("/statistics")', html)
        self.assertNotIn('api("/users")', html)
        self.assertIn('function renderCoverage(', html)
        self.assertIn('function renderActivityInsights(', html)

    def test_dashboard_uses_shanghai_timezone_formatter(self):
        response = self.client.get('/api/monitor/dashboard')

        self.assertEqual(response.status_code, 200)
        html = response.text
        self._assert_loads_common_js(html)
        # 下载动态走服务端 recent_activity，页面只调用 formatTime 展示时间
        self.assertIn("formatTime(record.downloaded_at)", html)
        common_js = self._common_js_text()
        self.assertIn('const SHANGHAI_TIME_ZONE = "Asia/Shanghai";', common_js)
        self.assertIn("function parseTimeToMillis(", common_js)
        self.assertIn("function formatTime(", common_js)

    def test_dashboard_has_no_platform_switcher(self):
        response = self.client.get('/api/monitor/dashboard')

        self.assertEqual(response.status_code, 200)
        html = response.text
        self.assertIn('id="sidebarTitle"', html)
        self.assertIn('let refreshSeq = 0;', html)
        self.assertIn('if (seq !== refreshSeq) return;', html)
        self._assert_loads_common_js(html)
        self.assertIn('const API_BASE = "/api/monitor";', self._common_js_text())
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
        self._assert_loads_common_js(html)
        self.assertIn('const API_BASE = "/api/monitor";', self._common_js_text())
        self.assertIn('Cookie 池', html)
        self.assertIn('/api/monitor/cookies/dashboard', html)
        self.assertNotIn("function switchPlatform(platform)", html)
        self.assertNotIn('class="platform-tab', html)
        self.assertNotIn(">抖音</button>", html)

    def test_statistics_summary_uses_dark_theme_surface(self):
        response = self.client.get('/api/monitor/statistics/dashboard')

        self.assertEqual(response.status_code, 200)
        html = response.text
        self.assertIn('body.theme-dark .summary-bar', html)
        self.assertIn('background: #172033;', html)
        self.assertIn('-webkit-backdrop-filter: none;', html)
        self.assertIn('backdrop-filter: none;', html)
        self.assertIn('/static/common.css?v=theme-20260921b', html)
        self.assertIn('UI theme-20260921b', html)

    def test_cookies_dashboard_has_pool_controls(self):
        response = self.client.get('/api/monitor/cookies/dashboard')

        self.assertEqual(response.status_code, 200)
        html = response.text
        self.assertIn('id="cookieList"', html)
        self.assertIn('id="cookieValue"', html)
        self.assertIn('id="btnAddCookie"', html)
        self.assertIn('function checkCookie(', html)
        self.assertIn('function activateCookie(', html)
        self.assertIn('${active ? "已启用" : "设为启用"}', html)
        self.assertIn('let refreshSeq = 0;', html)
        self.assertIn('if (seq !== refreshSeq) return;', html)
        self.assertIn('Cookie 池', html)
        self._assert_loads_common_js(html)
        self.assertIn('const API_BASE = "/api/monitor";', self._common_js_text())
        # 遮罩/侧栏交互样式已抽到 common.css，遮罩不得盖住侧栏
        common_css = self.client.get('/static/common.css').text
        self.assertIn('pointer-events: none', common_css)
        self.assertIn('.sidebar.show', common_css)
        self.assertNotIn('.sidebar.open', html)
        self.assertNotIn('.sidebar.open', common_css)

    def test_common_api_requests_bypass_browser_cache(self):
        common_js = self._common_js_text()
        self.assertIn('cache: "no-store"', common_js)

    def test_statistics_dashboard_uses_shanghai_timezone_formatter(self):
        response = self.client.get('/api/monitor/statistics/dashboard')

        self.assertEqual(response.status_code, 200)
        html = response.text
        self._assert_loads_common_js(html)
        self.assertIn("生成时间（上海）", html)
        common_js = self._common_js_text()
        self.assertIn('const SHANGHAI_TIME_ZONE = "Asia/Shanghai";', common_js)
        self.assertIn('hourCycle: "h23"', common_js)
        # 账号状态字段在统一用户表行里展示（label）；reason/updated_at 由 API 投影，页面不再硬编码字段名
        self.assertIn("account_status", html)
        self.assertIn("account_status_label", html)
        self.assertIn("toggleUser(userId, enabled)", html)
        # 统一用户表承接原 preview 列表能力
        self.assertIn("monitor-users", html)
        self.assertIn("function toggleUser(", html)
        # 内容结构 / 活跃洞察已迁到数据大盘
        self.assertNotIn("内容结构总览", html)
        self.assertNotIn("活跃与风险洞察", html)
        self.assertNotIn("function renderCoverage(", html)
        self.assertIn("function renderSummary(", html)


if __name__ == '__main__':
    unittest.main()
