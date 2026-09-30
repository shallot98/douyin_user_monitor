// 三页共享前端逻辑（dashboard / statistics / cookies）。
// 加载顺序：本文件必须在各页依赖它的内联脚本之前引入。
// 说明：
//  - isMobileViewport 各页断点不同（cookies 900 / 其余 1024），仍留在各页内联脚本，
//    本文件的侧栏函数在“调用时”解析全局 isMobileViewport。
//  - showToast / escapeHtml / Icons / renderUserAvatar 各页实现不一致，未抽取，保留在各页。

const API_BASE = "/api/monitor";

const THEME_STORAGE_KEY = "douyin-monitor-theme";

function applyTheme(theme) {
  const dark = theme === "dark";
  document.body.classList.toggle("theme-dark", dark);
  document.querySelectorAll(".theme-toggle").forEach((button) => {
    button.title = dark ? "切换正常模式" : "切换暗黑模式";
    button.setAttribute("aria-label", button.title);
  });
}

function toggleTheme() {
  const next = document.body.classList.contains("theme-dark") ? "light" : "dark";
  localStorage.setItem(THEME_STORAGE_KEY, next);
  applyTheme(next);
  return false;
}

applyTheme(localStorage.getItem(THEME_STORAGE_KEY) || "light");

// --- 侧栏（移动端抽屉）交互 ---
function closeSidebarMobile() {
  document.getElementById("sidebar")?.classList.remove("show");
  document.getElementById("sidebarOverlay")?.classList.remove("show");
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
    cache: "no-store",
    ...options,
  });
  if (res.status === 401) {
    // 会话过期 / 未登录：回登录页，登录后回到当前页面
    redirectToLogin();
    throw new Error("登录已过期，正在跳转登录页");
  }
  const data = await res.json().catch(() => ({}));
  if (!res.ok || data.code !== 200) {
    throw new Error(data?.detail?.message || data?.detail || data?.message || "请求失败");
  }
  return data.data;
}

// --- 登录 / 退出 ---
function redirectToLogin() {
  const next = window.location.pathname + window.location.search;
  window.location.assign(`/login?next=${encodeURIComponent(next)}`);
}

async function logout() {
  await fetch("/auth/logout", { method: "POST", credentials: "same-origin" }).catch(() => {});
  redirectToLogin();
  return false;
}

// 启用了登录才在侧栏末尾加“退出登录”，免得每个页面各写一份
async function mountLogoutNav() {
  const nav = document.querySelector(".sidebar-nav");
  if (!nav) return;
  const res = await fetch("/auth/status", { cache: "no-store", credentials: "same-origin" }).catch(() => null);
  const status = res?.ok ? (await res.json().catch(() => ({})))?.data : null;
  if (!status?.login_enabled) return;
  const item = document.createElement("a");
  item.className = "nav-item";
  item.href = "#";
  item.title = "退出登录";
  item.setAttribute("aria-label", "退出登录");
  item.addEventListener("click", (event) => {
    event.preventDefault();
    logout();
  });
  item.innerHTML = '<svg xmlns="http://www.w3.org/2000/svg" fill="none" viewBox="0 0 24 24" stroke-width="1.5" stroke="currentColor"><path stroke-linecap="round" stroke-linejoin="round" d="M15.75 9V5.25A2.25 2.25 0 0 0 13.5 3h-6a2.25 2.25 0 0 0-2.25 2.25v13.5A2.25 2.25 0 0 0 7.5 21h6a2.25 2.25 0 0 0 2.25-2.25V15m3 0 3-3m0 0-3-3m3 3H9" /></svg>退出登录';
  nav.appendChild(item);
}

if (document.readyState === "loading") {
  document.addEventListener("DOMContentLoaded", mountLogoutNav);
} else {
  mountLogoutNav();
}

// --- 热更新：定时轮询 + 切回标签页 / bfcache 返回时立即重拉 ---
// 后台（轮换、测活、调度、其他标签页）产生的变化不依赖手动刷新就能出现。
// 标签页隐藏时跳过轮询；fn 自己负责并发去重（各页已有 seq 防回写）。
function startLiveRefresh(fn, intervalMs = 15000) {
  setInterval(() => {
    if (!document.hidden) fn();
  }, intervalMs);
  document.addEventListener("visibilitychange", () => {
    if (!document.hidden) fn();
  });
  // 浏览器前进/后退从 bfcache 还原时，页面脚本不重新执行，DOM 是离开时的旧快照
  window.addEventListener("pageshow", (event) => {
    if (event.persisted) fn();
  });
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
