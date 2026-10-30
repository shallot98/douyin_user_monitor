// 作品悬浮播放器（作品浏览页 / 作品库共用）。依赖 common.js 的 API_BASE 与 api()。
// 用法：WorksViewer.init({ defaultUserId, getItems, getListInfo, loadAdjacent, hasAdjacent, onPlayingChange, onEscapeWhenClosed })
(function () {
  "use strict";

  const STYLE_ID = "works-viewer-style";
  const VIEWER_CSS = `
    /* 悬浮播放器：不遮挡列表，可拖动标题栏；尺寸按作品宽高比由 JS 计算 */
    .viewer { position:fixed; z-index:80; right:16px; bottom:16px; display:none; flex-direction:column; overflow:hidden; color:#e2e8f0; background:#0b1220; border:1px solid rgba(148,163,184,.2); border-radius:16px; box-shadow:0 24px 60px rgba(0,0,0,.45), 0 2px 10px rgba(0,0,0,.3); }
    .viewer.show { display:flex; animation:viewer-in .18s ease-out; }
    .viewer.dragging { user-select:none; }
    @keyframes viewer-in { from { opacity:0; transform:translateY(12px) scale(.98); } to { opacity:1; transform:none; } }
    .viewer-head { display:flex; align-items:center; gap:8px; padding:8px 8px 8px 14px; cursor:move; touch-action:none; background:#111a2e; border-bottom:1px solid rgba(148,163,184,.14); }
    .viewer-title { flex:1; min-width:0; font-size:12.5px; font-weight:650; white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
    .viewer-counter { flex-shrink:0; font-size:11px; font-weight:700; color:#94a3b8; font-variant-numeric:tabular-nums; }
    .viewer-close { flex-shrink:0; width:28px; height:28px; border:0; border-radius:8px; background:transparent; color:#cbd5e1; cursor:pointer; display:grid; place-items:center; }
    .viewer-close:hover { background:rgba(148,163,184,.18); color:#fff; }
    .viewer-close svg { width:16px; height:16px; }
    /* 竖向手势留给播放器（上下滑切作品），不让背后的列表跟着滚/iOS 整页回弹 */
    .viewer { overscroll-behavior:contain; }
    .viewer-body { position:relative; touch-action:pan-x pinch-zoom; }
    .viewer-stage { position:relative; width:360px; height:640px; display:grid; place-items:center; background:#000; overflow:hidden; }
    /* 绝对定位铺满舞台：grid 子项的 height:100% 会退回视频原始尺寸，大分辨率视频被裁掉一截 */
    .viewer-stage video, .viewer-stage img { position:absolute; inset:0; display:block; width:100%; height:100%; object-fit:contain; }
    .viewer-stage .empty-state { color:#94a3b8; }
    .viewer-loading { position:absolute; inset:0; display:grid; place-items:center; color:#94a3b8; font-size:12.5px; font-weight:600; }
    /* 上/下一个作品：竖排在画面右侧中部，避开视频原生控制条 */
    .viewer-works { position:absolute; right:10px; top:50%; transform:translateY(-50%); z-index:2; display:flex; flex-direction:column; gap:10px; }
    .viewer-work-btn { width:38px; height:38px; border:1px solid rgba(255,255,255,.2); border-radius:999px; background:rgba(15,23,42,.55); color:#fff; backdrop-filter:blur(6px); cursor:pointer; display:grid; place-items:center; transition:background .15s ease, opacity .15s ease; }
    .viewer-work-btn:hover:not(:disabled) { background:rgba(15,23,42,.85); }
    .viewer-work-btn:disabled { opacity:.3; cursor:not-allowed; }
    .viewer-work-btn svg { width:18px; height:18px; }
    /* 图文翻页放在底部中间，和右侧的作品切换按钮分开 */
    .viewer-pager { position:absolute; left:50%; bottom:12px; transform:translateX(-50%); display:flex; align-items:center; gap:4px; padding:3px; border-radius:999px; background:rgba(15,23,42,.62); backdrop-filter:blur(6px); font-size:11.5px; font-weight:700; font-variant-numeric:tabular-nums; }
    .viewer-pager span { padding:0 6px; }
    .viewer-nav { width:28px; height:28px; border:0; border-radius:999px; background:transparent; color:#fff; cursor:pointer; display:grid; place-items:center; }
    .viewer-nav:hover { background:rgba(255,255,255,.16); }
    .viewer-nav svg { width:16px; height:16px; }
    .viewer-cf { padding:8px 14px; font-size:11.5px; line-height:1.5; color:#cbd5e1; background:#0f172a; border-top:1px solid rgba(148,163,184,.14); max-height:112px; overflow-y:auto; overscroll-behavior:contain; }
    .viewer-cf[hidden] { display:none; }
    .viewer-cf-summary { color:#e2e8f0; margin-bottom:5px; }
    .viewer-cf-verdict { color:#fda4af; font-weight:700; margin-right:6px; }
    .viewer-cf-line { margin-bottom:5px; color:#cbd5e1; }
    .viewer-cf-score { display:inline-block; padding:0 7px; margin-right:4px; border-radius:999px; line-height:18px; color:#fff; background:#475569; }
    .viewer-cf-score.borderline-1 { background:rgba(14,165,233,.85); }
    .viewer-cf-score.borderline-2 { background:rgba(99,102,241,.88); }
    .viewer-cf-score.borderline-3 { background:rgba(217,119,6,.9); }
    .viewer-cf-score.borderline-4 { background:rgba(234,88,12,.92); }
    .viewer-cf-score.borderline-5 { background:rgba(220,38,38,.92); }
    .viewer-cf-tags { display:flex; flex-wrap:wrap; gap:4px; }
    .viewer-cf-tag { padding:0 7px; border-radius:999px; line-height:18px; background:rgba(56,189,248,.14); color:#7dd3fc; }
    .viewer-cf-tag b { font-weight:600; color:#94a3b8; margin-right:3px; }
    .viewer-foot { display:flex; align-items:center; gap:8px; padding:8px 10px 8px 14px; font-size:11.5px; color:#94a3b8; background:#111a2e; border-top:1px solid rgba(148,163,184,.14); }
    .viewer-meta { flex:1; min-width:0; white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
    .viewer-link { flex-shrink:0; padding:4px 10px; border-radius:8px; border:1px solid rgba(148,163,184,.25); color:#e2e8f0; text-decoration:none; font-weight:650; }
    .viewer-link:hover { background:rgba(148,163,184,.16); }
    .viewer-link[hidden] { display:none; }
    @media (max-width:640px) {
      .viewer { left:12px !important; right:12px; top:auto !important; bottom:calc(12px + env(safe-area-inset-bottom)); border-radius:14px; }
      .viewer-head { cursor:default; }
      .viewer-stage { width:100% !important; }
    }
  `;

  const VIEWER_HTML = `
    <div class="viewer-head" id="viewerHead">
      <div class="viewer-title" id="viewerTitle">-</div>
      <span class="viewer-counter" id="viewerCounter"></span>
      <button type="button" class="viewer-close" id="viewerClose" aria-label="关闭">
        <svg xmlns="http://www.w3.org/2000/svg" fill="none" viewBox="0 0 24 24" stroke-width="2" stroke="currentColor"><path stroke-linecap="round" stroke-linejoin="round" d="M6 18 18 6M6 6l12 12" /></svg>
      </button>
    </div>
    <div class="viewer-body" id="viewerBody">
      <div class="viewer-stage" id="viewerStage"></div>
      <div class="viewer-works">
        <button type="button" class="viewer-work-btn" id="viewerPrevWork" aria-label="上一个作品" title="上一个作品（↑）">
          <svg xmlns="http://www.w3.org/2000/svg" fill="none" viewBox="0 0 24 24" stroke-width="2.2" stroke="currentColor"><path stroke-linecap="round" stroke-linejoin="round" d="m4.5 15.75 7.5-7.5 7.5 7.5" /></svg>
        </button>
        <button type="button" class="viewer-work-btn" id="viewerNextWork" aria-label="下一个作品" title="下一个作品（↓）">
          <svg xmlns="http://www.w3.org/2000/svg" fill="none" viewBox="0 0 24 24" stroke-width="2.2" stroke="currentColor"><path stroke-linecap="round" stroke-linejoin="round" d="m19.5 8.25-7.5 7.5-7.5-7.5" /></svg>
        </button>
      </div>
    </div>
    <div class="viewer-cf" id="viewerContentLabels" hidden></div>
    <div class="viewer-foot">
      <span class="viewer-meta" id="viewerMeta">-</span>
      <a class="viewer-link" id="viewerOriginalLink" download hidden>下载原片</a>
      <a class="viewer-link" id="viewerShareLink" target="_blank" rel="noreferrer noopener">抖音打开</a>
    </div>`;

  const PREV_IMAGE_ICON = '<svg xmlns="http://www.w3.org/2000/svg" fill="none" viewBox="0 0 24 24" stroke-width="2.2" stroke="currentColor"><path stroke-linecap="round" stroke-linejoin="round" d="M15.75 19.5 8.25 12l7.5-7.5" /></svg>';
  const NEXT_IMAGE_ICON = '<svg xmlns="http://www.w3.org/2000/svg" fill="none" viewBox="0 0 24 24" stroke-width="2.2" stroke="currentColor"><path stroke-linecap="round" stroke-linejoin="round" d="m8.25 4.5 7.5 7.5-7.5 7.5" /></svg>';

  const DIRECT_FAILURE_LIMIT = 2;
  // 过期前留 1 分钟余量，避免拿到临界直链
  const DIRECT_URL_REFRESH_MARGIN_MS = 60 * 1000;
  // 直链接口整体失败时，这段时间内相关作品直接走代理，避免每次点开都先白等一次
  const DIRECT_URL_FAILURE_BACKOFF_MS = 60 * 1000;
  const VIEWER_MARGIN = 16;
  // 标题栏 + 底栏的大致高度，给画面留出空间
  const VIEWER_CHROME_HEIGHT = 96;

  const CATEGORY_LABELS = { ad: "广告", text_only: "纯文字", screenshot: "截图", normal: "正常" };
  const AD_TYPE_LABELS = { selling: "带货", sponsored: "商单", traffic: "引流", local_promo: "探店团购" };
  // 标签分组：[来源, 键, 显示名]；来源 tags=作品标签，appearance=人物外观
  const TAG_GROUPS = [
    ["tags", "content", "内容"], ["appearance", "outfit", "着装"], ["tags", "style", "风格"],
    ["tags", "scene", "场景"], ["tags", "subject", "主体"], ["appearance", "exposed_areas", "部位"],
    ["appearance", "camera_focus", "镜头"], ["appearance", "poses", "姿态"], ["tags", "features", "特征"],
  ];
  const BORDERLINE_FIELDS = [
    ["chest", "胸部", 3], ["body", "身材", 3], ["overall", "整体", 3], ["camera", "镜头", 3], ["motion", "动作", 3],
  ];

  let opts = null;
  // 播放器打开期间列表可以翻页/加载：item 是正在播的作品，index 是它在 getItems() 里的位置，
  // 不在当前列表时为 -1（此时上/下一个从列表首/尾开始）。
  const vs = {
    open: false,
    item: null,
    index: -1,
    busy: false,
    ratio: 9 / 16,
    pos: null,
    imageIndex: 0,
    // 直链：aweme_id → { url: string|null, images: Array|null, expiresAt }；null 表示只能走代理
    directUrls: new Map(),
    directPending: new Map(),
    directFailures: 0,
    directDisabled: false,
    lastPrefetchedAwemeId: "",
  };

  function $(id) { return document.getElementById(id); }

  function escapeHtml(value) {
    return String(value ?? "")
      .replaceAll("&", "&amp;")
      .replaceAll("<", "&lt;")
      .replaceAll(">", "&gt;")
      .replaceAll('"', "&quot;")
      .replaceAll("'", "&#39;");
  }

  // 作品数据来自抖音端，属不可信输入：URL 只放行 http(s)，杜绝 javascript: 之类协议
  function safeMediaUrl(value) {
    const text = String(value || "").trim();
    return /^https?:\/\//i.test(text) ? text : "";
  }

  function formatDuration(ms) {
    const total = Math.max(0, Math.round(Number(ms || 0) / 1000));
    const minutes = Math.floor(total / 60);
    const seconds = total % 60;
    return `${minutes}:${String(seconds).padStart(2, "0")}`;
  }

  function requireInit() {
    if (!opts) throw new Error("WorksViewer 尚未 init");
  }

  function items() {
    requireInit();
    const list = opts.getItems();
    return Array.isArray(list) ? list : [];
  }

  // 作品自带 user_id（作品库跨用户列表）优先，否则用页面给的默认用户
  function userIdOf(item) {
    const userId = String((item && item.user_id) || (opts && opts.defaultUserId) || "").trim();
    if (!userId) throw new Error(`作品 ${item && item.aweme_id} 缺少 user_id`);
    return userId;
  }

  function archiveBase(item) {
    return `${API_BASE}/users/${encodeURIComponent(userIdOf(item))}/archive`;
  }

  function archiveMediaUrl(item, fileIndex) {
    return `${archiveBase(item)}/${encodeURIComponent(item.aweme_id)}/media/${Number(fileIndex)}`;
  }

  // 独立封面只认本作品的本地带版本地址，其余回退到归档里的封面文件
  function coverImageUrl(item) {
    const local = String(item.cover_url || "");
    const prefix = `${archiveBase(item)}/${encodeURIComponent(item.aweme_id)}/cover?v=`;
    if (local.startsWith(prefix) && /^[0-9a-f]{64}$/.test(local.slice(prefix.length))) return local;
    return item.cover_index !== null && item.cover_index !== undefined
      ? archiveMediaUrl(item, item.cover_index) : "";
  }

  // ---- 内容标签面板 ----

  function categoryLabel(result) {
    const label = result.category === "ad"
      ? `广告·${AD_TYPE_LABELS[result.ad_type] || "其他"}`
      : CATEGORY_LABELS[result.category] || result.category;
    return `${label} ${Math.round(Number(result.confidence || 0) * 100)}%`;
  }

  function groupValues(result, source, key) {
    const bag = result && result[source] ? result[source] : {};
    return Array.isArray(bag[key]) ? bag[key] : [];
  }

  function borderlineOf(result) {
    const borderline = result && result.appearance ? result.appearance.borderline : null;
    return borderline && Number.isInteger(borderline.score) ? borderline : null;
  }

  function renderBorderlineLine(result) {
    const appearance = result.appearance || {};
    const borderline = borderlineOf(result);
    if (!borderline) {
      return appearance.age ? `<div class="viewer-cf-line">年龄判断：${escapeHtml(appearance.age)}（不做擦边评分）</div>` : "";
    }
    // 旧版结果没有身材分项，显示为 –
    const parts = BORDERLINE_FIELDS.map(([key, label, max]) => `${label} ${borderline[key] ?? "–"}/${max}`);
    const enhancers = Array.isArray(borderline.enhancers) && borderline.enhancers.length
      ? ` · 增强：${borderline.enhancers.join("、")}` : "";
    const violation = borderline.violation ? ' <span class="viewer-cf-verdict">疑似违规</span>' : "";
    const level = Math.min(Math.max(borderline.score, 0), 5);
    return `<div class="viewer-cf-line"><b class="viewer-cf-score borderline-${level}">擦边 ${borderline.score}/5</b>`
      + ` ${escapeHtml(parts.join(" · ") + enhancers)}${violation}</div>`;
  }

  function renderContentLabels(item) {
    const box = $("viewerContentLabels");
    const result = item && item.content_filter;
    if (!result) {
      box.hidden = true;
      box.innerHTML = "";
      return;
    }
    if (result.status === "error") {
      box.innerHTML = `<div class="viewer-cf-summary">未检测：${escapeHtml(result.error || "未知错误")}</div>`;
      box.hidden = false;
      return;
    }
    const verdict = result.verdict === "reject"
      ? `<span class="viewer-cf-verdict">${escapeHtml(categoryLabel(result))}</span>` : "";
    const pills = TAG_GROUPS.flatMap(([source, key, label]) => groupValues(result, source, key)
      .map((tag) => `<span class="viewer-cf-tag"><b>${label}</b>${escapeHtml(tag)}</span>`));
    box.innerHTML = `
      <div class="viewer-cf-summary">${verdict}${escapeHtml(result.summary || result.reason || "")}</div>
      ${renderBorderlineLine(result)}
      ${pills.length ? `<div class="viewer-cf-tags">${pills.join("")}</div>` : ""}`;
    box.hidden = false;
  }

  // ---- 打开 / 切换作品 ----

  function open(index, options = {}) {
    const item = items()[index];
    if (!item) return;
    vs.open = true;
    vs.item = item;
    vs.index = index;
    vs.imageIndex = 0;

    const desc = String(item.desc || "").trim();
    $("viewerTitle").textContent = desc || item.aweme_id;
    $("viewerTitle").title = desc;
    const shareLink = $("viewerShareLink");
    const shareUrl = safeMediaUrl(item.share_url);
    shareLink.href = shareUrl || "#";
    shareLink.hidden = !shareUrl;
    const originalLink = $("viewerOriginalLink");
    const hasOriginal = item.media_type === "video" && item.archive_index !== null && item.archive_index !== undefined;
    originalLink.hidden = !hasOriginal;
    if (hasOriginal) originalLink.href = archiveMediaUrl(item, item.archive_index);
    else originalLink.removeAttribute("href");

    $("viewer").classList.add("show");
    renderStage();
    // 服务端缓存预取只对走代理的视频有用：视频等出画面再决定（见 onVideoReady）；
    // 图文作品只在视频直链停用时才替相邻视频预取
    if (!$("viewerStage").querySelector("video") && vs.directDisabled) prefetchNeighbors();
    updateWorkNav();
    notifyPlaying(options.scroll);
  }

  function notifyPlaying(scroll = false, extra = {}) {
    if (opts.onPlayingChange) opts.onPlayingChange(vs.open ? vs.index : -1, { scroll: Boolean(scroll), ...extra });
  }

  // 列表变化（翻页/筛选/加载更多）后重新定位正在播的作品；它可能已不在列表里
  // 作品库跨用户：同一 aweme_id 可能挂在多个用户下，按 用户+作品 认定同一条
  function sameWork(a, b) {
    return a.aweme_id === b.aweme_id && (a.user_id || null) === (b.user_id || null);
  }

  function syncAfterReload() {
    if (!vs.open || !vs.item) return;
    const playing = vs.item;
    vs.index = items().findIndex((item) => sameWork(item, playing));
    updateWorkNav();
    notifyPlaying();
  }

  function hasAdjacentWork(delta) {
    const list = items();
    const idx = vs.index;
    const inList = delta > 0 ? idx >= 0 && idx < list.length - 1 : idx > 0;
    const fromOutside = idx < 0 && list.length > 0;
    const beyond = opts.hasAdjacent ? Boolean(opts.hasAdjacent(delta)) : false;
    return inList || fromOutside || beyond;
  }

  function updateWorkNav() {
    if (!vs.open) return;
    $("viewerPrevWork").disabled = vs.busy || !hasAdjacentWork(-1);
    $("viewerNextWork").disabled = vs.busy || !hasAdjacentWork(1);
    const info = opts.getListInfo ? opts.getListInfo() : { offset: 0, total: items().length };
    const total = Number(info && info.total) || 0;
    const offset = Number(info && info.offset) || 0;
    $("viewerCounter").textContent = vs.index >= 0 && total ? `${offset + vs.index + 1} / ${total}` : "";
  }

  // 上/下一个作品；到列表边界时让页面加载相邻一段，接着播它的首/尾
  async function stepWork(delta) {
    if (!vs.open || vs.busy || !hasAdjacentWork(delta)) return;
    const list = items();
    const idx = vs.index;
    const target = idx < 0 ? (delta > 0 ? 0 : list.length - 1) : idx + delta;
    if (target >= 0 && target < list.length) {
      open(target);
      return;
    }
    if (!opts.loadAdjacent) return;
    const step = delta > 0 ? 1 : -1;
    vs.busy = true;
    updateWorkNav();
    let changed = false;
    try {
      changed = await opts.loadAdjacent(step);
    } finally {
      vs.busy = false;
    }
    // 加载失败（页面已提示）就留在当前作品
    const loaded = items();
    if (!vs.open || !changed || !loaded.length) {
      updateWorkNav();
      return;
    }
    // 追加式加载（下一段接在后面）时正在播的作品仍在列表里，从它的下一个接着播
    const current = vs.item ? loaded.findIndex((item) => sameWork(item, vs.item)) : -1;
    const next = current >= 0 ? current + step : (step > 0 ? 0 : loaded.length - 1);
    if (next < 0 || next >= loaded.length) {
      vs.index = current;
      updateWorkNav();
      return;
    }
    open(next);
  }

  function currentSource(item) {
    return item.has_fast_play ? item.fast_play_source : item.video_source;
  }

  function renderStage() {
    const item = vs.item;
    const stage = $("viewerStage");
    const meta = $("viewerMeta");
    if (!item) return;
    stopMedia();
    renderContentLabels(item);

    if (String(item.media_type) === "image") {
      renderImageStage(item, stage, meta);
      return;
    }

    const source = currentSource(item);
    // 先用记录里的分辨率定尺寸，metadata 到了再以真实宽高（含旋转）校正
    sizeStage(Number(source?.width || 9), Number(source?.height || 16));
    if (item.play_index === null || item.play_index === undefined) {
      stage.innerHTML = '<div class="empty-state">该视频没有可播放文件</div>';
      meta.textContent = "视频 · 无可播放文件";
      return;
    }
    stage.innerHTML = "";
    const video = createVideo(item);
    stage.appendChild(video);
    const direct = vs.directDisabled ? null : freshDirectUrl(item.aweme_id);
    if (direct !== undefined) {
      // 直链已就绪（列表加载时批量取好）或已知只能代理：同步设 src，保住点击手势的自动播放
      setVideoSource(video, item, direct);
      return;
    }
    setVideoMeta(item, "正在获取播放地址…");
    getDirectEntry(item).then((entry) => {
      // 等待期间用户可能已切走
      if (vs.item !== item || !video.isConnected) return;
      setVideoSource(video, item, entry?.url ?? null);
      video.play().catch(() => {});  // 异步设 src 后自动播放可能被拦，用户点播放即可
    });
  }

  function createVideo(item) {
    const video = document.createElement("video");
    video.controls = true;
    video.autoplay = true;
    video.playsInline = true;
    video.preload = "metadata";
    video.setAttribute("playsinline", "");
    const coverUrl = coverImageUrl(item);
    if (coverUrl) video.poster = coverUrl;
    video.addEventListener("loadedmetadata", () => sizeStage(video.videoWidth, video.videoHeight));
    video.addEventListener("loadeddata", () => onVideoReady(video));
    video.addEventListener("error", () => onVideoError(video));
    return video;
  }

  function renderImageStage(item, stage, meta) {
    // 服务端已排除单独存的封面，只展示内容图片。
    const indices = Array.isArray(item.image_indices) ? item.image_indices : [];
    const urls = indices.map((i) => archiveMediaUrl(item, i));
    if (!urls.length) {
      sizeStage(9, 16);
      stage.innerHTML = '<div class="empty-state">该图文没有已归档的图片</div>';
      meta.textContent = "图文 · 0 张";
      return;
    }
    const clamped = Math.min(Math.max(vs.imageIndex, 0), urls.length - 1);
    vs.imageIndex = clamped;
    const pagerHtml = urls.length > 1
      ? `<div class="viewer-pager">
           <button type="button" class="viewer-nav" data-image-step="-1" aria-label="上一张">${PREV_IMAGE_ICON}</button>
           <span>${clamped + 1} / ${urls.length}</span>
           <button type="button" class="viewer-nav" data-image-step="1" aria-label="下一张">${NEXT_IMAGE_ICON}</button>
         </div>`
      : "";
    // 图片尺寸未知：先沿用上一次的比例，加载后再贴合，避免翻页时窗口来回跳
    sizeStage(vs.ratio, 1);
    meta.textContent = `图文 · 第 ${clamped + 1} / ${urls.length} 张`;
    const entry = freshDirectEntry(item.aweme_id);
    if (entry === undefined) {
      // 先取直链（单条约 1 秒）；经代理冷读一张图要 3~5 秒
      stage.innerHTML = `<div class="viewer-loading">正在获取图片地址…</div>${pagerHtml}`;
      const imageIndex = clamped;
      getDirectEntry(item).then(() => {
        if (vs.item === item && vs.imageIndex === imageIndex) renderStage();
      });
      return;
    }
    const direct = entry?.images?.[clamped] || null;
    const mode = direct ? "direct" : "proxy";
    stage.innerHTML = `<div class="viewer-loading">图片加载中…</div>${pagerHtml}`;
    const image = document.createElement("img");
    image.alt = "";
    image.dataset.mode = mode;
    image.dataset.proxy = urls[clamped];
    image.addEventListener("load", () => onImageReady(image));
    image.addEventListener("error", () => onImageError(image));
    image.src = direct || urls[clamped];
    // 图片压在加载提示上、翻页条下
    stage.insertBefore(image, stage.querySelector(".viewer-pager"));
    meta.textContent += ` · ${mode === "direct" ? "直连" : "代理"}`;
    // 预载下一张，翻页时直接出图（直链没有就不预载，免得给代理加负担）
    const nextDirect = entry?.images?.[clamped + 1];
    if (nextDirect) new Image().src = nextDirect;
  }

  // direct 为 null 时走服务端代理：源站在国内、本服务在美国，冷启动慢，只作兜底
  function setVideoSource(video, item, direct, note = "") {
    const mode = direct ? "direct" : "proxy";
    video.dataset.mode = mode;
    video.src = direct || archiveMediaUrl(item, item.play_index);
    setVideoMeta(item, note || (mode === "direct" ? "直连" : "代理"));
  }

  function setVideoMeta(item, modeLabel) {
    const source = currentSource(item);
    const quality = source && source.height ? ` · ${source.width}x${source.height}` : "";
    const label = item.has_fast_play ? "流畅播放" : "原片";
    $("viewerMeta").textContent = `视频 · ${label}${quality} · ${formatDuration(item.duration_ms)} · ${modeLabel}`;
  }

  function onVideoReady(video) {
    if (video.dataset.mode === "direct") {
      vs.directFailures = 0;
      return;
    }
    // 代理模式下当前视频出画面后再预取相邻作品，不和它抢首屏回源
    prefetchNeighbors();
  }

  function onVideoError(video) {
    const item = vs.item;
    if (!item || video.dataset.mode !== "direct") {
      showVideoError();
      prefetchNeighbors();
      return;
    }
    // 直链失败（过期/被拒/浏览器不认）：丢掉这条，改走代理重播；连续失败就本次会话不再用直链
    vs.directUrls.delete(item.aweme_id);
    vs.directFailures += 1;
    if (vs.directFailures >= DIRECT_FAILURE_LIMIT) vs.directDisabled = true;
    console.warn("直链播放失败，改走代理", item.aweme_id, video.error, { directDisabled: vs.directDisabled });
    setVideoSource(video, item, null, vs.directDisabled ? "代理（直连连续失败，已停用）" : "代理（直连失败）");
    video.play().catch(() => {});
  }

  function showVideoError() {
    $("viewerMeta").textContent = "视频加载失败，请重新打开重试";
  }

  function onImageReady(image) {
    image.parentElement?.querySelector(".viewer-loading")?.remove();
    sizeStage(image.naturalWidth, image.naturalHeight);
  }

  function onImageError(image) {
    if (image.dataset.mode === "direct") {
      // 直链失败（过期/被拒）：这张改走代理，作品的直链作废，下次重取
      console.warn("图片直链加载失败，改走代理", vs.item?.aweme_id);
      if (vs.item) vs.directUrls.delete(vs.item.aweme_id);
      image.dataset.mode = "proxy";
      image.src = image.dataset.proxy;
      const meta = $("viewerMeta");
      meta.textContent = meta.textContent.replace(/直连$/, "代理（直连失败）");
      return;
    }
    const loading = image.parentElement?.querySelector(".viewer-loading");
    if (loading) loading.textContent = "图片加载失败，请重新打开重试";
    $("viewerMeta").textContent = "图片加载失败，请重新打开重试";
  }

  function stepImage(delta) {
    if (vs.item?.media_type !== "image") return;
    vs.imageIndex += Number(delta || 0);
    renderStage();
  }

  // ---- 直链缓存 ----

  // 返回 { url, images } 或 undefined（没取过或已过期）。url/images 里的 null 表示该文件只能走代理
  function freshDirectEntry(awemeId) {
    const entry = vs.directUrls.get(awemeId);
    if (!entry || Date.now() >= entry.expiresAt - DIRECT_URL_REFRESH_MARGIN_MS) return undefined;
    return entry;
  }

  // 视频直链：字符串=可用；null=只能走代理；undefined=还不知道
  function freshDirectUrl(awemeId) {
    const entry = freshDirectEntry(awemeId);
    return entry === undefined ? undefined : entry.url;
  }

  // 一批只能属于同一个用户（接口按用户归档目录解析）
  function fetchDirectUrls(userId, awemeIds) {
    const pending = api(`/users/${encodeURIComponent(userId)}/archive/playback-urls`, {
      method: "POST",
      body: JSON.stringify({ aweme_ids: awemeIds }),
    }).then((data) => {
      const expiresAt = Date.now() + Number(data.ttl_seconds || 0) * 1000;
      for (const [awemeId, url] of Object.entries(data.urls || {})) {
        vs.directUrls.set(awemeId, { url: safeMediaUrl(url) || null, images: null, expiresAt });
      }
      for (const [awemeId, images] of Object.entries(data.image_urls || {})) {
        const list = (Array.isArray(images) ? images : []).map((url) => safeMediaUrl(url) || null);
        vs.directUrls.set(awemeId, { url: null, images: list, expiresAt });
      }
      const errors = data.errors || {};
      // 解析失败的作品这段时间内直接走代理（代理自己会再试主力盘/备份盘）
      for (const awemeId of Object.keys(errors)) vs.directUrls.set(awemeId, { url: null, images: null, expiresAt });
      if (Object.keys(errors).length) console.warn("部分作品直链解析失败，改走代理", errors);
      // 响应里没提到的作品也按“只能代理”记下，否则图文会反复重取直链
      const answered = new Set([...Object.keys(data.urls || {}), ...Object.keys(data.image_urls || {}), ...Object.keys(errors)]);
      const missing = awemeIds.filter((awemeId) => !answered.has(awemeId));
      missing.forEach((awemeId) => vs.directUrls.set(awemeId, { url: null, images: null, expiresAt }));
      if (missing.length) console.warn("直链接口未返回这些作品，改走代理", missing);
    }).catch((error) => {
      console.warn("获取直链失败，改走代理", error);
      const expiresAt = Date.now() + DIRECT_URL_REFRESH_MARGIN_MS + DIRECT_URL_FAILURE_BACKOFF_MS;
      awemeIds.forEach((awemeId) => vs.directUrls.set(awemeId, { url: null, images: null, expiresAt }));
    }).finally(() => {
      awemeIds.forEach((awemeId) => {
        if (vs.directPending.get(awemeId) === pending) vs.directPending.delete(awemeId);
      });
    });
    pending.batchSize = awemeIds.length;
    awemeIds.forEach((awemeId) => vs.directPending.set(awemeId, pending));
    return pending;
  }

  async function getDirectEntry(item) {
    const awemeId = item.aweme_id;
    if (freshDirectEntry(awemeId) === undefined) {
      // 整批要等最慢的那条，点开的这条单独取更快；单条请求已在路上就复用
      const pending = vs.directPending.get(awemeId);
      await (pending && pending.batchSize === 1 ? pending : fetchDirectUrls(userIdOf(item), [awemeId]));
    }
    return freshDirectEntry(awemeId) ?? null;
  }

  // 列表加载后批量取直链，点开时通常已就绪；跨用户列表按 user_id 分批
  function prefetchDirectUrls(list) {
    requireInit();
    const groups = new Map();
    for (const item of Array.isArray(list) ? list : []) {
      // 视频直链被本会话停用时，图文照样直连（<img> 不挑 Content-Type）
      const wanted = item.media_type === "image"
        ? (item.image_indices || []).length > 0
        : !vs.directDisabled && item.play_index !== null && item.play_index !== undefined;
      if (!wanted || freshDirectEntry(item.aweme_id) !== undefined || vs.directPending.has(item.aweme_id)) continue;
      const userId = userIdOf(item);
      if (!groups.has(userId)) groups.set(userId, []);
      groups.get(userId).push(item.aweme_id);
    }
    groups.forEach((ids, userId) => fetchDirectUrls(userId, ids));
  }

  // 服务端按该用户列表顺序把后 2 个、前 1 个作品的播放文件拉进本机缓存，
  // 点上/下一个时直接本机应答。只是加速手段，失败不打扰用户。
  function prefetchNeighbors() {
    const item = vs.item;
    const awemeId = item?.aweme_id;
    if (!awemeId || awemeId === vs.lastPrefetchedAwemeId) return;
    vs.lastPrefetchedAwemeId = awemeId;
    api(`/users/${encodeURIComponent(userIdOf(item))}/archive/${encodeURIComponent(awemeId)}/prefetch`, { method: "POST" })
      .catch((error) => console.warn("相邻作品预取失败", awemeId, error));
  }

  // ---- 尺寸 / 拖动 ----

  function isCompactViewer() { return window.innerWidth <= 640; }

  // 按宽高比在视口内取最大尺寸：竖屏视频不再被塞进 16:9 黑框
  function sizeStage(width, height) {
    const w = Number(width);
    const h = Number(height);
    if (w > 0 && h > 0) vs.ratio = w / h;
    const ratio = vs.ratio;
    const stage = $("viewerStage");
    const compact = isCompactViewer();
    const maxW = compact ? window.innerWidth - 26 : Math.min(window.innerWidth - VIEWER_MARGIN * 2 - 2, 760);
    const maxH = Math.max(180, compact
      ? window.innerHeight * 0.62
      : Math.min(window.innerHeight - VIEWER_MARGIN * 2 - VIEWER_CHROME_HEIGHT, 720));
    let stageW = maxH * ratio;
    let stageH = maxH;
    if (stageW > maxW) { stageW = maxW; stageH = maxW / ratio; }
    // 太窄时标题栏挤不下，宽度兜底，画面 contain 留黑边
    stageW = Math.min(maxW, Math.max(stageW, 320));
    stage.style.width = `${Math.round(stageW)}px`;
    stage.style.height = `${Math.round(compact ? Math.min(stageH, maxW / ratio) : stageH)}px`;
    // 窗口宽度跟随画面，不让长标题/底栏把窗口撑宽（+2 为边框）
    $("viewer").style.width = compact ? "" : `${Math.round(stageW) + 2}px`;
    clampViewerPosition();
  }

  function applyViewerPosition() {
    const viewer = $("viewer");
    if (!vs.pos || isCompactViewer()) {
      viewer.style.left = viewer.style.top = "";
      viewer.style.right = viewer.style.bottom = "";
      return;
    }
    viewer.style.left = `${vs.pos.x}px`;
    viewer.style.top = `${vs.pos.y}px`;
    viewer.style.right = viewer.style.bottom = "auto";
  }

  // 拖过的窗口在尺寸变化/视口缩放后仍要留在屏幕内
  function clampViewerPosition() {
    if (!vs.pos) { applyViewerPosition(); return; }
    const viewer = $("viewer");
    const maxX = Math.max(VIEWER_MARGIN, window.innerWidth - viewer.offsetWidth - VIEWER_MARGIN);
    const maxY = Math.max(VIEWER_MARGIN, window.innerHeight - viewer.offsetHeight - VIEWER_MARGIN);
    vs.pos = {
      x: Math.min(Math.max(vs.pos.x, VIEWER_MARGIN), maxX),
      y: Math.min(Math.max(vs.pos.y, VIEWER_MARGIN), maxY),
    };
    applyViewerPosition();
  }

  // 画面区竖向滑动切换作品；底部原生控制条那一截不算（留给拖进度）
  const SWIPE_MIN = 60;
  const CONTROLS_STRIP = 56;
  function initSwipe() {
    const body = $("viewerBody");
    let touch = null;
    body.addEventListener("touchstart", (event) => {
      if (event.touches.length !== 1 || event.target.closest("button, a")) { touch = null; return; }
      const point = event.touches[0];
      const rect = $("viewerStage").getBoundingClientRect();
      touch = { x: point.clientX, y: point.clientY, swipe: point.clientY < rect.bottom - CONTROLS_STRIP };
    }, { passive: true });
    body.addEventListener("touchmove", (event) => {
      if (!touch || event.touches.length !== 1) return;
      const point = event.touches[0];
      const dx = point.clientX - touch.x;
      const dy = point.clientY - touch.y;
      // 竖向为主就吃掉，iOS 老版本不认 touch-action 时靠这里挡住整页回弹
      if (Math.abs(dy) > Math.abs(dx) && event.cancelable) event.preventDefault();
    }, { passive: false });
    body.addEventListener("touchend", (event) => {
      if (!touch) return;
      const point = event.changedTouches[0];
      const dx = point.clientX - touch.x;
      const dy = point.clientY - touch.y;
      const swipe = touch.swipe;
      touch = null;
      if (swipe && Math.abs(dy) >= SWIPE_MIN && Math.abs(dy) > Math.abs(dx) * 1.5) stepWork(dy < 0 ? 1 : -1);
    });
    body.addEventListener("touchcancel", () => { touch = null; });
  }

  function initDrag() {
    const viewer = $("viewer");
    const head = $("viewerHead");
    let drag = null;
    head.addEventListener("pointerdown", (event) => {
      if (event.button !== 0 || isCompactViewer() || event.target.closest("button")) return;
      const rect = viewer.getBoundingClientRect();
      drag = { id: event.pointerId, dx: event.clientX - rect.left, dy: event.clientY - rect.top };
      head.setPointerCapture(event.pointerId);
      viewer.classList.add("dragging");
    });
    head.addEventListener("pointermove", (event) => {
      if (!drag || event.pointerId !== drag.id) return;
      vs.pos = { x: event.clientX - drag.dx, y: event.clientY - drag.dy };
      clampViewerPosition();
    });
    const endDrag = (event) => {
      if (!drag || event.pointerId !== drag.id) return;
      drag = null;
      viewer.classList.remove("dragging");
    };
    head.addEventListener("pointerup", endDrag);
    head.addEventListener("pointercancel", endDrag);
  }

  // ---- 关闭 / 键盘 / 初始化 ----

  function stopMedia() {
    const video = $("viewerStage").querySelector("video");
    if (!video) return;
    video.pause();
    video.removeAttribute("src");
    video.load();
  }

  function close() {
    if (!opts) return;
    stopMedia();
    $("viewerStage").innerHTML = "";
    const lastIndex = vs.index;
    vs.open = false;
    vs.item = null;
    vs.index = -1;
    $("viewer").classList.remove("show");
    notifyPlaying(false, { revealIndex: lastIndex });
  }

  function onKeydown(event) {
    if (!vs.open) {
      // 没开播放器时，Esc 交给页面（嵌入浮层时焦点在 iframe 内，父页收不到按键）
      if (event.key === "Escape" && opts.onEscapeWhenClosed) opts.onEscapeWhenClosed();
      return;
    }
    if (event.target?.closest?.("input, select, textarea")) return;
    if (event.key === "Escape") close();
    else if (event.key === "ArrowLeft") stepImage(-1);
    else if (event.key === "ArrowRight") stepImage(1);
    else if (event.key === "ArrowUp" || event.key === "ArrowDown") {
      event.preventDefault();
      stepWork(event.key === "ArrowDown" ? 1 : -1);
    }
  }

  function injectDom() {
    if (!document.getElementById(STYLE_ID)) {
      const style = document.createElement("style");
      style.id = STYLE_ID;
      style.textContent = VIEWER_CSS;
      document.head.appendChild(style);
    }
    if (document.getElementById("viewer")) throw new Error("页面已存在 #viewer，播放器不能重复挂载");
    const viewer = document.createElement("div");
    viewer.className = "viewer";
    viewer.id = "viewer";
    viewer.setAttribute("role", "dialog");
    viewer.setAttribute("aria-label", "作品播放器");
    viewer.innerHTML = VIEWER_HTML;
    document.body.appendChild(viewer);
  }

  function init(options) {
    if (opts) throw new Error("WorksViewer 只能 init 一次");
    if (!options || typeof options.getItems !== "function") throw new Error("WorksViewer.init 需要 getItems");
    opts = { ...options };
    injectDom();
    $("viewerClose").addEventListener("click", close);
    $("viewerPrevWork").addEventListener("click", () => stepWork(-1));
    $("viewerNextWork").addEventListener("click", () => stepWork(1));
    $("viewerStage").addEventListener("click", (event) => {
      const button = event.target.closest?.("[data-image-step]");
      if (button) stepImage(Number(button.dataset.imageStep));
    });
    initDrag();
    initSwipe();
    document.addEventListener("keydown", onKeydown);
    window.addEventListener("resize", () => {
      if (vs.open) sizeStage(vs.ratio, 1);
    });
  }

  window.WorksViewer = {
    init,
    open(index) { requireInit(); open(Number(index)); },
    close,
    isOpen: () => vs.open,
    currentAwemeId: () => (vs.open && vs.item ? String(vs.item.aweme_id) : null),
    prefetchDirectUrls,
    archiveMediaUrl,
    coverImageUrl,
    syncAfterReload,
    // 仅供测试/调试：翻图与视频失败提示
    stepImage,
    stepWork,
    showVideoError,
  };
})();
