import json
import re
import shutil
import subprocess
import unittest
from pathlib import Path

WEB_DIR = Path(__file__).resolve().parents[1] / "douyin_user_monitor" / "web"
DASHBOARD_HTML = WEB_DIR / "dashboard.html"
STATISTICS_HTML = WEB_DIR / "statistics.html"
# jsAttr 已抽取到公共脚本，供三页共享。
COMMON_JS = WEB_DIR / "static" / "common.js"

# jsAttr 定义（common.js 中顶层函数，闭合花括号在行首；函数体内无嵌套花括号）
_JSATTR_RE = re.compile(r"function jsAttr\(value\)\s*\{.*?\n\}", re.DOTALL)

# onclick 数据注入点必须用 jsAttr(...) 包裹，禁止裸 escapeHtml / 直接插值
MALICIOUS_INPUTS = [
    "');alert(1)//",
    "'-alert(document.cookie)-'",
    "\\');alert(1)//",
    "');alert(String.fromCharCode(88))//",
    "<script>alert(1)</script>",
    "\"><img src=x onerror=alert(1)>",
    "a & b \" c ' d < e > f",
    "line1\nline2\r\ttab",
    "https://www.douyin.com/user/abc?x=1&y=2",
    "普通昵称_no_injection",
]


def _extract_jsattr() -> str:
    text = COMMON_JS.read_text(encoding="utf-8")
    match = _JSATTR_RE.search(text)
    if match is None:
        raise AssertionError("未能在 common.js 中定位 jsAttr 函数定义")
    return match.group(0)


# node 脚本：定义 jsAttr → 放入 HTML 双引号属性内的 JS 单引号字符串 →
# 浏览器 HTML 属性解码 → 解析 JS 字符串字面量，验证还原出原始输入且无越权执行。
_NODE_HARNESS = r"""
%(jsattr)s

function htmlAttrDecode(s) {
  // 模拟浏览器对 HTML 属性值的实体解码（&amp; 最后解，避免二次解码）
  return String(s)
    .replaceAll("&lt;", "<")
    .replaceAll("&gt;", ">")
    .replaceAll("&quot;", '"')
    .replaceAll("&#39;", "'")
    .replaceAll("&amp;", "&");
}

const inputs = %(inputs)s;
for (const input of inputs) {
  const attr = jsAttr(input);
  // 原始 HTML 属性用双引号包裹：编码后不得残留可越权的 " < >
  if (/["<>]/.test(attr)) {
    console.log("FAIL RAW_BREAK " + JSON.stringify(attr));
    process.exit(1);
  }
  const jsSource = htmlAttrDecode(attr); // 浏览器交给 JS 的单引号字符串内容
  let injected = false;
  const alert = () => { injected = true; };
  const document = { cookie: "" };
  const String_ = String;
  let recovered;
  try {
    // 只对「单引号字符串字面量」求值；若转义不严，闭合引号会执行注入代码或抛语法错误
    recovered = eval("(function(){ return '" + jsSource + "'; })()");
  } catch (e) {
    console.log("FAIL SYNTAX_BREAK " + JSON.stringify(input) + " :: " + e.message);
    process.exit(1);
  }
  if (injected) {
    console.log("FAIL INJECTED " + JSON.stringify(input));
    process.exit(1);
  }
  if (recovered !== input) {
    console.log("FAIL MISMATCH " + JSON.stringify(input) + " => " + JSON.stringify(recovered));
    process.exit(1);
  }
}
console.log("OK");
"""


class JsAttrEscapingTests(unittest.TestCase):
    def _run_node_harness(self) -> None:
        script = _NODE_HARNESS % {
            "jsattr": _extract_jsattr(),
            "inputs": json.dumps(MALICIOUS_INPUTS),
        }
        proc = subprocess.run(
            [shutil.which("node"), "-e", script],
            capture_output=True,
            text=True,
            timeout=30,
        )
        self.assertEqual(
            proc.returncode,
            0,
            msg=f"common.js jsAttr 未能安全转义: {proc.stdout.strip()} {proc.stderr.strip()}",
        )
        self.assertIn("OK", proc.stdout)

    @unittest.skipUnless(shutil.which("node"), "node not available")
    def test_shared_jsattr_neutralizes_injection(self):
        # jsAttr 现为三页共享（common.js），一次校验即覆盖 dashboard/statistics 两页。
        self._run_node_harness()

    def test_both_pages_load_common_js(self):
        # 两页都必须引入 common.js，才能拿到共享的 jsAttr 安全实现。
        for html_path in (DASHBOARD_HTML, STATISTICS_HTML):
            text = html_path.read_text(encoding="utf-8")
            self.assertIn('src="/static/common.js"', text, f"{html_path.name} 未引入 common.js")


class JsAttrUsageStructureTests(unittest.TestCase):
    """纯结构断言（不依赖 node）：onclick 数据注入点必须用 jsAttr，禁止改回不安全写法。"""

    def test_dashboard_copyuser_onclick_uses_jsattr(self):
        text = DASHBOARD_HTML.read_text(encoding="utf-8")
        onclick_lines = [
            line for line in text.splitlines() if "copyUserProfile(" in line and "onclick" in line
        ]
        self.assertTrue(onclick_lines, "dashboard.html 未找到 copyUserProfile 的 onclick 注入点")
        for line in onclick_lines:
            self.assertIn("jsAttr(", line)
            self.assertNotIn("escapeHtml(", line)

    def test_statistics_copyuser_onclick_uses_jsattr(self):
        text = STATISTICS_HTML.read_text(encoding="utf-8")
        # profile_url / nickname 经 jsAttr 处理后再插入 copyUserProfile onclick
        self.assertRegex(text, r"jsAttr\(user\.profile_url")
        self.assertRegex(text, r"jsAttr\(user\.nickname")
        onclick_lines = [
            line for line in text.splitlines() if "copyUserProfile(" in line and "onclick" in line
        ]
        self.assertTrue(onclick_lines, "statistics.html 未找到 copyUserProfile 的 onclick 注入点")
        for line in onclick_lines:
            self.assertNotIn("escapeHtml(", line)

    def test_common_js_defines_jsattr_with_js_string_escaping(self):
        # jsAttr 必须包含 JS 字符串转义（反斜杠与单引号），区别于纯 escapeHtml
        body = _extract_jsattr()
        self.assertIn(r'"\\", "\\\\"', body, "common.js jsAttr 缺少反斜杠转义")
        self.assertIn(r'''"'", "\\'"''', body, "common.js jsAttr 缺少单引号 JS 转义")


if __name__ == "__main__":
    unittest.main()
