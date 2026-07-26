// 三页共享前端逻辑（dashboard / statistics / cookies）。
// 加载顺序：本文件必须在各页依赖它的内联脚本之前引入。
// 说明：
//  - isMobileViewport 各页断点不同（cookies 900 / 其余 1024），仍留在各页内联脚本，
//    本文件的侧栏函数在“调用时”解析全局 isMobileViewport。
//  - showToast / escapeHtml / Icons / renderUserAvatar 各页实现不一致，未抽取，保留在各页。

const API_BASE = "/api/monitor";

// --- 侧栏（移动端抽屉）交互 ---
function closeSidebarMobile() {
  document.getElementById("sidebar").classList.remove("show");
  document.getElementById("sidebarOverlay").classList.remove("show");
}

function toggleSidebar() {
  if (!isMobileViewport()) {
    closeSidebarMobile();
    return false;
  }
  document.getElementById("sidebar").classList.toggle("show");
  document.getElementById("sidebarOverlay").classList.toggle("show");
  return false;
}

function navigateToPage(path) {
  closeSidebarMobile();
  if (window.location.pathname === path) return false;
  window.location.assign(path);
  return false;
}

window.addEventListener("resize", () => {
  if (!isMobileViewport()) closeSidebarMobile();
});

// --- 统一 API 封装 ---
async function api(path, options = {}) {
  const res = await fetch(`${API_BASE}${path}`, {
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok || data.code !== 200) {
    throw new Error(data?.detail?.message || data?.detail || data?.message || "请求失败");
  }
  return data.data;
}

// --- 时间格式化：统一到上海时区（修正 cookies 原先裸 new Date 解析的时区 bug）---
const SHANGHAI_TIME_ZONE = "Asia/Shanghai";
const SHANGHAI_OFFSET_SUFFIX = "+08:00";
const SHANGHAI_TIME_FORMATTER = new Intl.DateTimeFormat("zh-CN", {
  timeZone: SHANGHAI_TIME_ZONE,
  year: "numeric",
  month: "2-digit",
  day: "2-digit",
  hour: "2-digit",
  minute: "2-digit",
  second: "2-digit",
  hourCycle: "h23",
});

function normalizeTimeInput(value) {
  const text = String(value || "").trim();
  if (!text || text === "-") return "";
  const normalized = text.includes("T") ? text : text.replace(" ", "T");
  if (!/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}/.test(normalized)) return normalized;
  if (/[zZ]$|[+-]\d{2}:\d{2}$/.test(normalized)) return normalized;
  return `${normalized}${SHANGHAI_OFFSET_SUFFIX}`;
}

function parseTimeValue(value) {
  const normalized = normalizeTimeInput(value);
  if (!normalized) return null;
  const parsed = new Date(normalized);
  return Number.isNaN(parsed.getTime()) ? null : parsed;
}

function formatTimeParts(date) {
  return SHANGHAI_TIME_FORMATTER.formatToParts(date).reduce((accumulator, part) => {
    if (part.type !== "literal") accumulator[part.type] = part.value;
    return accumulator;
  }, {});
}

function formatTime(value, emptyText = "-") {
  const rawText = String(value || "").trim();
  if (!rawText || rawText === "-") return emptyText;
  const parsed = parseTimeValue(rawText);
  if (!parsed) return rawText.replace("T", " ").replace(/(\.\d+)?([+-]\d{2}:\d{2}|Z)$/i, "");
  const parts = formatTimeParts(parsed);
  return `${parts.year}-${parts.month}-${parts.day} ${parts.hour}:${parts.minute}:${parts.second}`;
}

function parseTimeToMillis(value) {
  const parsed = parseTimeValue(value);
  return parsed ? parsed.getTime() : 0;
}

// --- onclick 内联事件安全转义 ---
// 数据同时处于 HTML 双引号属性与 JS 单引号字符串双重上下文，
// 先做 JS 字符串转义再做 HTML 属性转义，浏览器解码属性后恰好还原为安全的 JS 字面量。
function jsAttr(value) {
  return String(value ?? "")
    .replaceAll("\\", "\\\\")
    .replaceAll("'", "\\'")
    .replaceAll("\n", "\\n")
    .replaceAll("\r", "\\r")
    .replaceAll("&", "&amp;")
    .replaceAll('"', "&quot;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;");
}

// --- 复制用户主页分享文本（dashboard/statistics 共用；依赖各页自有 showToast）---
function copyUserProfile(profileUrl, nickname) {
  if (!profileUrl) { showToast('该用户没有主页链接', 'error'); return; }
  // 如果已经是抖音分享文本格式（包含 v.douyin.com 短链接），直接复制
  let textToCopy = profileUrl;
  if (!profileUrl.includes('v.douyin.com')) {
    // 完整链接格式，拼成抖音可识别的分享文本
    textToCopy = `打开抖音搜索，查看TA的更多作品 ${profileUrl}`;
  }
  navigator.clipboard.writeText(textToCopy).then(() => {
    showToast(`已复制 ${nickname} 的抖音分享`);
  }).catch(() => {
    const ta = document.createElement('textarea');
    ta.value = textToCopy; document.body.appendChild(ta); ta.select();
    document.execCommand('copy'); document.body.removeChild(ta);
    showToast(`已复制 ${nickname} 的抖音分享`);
  });
}
